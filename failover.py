"""Agent Failover — detects rate limits and auto-swaps to backup agents.

Runs as a background thread, monitoring agent health via:
- tmux screen output (terminal agents): looks for limit/quota/error patterns
- API response codes (API agents): 429, 503, quota exceeded
- Chat messages: agents self-reporting limits

When a limit is detected, the system:
1. Posts a notice to the chat
2. Stops the limited agent
3. Starts the next available backup with the same role
"""

import json
import re
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).parent

# Patterns that indicate rate limiting or quota exhaustion
LIMIT_PATTERNS = [
    # Claude Code
    r"usage limit",
    r"rate limit",
    r"you.ve hit your",
    r"limit reached",
    r"0% left",
    r"quota exceeded",
    r"too many requests",
    # Codex
    r"0% left",
    r"rate_limit_exceeded",
    r"billing_hard_limit_reached",
    # Gemini
    r"resource has been exhausted",
    r"quota.*exceeded",
    r"RESOURCE_EXHAUSTED",
    # Generic API
    r"429",
    r"503.*overloaded",
    r"capacity",
    r"throttl",
]

COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in LIMIT_PATTERNS]

# Low usage warning patterns (before full limit)
LOW_PATTERNS = [
    (r"(\d+)% left", lambda m: int(m.group(1)) <= 10),
]

# Failover chains: role -> ordered list of agents to try
# Each agent in the chain can fill the same role
FAILOVER_CHAINS = {
    "Builder": ["claude", "qwen-coder", "opencode", "codex", "qwen-api", "glm", "glm5-ds"],
    "Reviewer": ["codex", "glm", "glm5-ds", "minimax", "minimax-ds", "kimi-api", "gemini"],
    "Researcher": ["gemini", "kimi-api", "opencode", "qwen-api", "glm", "minimax"],
    "Architect": ["glm", "qwen-api", "claude", "minimax", "kimi-api", "opencode"],
    "Critic": ["minimax", "minimax-ds", "glm", "glm5-ds", "kimi-api", "codex", "gemini"],
    # Generic fallback: try any available agent (DashScope models last = cheapest failover)
    "_default": ["claude", "codex", "gemini", "opencode", "glm", "minimax",
                  "qwen-coder", "qwen-api", "kimi-api", "glm5-ds", "minimax-ds"],
}


class FailoverMonitor:
    def __init__(self, server_port=8300, check_interval=15):
        self.server_port = server_port
        self.check_interval = check_interval
        self._session_token = ""
        self._running = False
        self._thread = None
        self._cooldown = {}  # agent -> timestamp of last failover (prevent loops)

    def set_token(self, token):
        self._session_token = token

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def _api(self, path, method="GET", body=None):
        url = f"http://127.0.0.1:{self.server_port}{path}"
        if "?" in url:
            url += f"&token={self._session_token}"
        else:
            url += f"?token={self._session_token}"
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if body else {}
        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    def _get_agent_status(self):
        try:
            return self._api("/api/status")
        except Exception:
            return {}

    def _get_agent_role(self, name):
        status = self._get_agent_status()
        info = status.get(name, {})
        return info.get("role", "") if isinstance(info, dict) else ""

    def _check_tmux_for_limits(self, agent_name):
        """Check tmux output for rate limit patterns."""
        for session in [f"agentchattr-{agent_name}", f"ac-{agent_name}"]:
            try:
                r = subprocess.run(
                    ["tmux", "capture-pane", "-t", session, "-p"],
                    capture_output=True, text=True, timeout=5
                )
                if r.returncode != 0:
                    continue
                output = r.stdout
                for pattern in COMPILED_PATTERNS:
                    if pattern.search(output):
                        return True, pattern.pattern
                # Check low usage warnings
                for pattern_str, check_fn in LOW_PATTERNS:
                    for match in re.finditer(pattern_str, output, re.IGNORECASE):
                        if check_fn(match):
                            return True, f"low usage: {match.group(0)}"
            except Exception:
                continue
        return False, ""

    def _check_api_agent_logs(self, agent_name):
        """Check API agent log files for rate limit errors."""
        log_path = Path(f"/tmp/ac-{agent_name}.log")
        if not log_path.exists():
            return False, ""
        try:
            # Only check last 2KB of log
            content = log_path.read_text()[-2048:]
            for pattern in COMPILED_PATTERNS:
                if pattern.search(content):
                    return True, pattern.pattern
        except Exception:
            pass
        return False, ""

    def _find_replacement(self, limited_agent, role):
        """Find the next available agent in the failover chain."""
        chain = FAILOVER_CHAINS.get(role, FAILOVER_CHAINS["_default"])
        status = self._get_agent_status()

        # Get list of online agents
        online = {name for name, info in status.items()
                  if isinstance(info, dict) and info.get("available")}

        for candidate in chain:
            if candidate == limited_agent:
                continue
            # Skip if candidate is already online (already serving)
            if candidate in online:
                continue
            # Skip if in cooldown
            if candidate in self._cooldown:
                if time.time() - self._cooldown[candidate] < 300:  # 5 min cooldown
                    continue
            # Check if CLI is installed
            import manager
            clis = manager.detect_clis()
            if candidate in clis and clis[candidate]["installed"]:
                return candidate

        return None

    def _do_failover(self, limited_agent, reason, role):
        """Execute failover: stop limited, start replacement."""
        import manager

        # Cooldown check
        if limited_agent in self._cooldown:
            if time.time() - self._cooldown[limited_agent] < 300:
                return
        self._cooldown[limited_agent] = time.time()

        replacement = self._find_replacement(limited_agent, role)
        if not replacement:
            self._send_notice(
                f"@{limited_agent} hit limits ({reason}) but no replacement available."
            )
            return

        # Stop limited agent
        manager.stop_agent(limited_agent)

        # Start replacement
        result = manager.start_agent(replacement)

        # Set same role on replacement
        if role:
            try:
                time.sleep(8)  # Wait for registration
                self._api(f"/api/roles/{replacement}", "POST", {"role": role})
            except Exception:
                pass

        self._send_notice(
            f"Failover: @{limited_agent} hit limits ({reason}). "
            f"Replaced by @{replacement} (role: {role or 'none'})."
        )

    def _send_notice(self, text):
        """Send a system notice to the chat."""
        try:
            self._api("/api/send", "POST", {
                "text": text,
                "channel": "general",
                "sender": "system",
            })
        except Exception:
            pass

    def _monitor_loop(self):
        """Main monitoring loop."""
        while self._running:
            try:
                status = self._get_agent_status()
                for name, info in status.items():
                    if not isinstance(info, dict) or not info.get("available"):
                        continue

                    # Check terminal agents via tmux
                    limited, reason = self._check_tmux_for_limits(name)
                    if not limited:
                        # Check API agent logs
                        limited, reason = self._check_api_agent_logs(name)

                    if limited:
                        role = info.get("role", "")
                        self._do_failover(name, reason, role)

            except Exception:
                pass

            time.sleep(self.check_interval)


# Singleton instance
monitor = FailoverMonitor()
