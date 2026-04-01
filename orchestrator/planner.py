"""KI Planner — analyzes a project and generates swarm configuration with role assignments.

Uses direct API calls instead of opencode run (which hangs in subprocess).
Returns tasks with role tags and model recommendations per role.
"""

import json
import os
import logging
import subprocess
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"

ANALYSIS_PROMPT = """Analyze this project and create a work plan with role assignments.

PROJECT PATH: {project_path}
PROJECT FILES:
{file_tree}

USER REQUEST: {user_request}

Available models: qwen3-coder-next (strong coding), glm-5.1 (strong reasoning/coordination), MiniMax-M2.7 (fast).

Reply with ONLY valid JSON (no markdown, no backticks):
{{"summary":"1-2 sentence project description","state":"current state","tasks":[{{"p":1,"t":"task title","d":"description","role":"Builder"}}],"roles":{{"Builder":{{"model":"qwen3-coder-next","why":"best for code"}},"Reviewer":{{"model":"glm-5.1","why":"strong analysis"}}}},"start":"first instruction for orchestrator"}}

Roles: Builder (write code), Reviewer (review code, find bugs), Architect (design, plan structure), Tester (write tests), Analyst (analyze, document).
Assign exactly 1 role per task. Use 2-3 different roles. Each role gets its own worker agent.

Max 8 tasks. JSON only."""

# Default role-to-model mapping (used when KI doesn't return roles)
DEFAULT_ROLE_MODELS = {
    "Builder": "alibaba-coding-plan/qwen3-coder-next",
    "Reviewer": "zai-coding-plan/glm-5.1",
    "Architect": "zai-coding-plan/glm-5.1",
    "Tester": "alibaba-coding-plan/qwen3-coder-next",
    "Analyst": "zai-coding-plan/glm-5.1",
}


def _default_model_for_role(role: str) -> str:
    """Get the default model for a given role."""
    return DEFAULT_ROLE_MODELS.get(role, "alibaba-coding-plan/qwen3-coder-next")


def _get_file_tree(project_path: str, max_depth: int = 2) -> str:
    """Get a simple file tree for context."""
    try:
        result = subprocess.run(
            ["find", project_path, "-maxdepth", str(max_depth),
             "-not", "-path", "*/node_modules/*",
             "-not", "-path", "*/.git/*",
             "-not", "-path", "*/vendor/*",
             "-not", "-path", "*/__pycache__/*"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) > 50:
            lines = lines[:50] + [f"... ({len(lines) - 50} more files)"]
        return "\n".join(lines)
    except Exception:
        return "(could not list files)"


def _get_api_key(provider: str = "zai-coding-plan") -> tuple[str, str]:
    """Get API key and base URL for a provider."""
    key_map = {
        "zai-coding-plan": ("ZHIPU_API_KEY", "https://api.z.ai/api/coding/paas/v4"),
        "alibaba-coding-plan": ("DASHSCOPE_API_KEY", "https://coding-intl.dashscope.aliyuncs.com/v1"),
        "minimax": ("MINIMAX_API_KEY", "https://api.minimax.io/v1"),
    }
    env_var, base_url = key_map.get(provider, key_map["zai-coding-plan"])

    key = os.environ.get(env_var, "")
    if not key and AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text())
            key = data.get(provider, {}).get("key", "")
        except Exception:
            pass
    return key, base_url


def _model_from_string(model_str: str) -> tuple[str, str]:
    """Extract provider and model name from 'provider/model' string."""
    if "/" in model_str:
        provider, model = model_str.split("/", 1)
        return provider, model
    return "zai-coding-plan", model_str


def _parse_roles(raw_roles: dict) -> dict:
    """Parse roles from KI response into {role_name: {model, why}} format."""
    roles = {}
    for role_name, role_info in raw_roles.items():
        if isinstance(role_info, dict):
            model = role_info.get("model", _default_model_for_role(role_name))
            # Add provider prefix if missing
            if "/" not in model:
                model = f"alibaba-coding-plan/{model}"
            roles[role_name] = {"model": model, "why": role_info.get("why", "")}
        elif isinstance(role_info, str):
            model = role_info
            if "/" not in model:
                model = f"alibaba-coding-plan/{model}"
            roles[role_name] = {"model": model, "why": ""}
    return roles


