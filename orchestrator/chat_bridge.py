"""Chat Bridge — reliable communication with AgentChattr server.

Handles:
- Channel creation (via /api/channels REST endpoint)
- Message sending (via agent token or session token)
- Status queries (with retry and token refresh)
- Server lifecycle (start/stop/health check)
"""

import json
import logging
import subprocess
import time
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
SERVER_LOG = Path("/tmp/agentchattr-server.log")
CHAT_PORT = 8300


def room_settings_get() -> dict:
    """Read current room settings from settings.json."""
    settings_file = ROOT / "data" / "settings.json"
    if settings_file.exists():
        try:
            return json.loads(settings_file.read_text())
        except Exception:
            pass
    return {"channels": ["general"]}


class ChatBridge:
    """Reliable bridge to AgentChattr server."""

    def __init__(self, port: int = 8300):
        self.port = port
        self._server_proc = None
        self._session_token = ""

    # --- Server Lifecycle ---

    def is_server_running(self) -> bool:
        try:
            r = subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=3)
            return f":{self.port} " in r.stdout
        except Exception:
            return False

    def start_server(self) -> bool:
        """Start AgentChattr server as subprocess."""
        if self.is_server_running():
            self._refresh_token()
            return True

        import os
        env = {**os.environ, "DASHSCOPE_API_KEY": os.environ.get("DASHSCOPE_API_KEY", "")}

        cmd = f'cd {ROOT} && echo "YES" | .venv/bin/python run.py --allow-network'
        self._server_proc = subprocess.Popen(
            ["bash", "-c", cmd],
            stdout=open(str(SERVER_LOG), "w"),
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=lambda: __import__("os").setpgrp(),
        )

        # Wait for server to bind
        for _ in range(30):
            time.sleep(1)
            if self.is_server_running():
                self._refresh_token()
                logger.info("Chat server started on :%d (token: %s...)", self.port, self._session_token[:8])
                return True

        logger.error("Chat server failed to start within 30s")
        return False

    def stop_server(self):
        """Stop AgentChattr server."""
        if self._server_proc:
            try:
                import os, signal
                os.killpg(os.getpgid(self._server_proc.pid), signal.SIGTERM)
                self._server_proc.wait(timeout=5)
            except Exception:
                pass
            self._server_proc = None

        # Also kill by port
        try:
            r = subprocess.run(["lsof", "-t", f"-i:{self.port}"], capture_output=True, text=True, timeout=3)
            for pid in r.stdout.strip().split("\n"):
                if pid.strip():
                    subprocess.run(["kill", "-9", pid.strip()], timeout=3)
        except Exception:
            pass

    def restart_server(self) -> bool:
        """Restart server and refresh token."""
        self.stop_server()
        time.sleep(2)
        return self.start_server()

    # --- Token Management ---

    def _refresh_token(self):
        """Read current session token from server log."""
        try:
            if SERVER_LOG.exists():
                for line in reversed(SERVER_LOG.read_text().split("\n")):
                    if "Session token:" in line:
                        self._session_token = line.split("Session token:")[-1].strip()
                        return
        except Exception:
            pass

    def get_token(self) -> str:
        if not self._session_token:
            self._refresh_token()
        return self._session_token

    # --- Channel Management ---

    def create_channel(self, channel_name: str, max_retries: int = 3) -> bool:
        """Create a channel via the /api/channels REST endpoint.

        This is the proper way to create channels — it goes through the running
        AgentChattr server which updates its in-memory room_settings and
        broadcasts the change to all WebSocket clients.
        """
        import re

        # Validate channel name
        if not re.match(r'^[a-z0-9][a-z0-9\-]{0,19}$', channel_name):
            logger.error("Invalid channel name: %s", channel_name)
            return False

        for attempt in range(max_retries):
            try:
                token = self.get_token()
                body = json.dumps({"name": channel_name, "token": token}).encode()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/api/channels?token={token}",
                    method="POST",
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())

                if data.get("created") or data.get("exists"):
                    logger.info("Channel #%s %s", channel_name,
                                "created" if data.get("created") else "already exists")
                    return True

            except urllib.error.HTTPError as e:
                body_text = ""
                try:
                    body_text = e.read().decode()
                except Exception:
                    pass
                logger.warning("Attempt %d: HTTP %d creating channel #%s: %s",
                               attempt + 1, e.code, channel_name, body_text)
                if e.code == 403:
                    self._refresh_token()
                    continue
                if e.code == 400:
                    return False  # Bad request won't fix on retry
            except Exception as e:
                logger.warning("Attempt %d: Failed to create channel #%s: %s",
                               attempt + 1, channel_name, e)
                if attempt < max_retries - 1:
                    self._refresh_token()
                    time.sleep(1)

        logger.error("Failed to create channel #%s after %d attempts", channel_name, max_retries)
        return False

    def verify_channel_exists(self, channel_name: str) -> bool:
        """Check if a channel exists via the settings API."""
        try:
            token = self.get_token()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/settings?token={token}",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                settings = json.loads(resp.read())
            channels = settings.get("channels", ["general"])
            exists = channel_name in channels
            logger.info("Channel #%s exists: %s (channels=%s)", channel_name, exists, channels)
            return exists
        except Exception as e:
            logger.warning("Could not verify channel #%s: %s", channel_name, e)
            # Fallback: check file directly
            file_channels = room_settings_get().get("channels", ["general"])
            return channel_name in file_channels

    def delete_channel(self, channel_name: str) -> bool:
        """Delete a channel via the REST API. Only non-'general' channels."""
        if channel_name == "general":
            return False
        try:
            token = self.get_token()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/channels/{channel_name}?token={token}",
                method="DELETE",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            deleted = data.get("deleted", False)
            if deleted:
                logger.info("Channel #%s deleted", channel_name)
            return deleted
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return True  # Already gone
            logger.warning("Failed to delete channel #%s: HTTP %d", channel_name, e.code)
            return False
        except Exception as e:
            logger.warning("Failed to delete channel #%s: %s", channel_name, e)
            return False

    # --- Message Queries ---

    def send_message(self, sender: str, text: str, channel: str = "general") -> bool:
        """Post a message to a channel via the /api/inject endpoint.

        Uses session token auth. The inject endpoint allows posting as
        any sender, which is needed for swarm orchestrator messages.
        """
        try:
            token = self.get_token()
            body = json.dumps({
                "sender": sender,
                "text": text,
                "channel": channel,
            }).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/inject?token={token}",
                method="POST",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                logger.info("Message posted to #%s as %s (%d chars)", channel, sender, len(text))
                return bool(data)
        except Exception as e:
            logger.warning("send_message failed: %s", e)
            return False

    def get_status(self) -> dict:
        """Get agent status from server, with token refresh on failure."""
        for attempt in range(3):
            try:
                token = self.get_token()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/api/status?token={token}",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read())
                    # Add channels from room_settings for convenience
                    data["channels"] = room_settings_get().get("channels", ["general"])
                    return data
            except urllib.error.HTTPError as e:
                if e.code == 403:
                    self._refresh_token()
                    continue
                raise
            except Exception as e:
                logger.warning("get_status attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(1)
                    self._refresh_token()
        return {}

    def get_messages(self, channel: str = "general", limit: int = 20) -> list:
        """Get recent messages from a channel."""
        try:
            token = self.get_token()
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/messages?channel={channel}&limit={limit}&token={token}",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            logger.warning("Failed to get messages: %s", e)
            return []

    def find_agent_by_label(self, label: str) -> str | None:
        """Find registered agent name by label, with retry."""
        for attempt in range(15):
            status = self.get_status()
            for name, info in status.items():
                if isinstance(info, dict) and info.get("available"):
                    if info.get("label", "") == label:
                        return name
            time.sleep(2)
        return None

    def wait_for_agents(self, labels: list[str], timeout: int = 60) -> dict[str, str]:
        """Wait for multiple agents to register. Returns {label: registered_name}."""
        found = {}
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_status()
            for name, info in status.items():
                if isinstance(info, dict) and info.get("available"):
                    lbl = info.get("label", "")
                    if lbl in labels and lbl not in found:
                        found[lbl] = name
                        logger.info("Agent registered: @%s (%s)", name, lbl)
            if len(found) == len(labels):
                return found
            time.sleep(2)

        missing = [l for l in labels if l not in found]
        if missing:
            logger.warning("Timeout: agents not registered: %s", missing)
        return found
