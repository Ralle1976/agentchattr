"""Agent Pool — manages agent processes with PID tracking and auto-restart.

Each agent runs as a background process (wrapper_opencode.py or wrapper_api.py).
The pool tracks PIDs, monitors health, and restarts on failure.
"""

import json
import os
import re
import signal
import shutil
import subprocess
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent  # agentchattr/
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"
PID_DIR = ROOT / "orchestrator" / "pids"


@dataclass
class AgentInstance:
    """A running agent instance."""
    id: str                    # Unique ID: "oc-glm-1", "claude-1", "api-minimax"
    agent_type: str            # "opencode", "claude", "api"
    model: str                 # "zai-coding-plan/glm-5.1"
    label: str                 # "GLM-5.1 [thai]"
    cwd: str                   # Working directory
    project: str               # Project name (for grouping)
    channel: str               # Chat channel
    pid: Optional[int] = None
    registered_name: str = ""  # Name in AgentChattr (@opencode-3 etc.)
    started_at: float = 0.0
    restarts: int = 0
    last_error: str = ""
    status: str = "stopped"    # stopped, starting, running, error


def load_api_keys() -> dict:
    """Load all API keys from OpenCode auth + environment."""
    keys = {}
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
            key_map = {
                "minimax": "MINIMAX_API_KEY",
                "zai-coding-plan": "ZHIPU_API_KEY",
                "alibaba-coding-plan": "DASHSCOPE_API_KEY",
            }
            for provider, env_var in key_map.items():
                if provider in data and "key" in data[provider]:
                    keys[env_var] = data[provider]["key"]
        except Exception:
            pass
    # Also check existing env vars
    for var in ["DASHSCOPE_API_KEY", "MINIMAX_API_KEY", "ZHIPU_API_KEY"]:
        if os.environ.get(var):
            keys[var] = os.environ[var]
    return keys


