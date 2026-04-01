#!/usr/bin/env sh
# Load API keys for AgentChattr API agents
# Source this file: . ./load_api_keys.sh

AUTH_FILE="$HOME/.local/share/opencode/auth.json"

# From OpenCode auth
if [ -f "$AUTH_FILE" ]; then
    export MINIMAX_API_KEY=$(python3 -c "import json; print(json.load(open('$AUTH_FILE'))['minimax']['key'])" 2>/dev/null)
    export ZHIPU_API_KEY=$(python3 -c "import json; print(json.load(open('$AUTH_FILE'))['zai-coding-plan']['key'])" 2>/dev/null)
    export DASHSCOPE_API_KEY=$(python3 -c "import json; print(json.load(open('$AUTH_FILE')).get('alibaba-coding-plan',{}).get('key',''))" 2>/dev/null)
fi

# Fallback: direct key if not in OpenCode auth
[ -z "$DASHSCOPE_API_KEY" ] && echo "Warning: DASHSCOPE_API_KEY not set"

for key in MINIMAX_API_KEY ZHIPU_API_KEY DASHSCOPE_API_KEY; do
    eval val=\$$key
    [ -n "$val" ] && echo "Loaded $key" || echo "Warning: $key not found"
done
