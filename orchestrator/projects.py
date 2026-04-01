"""Project Manager — handles multi-project agent assignments."""

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Project:
    name: str
    path: str
    channel: str  # AgentChattr channel for this project
    agents: list[str] = field(default_factory=list)  # Agent IDs assigned


def detect_projects() -> list[dict]:
    """Find likely project directories."""
    projects = []
    search_dirs = [
        Path.home(),
        Path("/mnt/c/Users/tango/Desktop"),
    ]
    markers = [".git", "package.json", "Cargo.toml", "pyproject.toml",
               "go.mod", "Makefile", "docker-compose.yml", "Dockerfile",
               "CLAUDE.md", ".env"]

    for base in search_dirs:
        if not base.exists():
            continue
        for item in sorted(base.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            found = [m for m in markers if (item / m).exists()]
            if found:
                projects.append({
                    "path": str(item),
                    "name": item.name,
                    "markers": found,
                })
    return projects


def get_opencode_models() -> list[dict]:
    """Get all available OpenCode provider/model combinations."""
    import subprocess
    try:
        r = subprocess.run(["opencode", "models"],
                           capture_output=True, text=True, timeout=15)
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


# Cache (expensive call)
_models_cache = None
_models_time = 0


def get_opencode_models_cached() -> list[dict]:
    global _models_cache, _models_time
    import time
    if _models_cache is None or time.time() - _models_time > 300:
        _models_cache = get_opencode_models()
        _models_time = time.time()
    return _models_cache
