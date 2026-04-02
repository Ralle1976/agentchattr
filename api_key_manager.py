"""API Key Manager — read/write API keys for OpenCode auth.json.

Provides:
- Masked key display for UI (show last 4 chars only)
- Save new keys (writes to auth.json + sets env var)
- Test key validity (optional ping to provider API)
- Auto-detect providers from agent configs

Security:
- Keys are NEVER sent to the frontend in full
- Backend reads full keys, sends only masked versions
- Session token required for all endpoints
"""

import json
import os
import logging
from pathlib import Path

log = logging.getLogger(__name__)

AUTH_FILE = Path.home() / ".local" / "share" / "opencode" / "auth.json"

# Well-known provider → env var mapping
PROVIDER_ENV_MAP = {
    "minimax": "MINIMAX_API_KEY",
    "zai-coding-plan": "ZHIPU_API_KEY",
    "alibaba-coding-plan": "DASHSCOPE_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
}


def _read_auth_file() -> dict:
    """Read the OpenCode auth.json file. Returns {} if not found."""
    if not AUTH_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_FILE.read_text("utf-8"))
    except Exception as e:
        log.warning("Failed to read auth file: %s", e)
        return {}


def _write_auth_file(data: dict):
    """Write data to the OpenCode auth.json file."""
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUTH_FILE.write_text(json.dumps(data, indent=2), "utf-8")


def _mask_key(key: str) -> str:
    """Mask API key for UI — show only last 4 chars."""
    if not key or len(key) < 8:
        return "****" if key else ""
    return f"****...{key[-4:]}"


def list_keys() -> list[dict]:
    """List all API keys (masked). Returns [{provider, env_var, masked, has_key}]."""
    auth_data = _read_auth_file()

    # Collect providers from both auth.json and known env vars
    providers = set()
    for provider_name in auth_data:
        if isinstance(auth_data[provider_name], dict) and "key" in auth_data[provider_name]:
            providers.add(provider_name)

    # Also check known providers that might be set via env vars
    for provider_name, env_var in PROVIDER_ENV_MAP.items():
        providers.add(provider_name)

    result = []
    for provider_name in sorted(providers):
        env_var = PROVIDER_ENV_MAP.get(provider_name, f"{provider_name.upper()}_API_KEY")

        # Check auth.json first, then env var
        auth_entry = auth_data.get(provider_name, {})
        key = ""
        source = ""
        if isinstance(auth_entry, dict):
            key = auth_entry.get("key", "")
            source = "auth.json"

        if not key:
            key = os.environ.get(env_var, "")
            source = "env" if key else ""

        result.append({
            "provider": provider_name,
            "env_var": env_var,
            "masked": _mask_key(key),
            "has_key": bool(key),
            "source": source,
        })

    return result


def save_key(provider: str, key: str) -> dict:
    """Save an API key for a provider. Writes to auth.json and sets env var."""
    if not provider or not key:
        return {"error": "provider and key are required"}

    # Validate key format (basic check — no whitespace, min 8 chars)
    key = key.strip()
    if len(key) < 8 or " " in key:
        return {"error": "invalid key format (min 8 chars, no spaces)"}

    # Write to auth.json
    auth_data = _read_auth_file()
    if provider not in auth_data:
        auth_data[provider] = {}
    auth_data[provider]["key"] = key
    _write_auth_file(auth_data)

    # Also set env var for the running process
    env_var = PROVIDER_ENV_MAP.get(provider, f"{provider.upper()}_API_KEY")
    os.environ[env_var] = key

    log.info("API key saved for provider '%s' (env: %s)", provider, env_var)
    return {
        "ok": True,
        "provider": provider,
        "masked": _mask_key(key),
        "env_var": env_var,
    }


def delete_key(provider: str) -> dict:
    """Delete an API key for a provider."""
    if not provider:
        return {"error": "provider is required"}

    auth_data = _read_auth_file()
    if provider in auth_data and isinstance(auth_data[provider], dict):
        auth_data[provider].pop("key", None)
        # Clean up empty provider entries
        if not auth_data[provider]:
            del auth_data[provider]
        _write_auth_file(auth_data)

    # Also unset env var
    env_var = PROVIDER_ENV_MAP.get(provider, f"{provider.upper()}_API_KEY")
    os.environ.pop(env_var, None)

    log.info("API key deleted for provider '%s'", provider)
    return {"ok": True, "provider": provider}


def detect_providers_from_config(config: dict) -> list[dict]:
    """Scan agent configs for api_key_env fields to discover providers.

    Returns [{agent_name, env_var, has_key, source}].
    """
    agents = config.get("agents", {})
    seen_envs = {}
    for name, cfg in agents.items():
        env_var = cfg.get("api_key_env", "")
        if not env_var:
            continue
        if env_var in seen_envs:
            seen_envs[env_var]["agents"].append(name)
            continue

        # Check if key is available
        key = os.environ.get(env_var, "")
        auth_data = _read_auth_file()
        source = ""
        if not key:
            # Try to find in auth.json by matching env var to provider
            for prov, ev in PROVIDER_ENV_MAP.items():
                if ev == env_var and prov in auth_data:
                    entry = auth_data[prov]
                    if isinstance(entry, dict) and entry.get("key"):
                        source = "auth.json"
                        break

        if not source and key:
            source = "env"

        seen_envs[env_var] = {
            "env_var": env_var,
            "agents": [name],
            "has_key": bool(key) or source == "auth.json",
            "source": source,
        }

    return list(seen_envs.values())
