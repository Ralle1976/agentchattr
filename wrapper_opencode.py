"""OpenCode wrapper — uses 'opencode run' non-interactively.

Unlike the standard wrapper (which injects keystrokes into a TUI),
this wrapper calls 'opencode run -m provider/model "message"' as a subprocess.
OpenCode run has full CLI capabilities: file access, MCP tools, code editing.

Usage:
    python wrapper_opencode.py --model zai-coding-plan/glm-5.1
    python wrapper_opencode.py --model alibaba-coding-plan/qwen3.5-plus --label "Qwen Builder"
    python wrapper_opencode.py --model minimax/MiniMax-M2.7 --cwd ~/qr-info-portal
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent


def _auth_headers(token, include_json=False):
    headers = {"Authorization": f"Bearer {token}"}
    if include_json:
        headers["Content-Type"] = "application/json"
    return headers


def main():
    parser = argparse.ArgumentParser(description="OpenCode wrapper for AgentChattr")
    parser.add_argument("--model", "-m", required=True, help="provider/model (e.g. zai-coding-plan/glm-5.1)")
    parser.add_argument("--label", default=None, help="Display label in chat")
    parser.add_argument("--cwd", default=None, help="Working directory for opencode")
    parser.add_argument("--channel", default="general", help="Chat channel to operate in")
    args = parser.parse_args()

    model = args.model
    model_short = model.split("/")[-1]
    provider = model.split("/")[0]
    label = args.label or f"OC:{model_short}"
    cwd = args.cwd or str(ROOT / "..")
    default_channel = args.channel

    # Verify opencode is installed
    if not shutil.which("opencode"):
        print("  Error: 'opencode' not found on PATH.")
        sys.exit(1)

    # Load config
    sys.path.insert(0, str(ROOT))
    from config_loader import load_config
    config = load_config(ROOT)
    server_port = config.get("server", {}).get("port", 8300)
    data_dir = ROOT / config.get("server", {}).get("data_dir", "./data")
    data_dir.mkdir(parents=True, exist_ok=True)

    # Register with server
    from wrapper import _register_instance
    try:
        registration = _register_instance(server_port, "opencode", label)
    except Exception as exc:
        print(f"  Registration failed ({exc}). Is the server running?")
        sys.exit(1)

    name = registration["name"]
    token = registration["token"]
    print(f"  Registered as: @{name} ({label})")

    _lock = threading.Lock()
    _state = {"name": name, "token": token, "working": False}

    def get_name():
        with _lock:
            return _state["name"]

    def get_token():
        with _lock:
            return _state["token"]

    def set_working(val):
        with _lock:
            _state["working"] = val

    def is_working():
        with _lock:
            return _state["working"]

    # Heartbeat thread
    def _heartbeat():
        while True:
            try:
                n, t = get_name(), get_token()
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server_port}/api/heartbeat/{n}",
                    method="POST",
                    data=json.dumps({"active": is_working()}).encode(),
                    headers=_auth_headers(t, include_json=True),
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    resp_data = json.loads(resp.read())
                server_name = resp_data.get("name", n)
                if server_name != n:
                    with _lock:
                        _state["name"] = server_name
                    print(f"  Identity updated: {n} -> {server_name}")
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    try:
                        replacement = _register_instance(server_port, "opencode", label)
                        with _lock:
                            _state["name"] = replacement["name"]
                            _state["token"] = replacement["token"]
                        print(f"  Re-registered as: {replacement['name']}")
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(5)

    threading.Thread(target=_heartbeat, daemon=True).start()

    # Read recent messages from chat server
    def read_messages(channel="general", limit=15):
        t = get_token()
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/api/messages?limit={limit}&channel={channel}",
            headers=_auth_headers(t),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    # Send message to chat
    def send_message(text, channel="general"):
        t = get_token()
        body = json.dumps({"text": text, "channel": channel}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{server_port}/api/send",
            method="POST", data=body,
            headers=_auth_headers(t, include_json=True),
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())

    # Call opencode run
    def call_opencode(prompt):
        """Run opencode non-interactively and capture output."""
        cmd = ["opencode", "run", "-m", model, prompt]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd,
                env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            )
            # Send periodic "still working" pings while opencode runs
            output = None
            elapsed = 0
            while output is None:
                try:
                    stdout, stderr = proc.communicate(timeout=30)
                    output = stdout
                except subprocess.TimeoutExpired:
                    elapsed += 30
                    set_working(True)  # heartbeat: still alive
                    if elapsed >= 120 and elapsed % 120 == 0:
                        print(f"  ... still working ({elapsed}s elapsed)")
            output = output.decode("utf-8", errors="replace").strip()
            # Strip ANSI escape codes
            output = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)
            output = re.sub(r'\x1b\[[\?]?[0-9;]*[a-zA-Z]', '', output)
            # Strip the "Sisyphus" header line
            lines = output.split("\n")
            clean_lines = []
            skip = True
            for line in lines:
                stripped = line.strip()
                if skip and (not stripped or "Sisyphus" in stripped or stripped.startswith(">") or stripped.startswith("[")):
                    continue
                skip = False
                clean_lines.append(line)
            return "\n".join(clean_lines).strip()
        except subprocess.TimeoutExpired:
            proc.kill()
            return "(timeout after 10 minutes)"
        except Exception as exc:
            return f"(error: {exc})"

    # Build context from chat messages
    def build_prompt(chat_msgs, channel):
        my_name = get_name()
        parts = [
            f"You are {my_name} ({label}), an AI agent in a developer chat room.",
            f"You have full access to the project in {cwd}.",
            "Read/write files, run commands, analyze code — do real work.",
            "Be concise. Show code changes, file paths, concrete results.",
            f"Your name is {my_name}. Don't prefix messages with your name.",
            "",
            "Recent chat context:",
        ]
        for msg in chat_msgs:
            sender = msg.get("sender", "")
            text = msg.get("text", "")
            if sender == "system":
                continue
            parts.append(f"  {sender}: {text}")
        parts.append("")
        parts.append("Respond to the conversation above. Take action if needed.")
        return "\n".join(parts)

    def is_mentioned(chat_msgs, my_name):
        """Check if this agent is @mentioned in recent chat messages."""
        for msg in chat_msgs:
            text = msg.get("text", "")
            if f"@{my_name}" in text:
                return True
        return False

    # Handle trigger
    def handle_trigger(channel="general", override_prompt=None):
        my_name = get_name()
        set_working(True)
        try:
            if override_prompt:
                # Direct prompt from queue (e.g. swarm kickoff) — always execute
                prompt = override_prompt
            else:
                chat_msgs = read_messages(channel=channel, limit=15)
                if not chat_msgs:
                    return
                # @mention filter: only respond if this agent is @mentioned
                # Exception: orchestrator agents (label starts with "Orchestrator")
                # respond to all messages to coordinate the swarm.
                is_orchestrator = label.startswith("Orchestrator")
                if not is_orchestrator and not is_mentioned(chat_msgs, my_name):
                    return
                prompt = build_prompt(chat_msgs, channel)
            print(f"  [{channel}] Calling opencode run -m {model}...")

            response = call_opencode(prompt)
            if not response:
                return

            # Strip self-prefix
            for prefix in [f"{my_name}: ", f"{my_name}:"]:
                if response.startswith(prefix):
                    response = response[len(prefix):]
                    break

            # Truncate very long responses
            if len(response) > 3000:
                response = response[:3000] + "\n\n_(truncated)_"

            send_message(response, channel=channel)
            print(f"  [{channel}] Responded ({len(response)} chars)")
        except Exception as exc:
            print(f"  Error: {exc}")
        finally:
            set_working(False)

    # Queue watcher
    queue_file = data_dir / f"{name}_queue.jsonl"
    if queue_file.exists():
        queue_file.write_text("")

    print(f"\n  === OpenCode Agent Wrapper ===")
    print(f"  Model: {model}")
    print(f"  Label: {label}")
    print(f"  CWD: {cwd}")
    print(f"  Channel: #{default_channel}")
    print(f"  @{name} mentions trigger opencode run")
    print(f"  Ctrl+C to stop\n")

    try:
        while True:
            try:
                current_name = get_name()
                qf = data_dir / f"{current_name}_queue.jsonl"

                if qf.exists() and qf.stat().st_size > 0:
                    with open(qf, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    qf.write_text("")

                    channels_triggered = {}
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            ch = data.get("channel", default_channel) if isinstance(data, dict) else default_channel
                            text = data.get("text", "") if isinstance(data, dict) else ""
                            if ch not in channels_triggered:
                                channels_triggered[ch] = text
                        except json.JSONDecodeError:
                            if default_channel not in channels_triggered:
                                channels_triggered[default_channel] = ""

                    # Force default channel if set (swarm agents stay in their channel)
                    if default_channel != "general":
                        forced = {}
                        for ch, text in channels_triggered.items():
                            forced[default_channel] = text or channels_triggered.get(default_channel, "")
                        channels_triggered = forced or {default_channel: ""}

                    for ch, override_prompt in channels_triggered.items():
                        handle_trigger(channel=ch, override_prompt=override_prompt or None)
            except Exception:
                pass

            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  Shutting down...")
    finally:
        try:
            n, t = get_name(), get_token()
            req = urllib.request.Request(
                f"http://127.0.0.1:{server_port}/api/deregister/{n}",
                method="POST", data=b"",
                headers=_auth_headers(t),
            )
            urllib.request.urlopen(req, timeout=5)
            print(f"  Deregistered {n}")
        except Exception:
            pass

    print("  Wrapper stopped.")


if __name__ == "__main__":
    main()