def analyze_project(project_path: str, user_request: str,
                    model: str = "zai-coding-plan/glm-5.1") -> dict:
    """Analyze a project using direct API call. Returns tasks with roles."""
    provider, model_name = _model_from_string(model)
    api_key, base_url = _get_api_key(provider)

    if not api_key:
        return _fallback(user_request, f"No API key for {provider}")

    file_tree = _get_file_tree(project_path)

    # Also try to read README
    readme = ""
    for name in ["README.md", "readme.md", "README"]:
        readme_path = Path(project_path) / name
        if readme_path.exists():
            try:
                readme = readme_path.read_text("utf-8")[:2000]
            except Exception:
                pass
            break

    prompt = ANALYSIS_PROMPT.format(
        project_path=project_path,
        file_tree=file_tree[:3000],
        user_request=user_request,
    )

    if readme:
        prompt += f"\n\nREADME (first 2000 chars):\n{readme}"

    logger.info("Analyzing %s via %s/%s API", project_path, provider, model_name)

    try:
        body = json.dumps({
            "model": model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 2000,
        }).encode()

        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            method="POST",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )

        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())

        content = data["choices"][0]["message"]["content"].strip()

        # Strip thinking tags if present
        import re
        content = re.sub(r'<!DOCTYPE[\s\S]*?-->', '', content).strip()
        content = re.sub(r'```json\s*', '', content)
        content = re.sub(r'```\s*', '', content)

        logger.info("API response: %d chars", len(content))

        # Parse JSON
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            plan = json.loads(json_match.group())

            # Parse roles from KI response
            raw_roles = plan.get("roles", {})
            roles = _parse_roles(raw_roles) if raw_roles else {}

            # Parse tasks with role field
            tasks = []
            for t in plan.get("tasks", []):
                role = t.get("role", "Builder")
                tasks.append({
                    "priority": t.get("p", t.get("priority", 0)),
                    "title": t.get("t", t.get("title", "")),
                    "description": t.get("d", t.get("description", "")),
                    "role": role,
                })
                # Auto-add role if KI returned tasks with roles but no roles dict
                if role not in roles:
                    roles[role] = {"model": _default_model_for_role(role), "why": "auto-assigned"}

            normalized = {
                "project_summary": plan.get("summary", plan.get("project_summary", "")),
                "current_state": plan.get("state", plan.get("current_state", "")),
                "tasks": tasks,
                "roles": roles,
                "initial_orchestrator_prompt": plan.get("start", plan.get("initial_orchestrator_prompt", user_request)),
            }

            # Fallback if no roles at all
            if not roles and tasks:
                roles["Builder"] = {"model": _default_model_for_role("Builder"), "why": "default"}
                normalized["roles"] = roles

            logger.info("Analysis complete: %d tasks, %d roles (%s)",
                        len(tasks), len(roles), ", ".join(roles.keys()))
            return normalized

        # Free-text fallback
        return {
            "project_summary": "KI-Analyse (Freitext)",
            "current_state": content[:500],
            "tasks": [{"priority": 1, "title": "Aufgabe", "description": user_request, "role": "Builder"}],
            "roles": {"Builder": {"model": _default_model_for_role("Builder"), "why": "fallback"}},
            "initial_orchestrator_prompt": user_request,
        }

    except Exception as e:
        logger.error("API call failed: %s", e)
        return _fallback(user_request, str(e))


def _fallback(user_request: str, error: str) -> dict:
    return {
        "project_summary": f"Analyse fehlgeschlagen: {error}",
        "current_state": "Konnte nicht analysiert werden",
        "tasks": [{"priority": 1, "title": "Manuelle Analyse", "description": user_request, "role": "Builder"}],
        "roles": {"Builder": {"model": _default_model_for_role("Builder"), "why": "fallback"}},
        "initial_orchestrator_prompt": user_request,
        "error": error,
    }
