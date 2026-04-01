#!/usr/bin/env bash
# AgentChattr - Start everything: server + all agents
# Usage: bash start-all.sh
# Stop:  bash stop-all.sh

cd "$(dirname "$0")"

# Load API keys from OpenCode auth
AUTH_FILE="$HOME/.local/share/opencode/auth.json"
if [ -f "$AUTH_FILE" ]; then
    export MINIMAX_API_KEY=$(python3 -c "import json; print(json.load(open('$AUTH_FILE'))['minimax']['key'])" 2>/dev/null)
    export ZHIPU_API_KEY=$(python3 -c "import json; print(json.load(open('$AUTH_FILE'))['zai-coding-plan']['key'])" 2>/dev/null)
    echo "API keys loaded"
fi

# Clean old processes (careful: only kill OUR processes)
for pid in $(pgrep -f "wrapper_api.py" 2>/dev/null); do kill "$pid" 2>/dev/null; done
tmux kill-server 2>/dev/null || true
for pid in $(pgrep -f "agentchattr/.*run.py" 2>/dev/null); do kill "$pid" 2>/dev/null; done
sleep 2

# Start server (pipe YES for network confirmation)
echo "Starting server..."
echo "YES" | .venv/bin/python run.py --allow-network > /tmp/agentchattr-server.log 2>&1 &

for i in $(seq 1 20); do
    if ss -tlnp 2>/dev/null | grep -q ':8300 '; then break; fi
    sleep 0.5
done

if ! ss -tlnp 2>/dev/null | grep -q ':8300 '; then
    echo "ERROR: Server failed to start. Check /tmp/agentchattr-server.log"
    exit 1
fi

# API agents
.venv/bin/python wrapper_api.py minimax > /tmp/ac-minimax.log 2>&1 &
.venv/bin/python wrapper_api.py glm > /tmp/ac-glm.log 2>&1 &

# Terminal agents in tmux
tmux new-session -d -s ac-claude -c "$PWD" \
    "bash -lc 'cd $PWD && source .venv/bin/activate && python wrapper.py claude --dangerously-skip-permissions'"
tmux new-session -d -s ac-codex -c "$PWD" \
    "bash -lc 'cd $PWD && source .venv/bin/activate && python wrapper.py codex -- --dangerously-bypass-approvals-and-sandbox'"
tmux new-session -d -s ac-gemini -c "$PWD" \
    "bash -lc 'cd $PWD && source .venv/bin/activate && python wrapper.py gemini -- --yolo'"

sleep 8

# Status
LAN_IP=$(hostname -I | awk '{print $1}')
TOKEN=$(grep 'Session token' /tmp/agentchattr-server.log | tail -1 | awk '{print $NF}')

echo ""
echo "========================================"
echo "  AgentChattr"
echo "  Local:   http://localhost:8300"
echo "  Tablet:  http://$LAN_IP:8300"
echo "========================================"
echo ""

curl -s "http://127.0.0.1:8300/api/status?token=$TOKEN" 2>/dev/null | \
    python3 -c "
import json,sys
data=json.load(sys.stdin)
for name,info in data.items():
    if isinstance(info, dict):
        s='ONLINE' if info.get('available') else 'starting...'
        print(f'  {info.get(\"label\",name):12} @{name:10} [{s}]')
" 2>/dev/null || echo "  (agents registering...)"

echo ""
echo "Stop: bash ~/agentchattr/stop-all.sh"