class AgentPool:
    """Manages a pool of agent processes."""

    def __init__(self, chat_server_port: int = 8300):
        self.chat_port = chat_server_port
        self._root = ROOT
        self.agents: dict[str, AgentInstance] = {}
        self._lock = threading.Lock()
        self._counter = 0
        PID_DIR.mkdir(parents=True, exist_ok=True)
        self._cleanup_stale_pids()

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _cleanup_stale_pids(self):
        """Remove PID files for processes that no longer exist."""
        for pid_file in PID_DIR.glob("*.pid"):
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)

    def _write_pid(self, agent_id: str, pid: int):
        (PID_DIR / f"{agent_id}.pid").write_text(str(pid))

    def _read_pid(self, agent_id: str) -> Optional[int]:
        pid_file = PID_DIR / f"{agent_id}.pid"
        if pid_file.exists():
            try:
                return int(pid_file.read_text().strip())
            except ValueError:
                pass
        return None

    def _remove_pid(self, agent_id: str):
        (PID_DIR / f"{agent_id}.pid").unlink(missing_ok=True)

    def _is_process_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    def start_opencode(self, model: str, label: str, cwd: str,
                       project: str = "default", channel: str = "general") -> AgentInstance:
        """Start an OpenCode agent via wrapper_opencode.py."""
        agent_id = self._next_id("oc")

        env = {**os.environ, **load_api_keys(), "PYTHONUNBUFFERED": "1"}
        cmd = [
            VENV_PYTHON, "-u", str(ROOT / "wrapper_opencode.py"),
            "-m", model,
            "--label", label,
            "--cwd", cwd,
            "--channel", channel,
        ]
        log_file = f"/tmp/orchestrator-{agent_id}.log"

        with open(log_file, "w") as log:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), env=env,
                stdout=log, stderr=log,
                preexec_fn=os.setsid,  # Own session for clean kill + zombie reaping
            )

        agent = AgentInstance(
            id=agent_id,
            agent_type="opencode",
            model=model,
            label=label,
            cwd=cwd,
            project=project,
            channel=channel,
            pid=proc.pid,
            started_at=time.time(),
            status="starting",
        )

        self._write_pid(agent_id, proc.pid)

        with self._lock:
            self.agents[agent_id] = agent

        # Monitor registration in background
        def _wait_for_registration():
            for _ in range(30):
                time.sleep(2)
                name = self._find_registered_name(agent_id, label)
                if name:
                    with self._lock:
                        agent.registered_name = name
                        agent.status = "running"
                    return
            with self._lock:
                agent.status = "error"
                agent.last_error = "Registration timeout"

        threading.Thread(target=_wait_for_registration, daemon=True).start()
        return agent

    def start_claude(self, cwd: str, project: str = "default",
                     channel: str = "general") -> AgentInstance:
        """Start Claude Code CLI in tmux with auto-reconnect."""
        agent_id = self._next_id("claude")

        # Ensure no TMUX nesting issues
        env = {k: v for k, v in os.environ.items() if k != "TMUX"}
        session_name = f"orch-{agent_id}"

        wrapper_cmd = (
            f"cd {ROOT} && source .venv/bin/activate && "
            f"python wrapper.py claude --dangerously-skip-permissions"
        )

        subprocess.run([
            "tmux", "new-session", "-d", "-s", session_name, "-c", str(ROOT),
            "bash", "-lc", wrapper_cmd,
        ], env=env, timeout=10)

        # Get tmux server PID as our tracking PID
        try:
            r = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}:#{pid}"],
                capture_output=True, text=True, timeout=5
            )
            pid = None
            for line in r.stdout.strip().split("\n"):
                if session_name in line:
                    pid = int(line.split(":")[-1])
        except Exception:
            pid = None

        agent = AgentInstance(
            id=agent_id,
            agent_type="claude",
            model="claude-sonnet-4-6",
            label="Claude (Max Plan)",
            cwd=cwd,
            project=project,
            channel=channel,
            pid=pid,
            started_at=time.time(),
            status="starting",
        )

        if pid:
            self._write_pid(agent_id, pid)

        with self._lock:
            self.agents[agent_id] = agent

        # Auto-trust and wait for registration
        def _setup_claude():
            time.sleep(8)
            for s in _tmux_sessions():
                if "agentchattr-claude" in s:
                    try:
                        subprocess.run(["tmux", "send-keys", "-t", s, "Enter"],
                                       env=env, timeout=5)
                    except Exception:
                        pass
            time.sleep(5)
            name = self._find_registered_name(agent_id, "Claude")
            if name:
                with self._lock:
                    agent.registered_name = name
                    agent.status = "running"
            else:
                with self._lock:
                    agent.status = "error"
                    agent.last_error = "Registration timeout"

        threading.Thread(target=_setup_claude, daemon=True).start()
        return agent

    def start_api(self, agent_name: str, project: str = "default") -> AgentInstance:
        """Start a config-defined API agent via wrapper_api.py."""
        agent_id = self._next_id("api")

        env = {**os.environ, **load_api_keys()}
        cmd = [VENV_PYTHON, str(ROOT / "wrapper_api.py"), agent_name]
        log_file = f"/tmp/orchestrator-{agent_id}.log"

        with open(log_file, "w") as log:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), env=env,
                stdout=log, stderr=log,
                preexec_fn=os.setsid,
            )

        agent = AgentInstance(
            id=agent_id,
            agent_type="api",
            model=agent_name,
            label=agent_name,
            cwd="",
            project=project,
            channel="general",
            pid=proc.pid,
            started_at=time.time(),
            status="starting",
        )

        self._write_pid(agent_id, proc.pid)
        with self._lock:
            self.agents[agent_id] = agent
        return agent

    def stop(self, agent_id: str) -> bool:
        """Stop a specific agent by ID."""
        with self._lock:
            agent = self.agents.get(agent_id)
            if not agent:
                return False

        # Kill process and wait to avoid zombies
        if agent.pid and self._is_process_alive(agent.pid):
            try:
                pgid = os.getpgid(agent.pid)
                os.killpg(pgid, signal.SIGTERM)
                # Wait for clean exit
                for _ in range(10):
                    time.sleep(0.5)
                    if not self._is_process_alive(agent.pid):
                        break
                else:
                    os.killpg(pgid, signal.SIGKILL)
                # Reap zombie
                try:
                    os.waitpid(agent.pid, os.WNOHANG)
                except ChildProcessError:
                    pass
            except (ProcessLookupError, PermissionError):
                pass

        # Kill tmux session for claude
        if agent.agent_type == "claude":
            for s in _tmux_sessions():
                if agent.id in s or "agentchattr-claude" in s:
                    try:
                        subprocess.run(["tmux", "kill-session", "-t", s],
                                       capture_output=True, timeout=5)
                    except Exception:
                        pass

        self._remove_pid(agent_id)
        with self._lock:
            agent.status = "stopped"
            agent.pid = None
        return True

    def stop_all(self):
        """Stop all agents."""
        with self._lock:
            ids = list(self.agents.keys())
        for agent_id in ids:
            self.stop(agent_id)
        # Also kill any orphaned wrapper processes
        for pattern in ["wrapper_opencode.py", "wrapper_api.py"]:
            try:
                subprocess.run(["pkill", "-f", pattern],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        # Clear queues
        data_dir = ROOT / "data"
        if data_dir.exists():
            for qf in data_dir.glob("*_queue.jsonl"):
                qf.write_text("")

    def get_status(self) -> list[dict]:
        """Get status of all agents."""
        result = []
        with self._lock:
            for agent in self.agents.values():
                # Check if process is still alive
                alive = agent.pid and self._is_process_alive(agent.pid)
                if agent.status == "running" and not alive:
                    agent.status = "dead"

                result.append({
                    "id": agent.id,
                    "type": agent.agent_type,
                    "model": agent.model,
                    "label": agent.label,
                    "project": agent.project,
                    "channel": agent.channel,
                    "cwd": agent.cwd,
                    "pid": agent.pid,
                    "registered_name": agent.registered_name,
                    "status": agent.status,
                    "restarts": agent.restarts,
                    "uptime": int(time.time() - agent.started_at) if agent.started_at else 0,
                    "last_error": agent.last_error,
                    "alive": alive,
                })
        return result

    def restart_dead(self):
        """Restart agents that have died. Safe against race conditions."""
        with self._lock:
            dead = [a for a in self.agents.values()
                    if a.status == "dead" and a.restarts < 5]

        for agent in dead:
            # Double-check status (another thread might have fixed it)
            with self._lock:
                current = self.agents.get(agent.id)
                if not current or current.status != "dead":
                    continue

            prev_restarts = agent.restarts
            self.stop(agent.id)
            with self._lock:
                if agent.id in self.agents:
                    del self.agents[agent.id]

            new = None
            if agent.agent_type == "opencode":
                new = self.start_opencode(
                    agent.model, agent.label, agent.cwd,
                    agent.project, agent.channel
                )
            elif agent.agent_type == "claude":
                new = self.start_claude(agent.cwd, agent.project, agent.channel)
            elif agent.agent_type == "api":
                new = self.start_api(agent.model, agent.project)

            if new:
                with self._lock:
                    new.restarts = prev_restarts + 1
                logger.info("Restarted %s as %s (attempt %d)", agent.id, new.id, new.restarts)

    def _find_registered_name(self, agent_id: str, label_hint: str) -> Optional[str]:
        """Find the registered name in AgentChattr by checking status API."""
        try:
            token = _get_chat_token()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.chat_port}/api/status?token={token}",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            for name, info in data.items():
                if isinstance(info, dict) and info.get("available"):
                    if info.get("label", "") == label_hint:
                        return name
        except Exception:
            pass
        return None

    def inject_trigger(self, agent: AgentInstance, message: str):
        """Inject a trigger message into an agent's queue."""
        queue_name = agent.registered_name or "opencode"
        queue_file = ROOT / "data" / f"{queue_name}_queue.jsonl"
        trigger = json.dumps({
            "sender": "orchestrator",
            "text": message,
            "time": time.strftime("%H:%M:%S"),
            "channel": agent.channel,
        })
        with open(queue_file, "a") as f:
            f.write(trigger + "\n")


def _tmux_sessions() -> list[str]:
    try:
        r = subprocess.run(["tmux", "ls"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return [line.split(":")[0].strip() for line in r.stdout.strip().split("\n")]
    except Exception:
        pass
    return []


def _get_chat_token() -> str:
    """Read the current chat server session token from the log."""
    try:
        log = Path("/tmp/agentchattr-server.log")
        if log.exists():
            for line in reversed(log.read_text().split("\n")):
                if "Session token:" in line:
                    return line.split("Session token:")[-1].strip()
    except Exception:
        pass
    return ""
