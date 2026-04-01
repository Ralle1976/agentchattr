#!/bin/bash
# AgentChattr Team Restart
# Claude via CLI watchdog (Max Plan), others via wrapper_opencode.py
cd /home/tango/agentchattr
source .venv/bin/activate

echo "=== Stopping everything ==="
for pid in $(pgrep -f "wrapper_opencode" 2>/dev/null); do kill -9 "$pid" 2>/dev/null; done
for pid in $(pgrep -f "wrapper_claude_watchdog" 2>/dev/null); do kill -9 "$pid" 2>/dev/null; done
for pid in $(pgrep -f "wrapper.py claude" 2>/dev/null); do kill -9 "$pid" 2>/dev/null; done
tmux kill-server 2>/dev/null
sleep 2

echo "=== Clearing queues ==="
for f in data/*_queue.jsonl; do : > "$f" 2>/dev/null; done

CWD="/home/tango/qr-info-portal"

echo "=== Starting Claude (Max Plan + Watchdog) ==="
bash wrapper_claude_watchdog.sh > /tmp/oc-claude-watchdog.log 2>&1 &
echo "  Watchdog PID: $!"

sleep 15

echo "=== Starting OpenCode agents ==="

# GLM-5.1 (Z.ai Coding Plan)
python wrapper_opencode.py -m zai-coding-plan/glm-5.1 \
  --label "GLM-5.1" --cwd "$CWD" > /tmp/oc-glm.log 2>&1 &
echo "  GLM-5.1: PID $!"
sleep 1

# Qwen 3.5+ (Alibaba Coding Plan)
python wrapper_opencode.py -m alibaba-coding-plan/qwen3.5-plus \
  --label "Qwen 3.5+" --cwd "$CWD" > /tmp/oc-qwen.log 2>&1 &
echo "  Qwen 3.5+: PID $!"
sleep 1

# MiniMax M2.7
python wrapper_opencode.py -m minimax/MiniMax-M2.7 \
  --label "MiniMax M2.7" --cwd "$CWD" > /tmp/oc-minimax.log 2>&1 &
echo "  MiniMax M2.7: PID $!"

echo "=== Waiting for registration (15s) ==="
sleep 15

TOKEN=$(grep "Session token" /tmp/agentchattr-server.log | tail -1 | awk '{print $NF}')
echo ""
echo "=== Team Status ==="
curl -s "http://127.0.0.1:8300/api/status?token=$TOKEN" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n,i in d.items():
    if isinstance(i,dict):
        s='ONLINE' if i.get('available') else 'starting'
        print(f'  @{n:18} [{s:8}] {i.get(\"label\",\"\")}')
"

# Kickoff
echo ""
echo "=== Sending kickoff ==="
TS=$(date +%H:%M:%S)
for f in data/claude_queue.jsonl data/opencode-*_queue.jsonl; do
    [ -f "$f" ] || continue
    echo "{\"sender\":\"Ralle1976\",\"text\":\"Projekt: ~/qr-info-portal. Ihr arbeitet als autonomes Team. Analysiert das Projekt, erstellt Aufgabenliste, arbeitet Tasks ab. Jede Nachricht endet mit @mention. Keine Fragen an Menschen. Startet jetzt.\",\"time\":\"$TS\",\"channel\":\"general\"}" > "$f"
    echo "  Triggered: $(basename $f _queue.jsonl)"
done

echo ""
echo "=== DONE ==="
echo "Chat:    http://172.27.138.46:8300"
echo "Manager: http://172.27.138.46:8300/manager"
echo ""
echo "Claude: Max Plan via CLI (auto-restarts on failure)"
echo "Others: OpenCode wrapper (stable, no tmux issues)"
