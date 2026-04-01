"""Agent Manager API — start/stop agents, select projects, assign roles from the web UI."""

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
VENV_PYTHON = str(ROOT / ".venv" / "bin" / "python")
AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"

# Auto-approve flags per CLI
AUTO_APPROVE = {
    "claude": ["--dangerously-skip-permissions"],
    "codex": ["--", "--dangerously-bypass-approvals-and-sandbox"],
    "gemini": ["--", "--yolo"],
    "qwen": ["--yolo"],
    "kimi": [],
    "opencode": [],
}


def get_opencode_models():
    """Get all available OpenCode provider/model combinations."""
    try:
        r = subprocess.run(["opencode", "models"], capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return []
        models = []
        for line in r.stdout.strip().split("\n"):
            line = line.strip()
            if "/" in line and line:
                provider, model = line.split("/", 1)
                models.append({
                    "id": line,
                    "provider": provider,
                    "model": model,
                })
        return models
    except Exception:
        return []


# Cache opencode models (expensive call)
_opencode_models_cache = None
_opencode_models_time = 0


def get_opencode_models_cached():
    global _opencode_models_cache, _opencode_models_time
    if _opencode_models_cache is None or time.time() - _opencode_models_time > 300:
        _opencode_models_cache = get_opencode_models()
        _opencode_models_time = time.time()
    return _opencode_models_cache

# Predefined strategies
STRATEGIES = {
    "build-review": {
        "name": "Build + Review",
        "description": "One agent builds, another reviews",
        "agents": ["claude", "codex"],
        "roles": {"claude": "Builder", "codex": "Reviewer"},
    },
    "research-build-review": {
        "name": "Research → Build → Review",
        "description": "Research first, then implement, then review",
        "agents": ["gemini", "claude", "codex"],
        "roles": {"gemini": "Researcher", "claude": "Builder", "codex": "Reviewer"},
    },
    "full-team": {
        "name": "Full Team",
        "description": "All available agents with specialized roles",
        "agents": ["claude", "codex", "gemini", "minimax", "glm"],
        "roles": {
            "claude": "Builder",
            "codex": "Reviewer",
            "gemini": "Researcher",
            "minimax": "Critic",
            "glm": "Architect",
        },
    },
    "adversarial-review": {
        "name": "Adversarial Review",
        "description": "Multiple reviewers challenge each other",
        "agents": ["claude", "minimax", "glm"],
        "roles": {"claude": "Builder", "minimax": "Critic", "glm": "Devil's Advocate"},
    },
    "quick-api": {
        "name": "Quick API Only",
        "description": "Lightweight — API agents only, no terminal overhead",
        "agents": ["minimax", "glm"],
        "roles": {"minimax": "Assistant", "glm": "Reviewer"},
    },
    "dashscope-team": {
        "name": "DashScope Multi-Model",
        "description": "4 different models via Alibaba — maximum diversity, zero terminal overhead",
        "agents": ["qwen-api", "qwen-coder", "kimi-api", "glm5-ds"],
        "roles": {
            "qwen-api": "Architect",
            "qwen-coder": "Builder",
            "kimi-api": "Researcher",
            "glm5-ds": "Reviewer",
        },
    },
    "max-resilience": {
        "name": "Maximum Resilience",
        "description": "Claude builds + 3 API backups ready for failover",
        "agents": ["claude", "qwen-coder", "glm", "minimax"],
        "roles": {
            "claude": "Builder",
            "qwen-coder": "Builder",
            "glm": "Reviewer",
            "minimax": "Critic",
        },
    },
}


def _load_api_keys():
    """Load API keys from OpenCode auth.json."""
    env = {}
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
            if "minimax" in data and "key" in data["minimax"]:
                env["MINIMAX_API_KEY"] = data["minimax"]["key"]
            if "zai-coding-plan" in data and "key" in data["zai-coding-plan"]:
                env["ZHIPU_API_KEY"] = data["zai-coding-plan"]["key"]
            if "alibaba-coding-plan" in data and "key" in data["alibaba-coding-plan"]:
                env["DASHSCOPE_API_KEY"] = data["alibaba-coding-plan"]["key"]
        except Exception:
            pass
    return env


def detect_clis():
    """Detect which CLIs and API agents are available.

    Dynamically reads all agents from config (including config.local.toml)
    so any new agent added to config is automatically detected.
    """
    from config_loader import load_config
    config = load_config(ROOT)
    all_agents = config.get("agents", {})
    api_keys = _load_api_keys()
    # Also check env vars directly
    for var in ["DASHSCOPE_API_KEY", "MINIMAX_API_KEY", "ZHIPU_API_KEY"]:
        if os.environ.get(var):
            api_keys[var] = os.environ[var]

    clis = {}
    for name, cfg in all_agents.items():
        agent_type = cfg.get("type", "terminal")
        if agent_type == "api":
            key_env = cfg.get("api_key_env", "")
            has_key = bool(api_keys.get(key_env)) if key_env else True
            clis[name] = {
                "installed": has_key,
                "path": "API",
                "type": "api",
                "model": cfg.get("model", ""),
                "label": cfg.get("label", name),
            }
        else:
            path = shutil.which(cfg.get("command", name))
            clis[name] = {
                "installed": path is not None,
                "path": path or "",
                "type": "terminal",
                "model": "",
                "label": cfg.get("label", name),
            }
    return clis


def detect_projects():
    """Find likely project directories."""
    projects = []
    search_dirs = [
        Path.home(),
        Path("/mnt/c/Users/tango/Desktop"),
    ]
    for base in search_dirs:
        if not base.exists():
            continue
        for item in sorted(base.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            # Check for common project markers
            markers = [".git", "package.json", "Cargo.toml", "pyproject.toml",
                       "go.mod", "Makefile", "docker-compose.yml", "Dockerfile",
                       "CLAUDE.md", ".env"]
            if any((item / m).exists() for m in markers):
                projects.append({
                    "path": str(item),
                    "name": item.name,
                    "markers": [m for m in markers if (item / m).exists()],
                })
    return projects


def _tmux_session_exists(name):
    try:
        r = subprocess.run(["tmux", "has-session", "-t", name],
                           capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def _get_running_agents():
    """Get list of currently running agent processes."""
    running = {}
    # Check tmux sessions for terminal agents
    try:
        r = subprocess.run(["tmux", "ls"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n"):
                # Match ac-{name}: or agentchattr-{name}:
                session_name = line.split(":")[0].strip()
                if session_name.startswith("ac-"):
                    agent = session_name[3:]
                    running[agent] = "terminal"
                elif session_name.startswith("agentchattr-"):
                    agent = session_name[12:]
                    if agent not in running:
                        running[agent] = "terminal"
    except Exception:
        pass
    # Check background processes for API agents
    try:
        r = subprocess.run(["pgrep", "-af", "wrapper_api.py"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n"):
                # Extract agent name from "wrapper_api.py {name}"
                parts = line.strip().split()
                for i, p in enumerate(parts):
                    if p.endswith("wrapper_api.py") and i + 1 < len(parts):
                        running[parts[i + 1]] = "api"
    except Exception:
        pass
    return running


def start_agent(agent_name, project_path=None, model=None):
    """Start a single agent.

    For opencode with a model, the wrapper is called as:
        python wrapper.py opencode -m provider/model
    AgentChattr auto-names instances: opencode, opencode-2, opencode-3...
    Each gets its own tmux session keyed by a unique name.
    """
    from config_loader import load_config
    config = load_config(ROOT)

    # Determine if this is an opencode launch with a specific model
    is_opencode_with_model = model and ("opencode" in agent_name or agent_name.startswith("opencode"))
    config_agent = "opencode" if is_opencode_with_model else agent_name

    agent_cfg = config.get("agents", {}).get(config_agent)
    if not agent_cfg:
        return {"error": f"Unknown agent: {config_agent}"}

    agent_type = agent_cfg.get("type", "terminal")
    env = dict(os.environ)
    env.update(_load_api_keys())

    if agent_type == "api":
        cmd = [VENV_PYTHON, str(ROOT / "wrapper_api.py"), agent_name]
        log = f"/tmp/ac-{agent_name}.log"
        with open(log, "w") as f:
            subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=f, stderr=f)
        return {"status": "started", "type": "api", "log": log}
    else:
        # Unique session name for each launch — always auto-increment
        if is_opencode_with_model:
            # Find next free session number
            n = 1
            while _tmux_session_exists(f"ac-oc-{n}"):
                n += 1
            session = f"ac-oc-{n}"
        else:
            session = f"ac-{agent_name}"
            if _tmux_session_exists(session):
                return {"status": "already_running", "session": session}

        base_cmd = agent_cfg.get("command", config_agent)
        flags = " ".join(AUTO_APPROVE.get(base_cmd, []))

        # Build wrapper command
        if is_opencode_with_model:
            # Use dedicated opencode wrapper (non-interactive, no tmux nesting issues)
            provider_name = model.split("/")[0]
            model_name = model.split("/")[-1]
            label = f"OC [{provider_name}] {model_name}"
            cwd_path = project_path or str((ROOT / "..").resolve())
            wrapper_cmd = (
                f"cd {ROOT} && source .venv/bin/activate && "
                f'python wrapper_opencode.py -m {model} --label "{label}" --cwd "{cwd_path}"'
            )
        else:
            flags = " ".join(AUTO_APPROVE.get(base_cmd, []))
            wrapper_cmd = (
                f"cd {ROOT} && source .venv/bin/activate && "
                f"python wrapper.py {config_agent} {flags}"
            )

        # Remove TMUX var to allow nesting when manager runs inside tmux
        tmux_env = {k: v for k, v in env.items() if k != "TMUX"}
        subprocess.run([
            "tmux", "new-session", "-d", "-s", session, "-c", str(ROOT),
            "bash", "-lc", wrapper_cmd
        ], env=tmux_env, timeout=10)

        # Auto-confirm trust prompts
        def _auto_trust():
            time.sleep(5)
            # The wrapper creates agentchattr-{registered_name} tmux sessions
            # For multi-instance, names are opencode, opencode-2, opencode-3...
            try:
                r = subprocess.run(["tmux", "ls"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0:
                    for line in r.stdout.strip().split("\n"):
                        sname = line.split(":")[0].strip()
                        if sname.startswith("agentchattr-opencode"):
                            for _ in range(3):
                                try:
                                    subprocess.run(["tmux", "send-keys", "-t", sname, "Enter"], timeout=5)
                                except Exception:
                                    pass
                                time.sleep(2)
            except Exception:
                pass
        threading.Thread(target=_auto_trust, daemon=True).start()

        return {"status": "started", "type": "terminal", "session": session, "model": model}


def stop_agent(agent_name):
    """Stop a single agent."""
    stopped = False
    # Kill tmux sessions
    for prefix in [f"ac-{agent_name}", f"agentchattr-{agent_name}"]:
        try:
            subprocess.run(["tmux", "kill-session", "-t", prefix], capture_output=True, timeout=5)
            stopped = True
        except Exception:
            pass
    # Kill API wrapper
    try:
        r = subprocess.run(["pgrep", "-af", f"wrapper_api.py {agent_name}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            for line in r.stdout.strip().split("\n"):
                pid = line.split()[0]
                subprocess.run(["kill", pid], timeout=5)
                stopped = True
    except Exception:
        pass
    return {"status": "stopped" if stopped else "not_running"}


def nuke_all():
    """Kill ALL agent processes, tmux sessions, queues. Full reset."""
    killed = []

    # 1. Kill tmux server (kills ALL sessions at once)
    try:
        subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=5)
        killed.append("tmux-server")
    except Exception:
        pass

    # 2. Kill all wrapper processes
    for pattern in ["wrapper.py", "wrapper_api.py"]:
        try:
            subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=5)
            killed.append(pattern)
        except Exception:
            pass

    # 3. Kill orphaned opencode/codex/gemini processes spawned by wrappers
    for pattern in ["opencode", "codex --", "gemini"]:
        try:
            subprocess.run(["pkill", "-f", pattern], capture_output=True, timeout=5)
        except Exception:
            pass

    time.sleep(2)

    # 4. Clear all queue files
    data_dir = ROOT / "data"
    cleared = 0
    if data_dir.exists():
        for qf in data_dir.glob("*_queue.jsonl"):
            qf.write_text("")
            cleared += 1

    return {"status": "nuked", "killed": killed, "queues_cleared": cleared}


def start_strategy(strategy_key, project_path=None):
    """Start a predefined strategy (set of agents with roles)."""
    strategy = STRATEGIES.get(strategy_key)
    if not strategy:
        return {"error": f"Unknown strategy: {strategy_key}"}

    results = {}
    for agent_name in strategy["agents"]:
        results[agent_name] = start_agent(agent_name, project_path)

    return {
        "strategy": strategy_key,
        "agents": results,
        "roles": strategy.get("roles", {}),
    }
