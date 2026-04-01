#!/bin/bash
# Claude Code Watchdog - restarts Claude CLI wrapper when MCP connection breaks
# Runs alongside the team, monitors Claude's health, auto-restarts on failure
cd /home/tango/agentchattr
source .venv/bin/activate

echo "=== Claude Watchdog started ==="

while true; do
    # Kill any existing Claude tmux sessions
    tmux kill-session -t ac-claude 2>/dev/null
    tmux kill-session -t agentchattr-claude 2>/dev/null
    for s in $(tmux ls 2>/dev/null | grep "agentchattr-claude" | cut -d: -f1); do
        tmux kill-session -t "$s" 2>/dev/null
    done
    sleep 2

    # Clear Claude queues
    for f in data/claude*_queue.jsonl; do : > "$f" 2>/dev/null; done

    echo "[$(date +%H:%M:%S)] Starting Claude CLI..."

    # Start Claude in tmux
    TMUX="" tmux new-session -d -s ac-claude -c /home/tango/agentchattr \
        "bash -lc 'source .venv/bin/activate && python wrapper.py claude --dangerously-skip-permissions'"

    sleep 8

    # Auto-trust
    for s in $(tmux ls 2>/dev/null | grep agentchattr-claude | cut -d: -f1); do
        TMUX="" tmux send-keys -t "$s" Enter 2>/dev/null
    done

    sleep 5
    echo "[$(date +%H:%M:%S)] Claude should be online"

    # Monitor loop - check every 30s if Claude is still healthy
    FAIL_COUNT=0
    while true; do
        sleep 30

        # Check if tmux session still exists
        if ! tmux has-session -t ac-claude 2>/dev/null; then
            echo "[$(date +%H:%M:%S)] Claude tmux session died. Restarting..."
            break
        fi

        # Check if Claude is registered with the server
        TOKEN=$(grep "Session token" /tmp/agentchattr-server.log | tail -1 | awk '{print $NF}')
        STATUS=$(curl -s "http://127.0.0.1:8300/api/status?token=$TOKEN" 2>/dev/null)

        # Look for any claude agent that's available
        HAS_CLAUDE=$(echo "$STATUS" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    found=any(n.startswith('claude') and isinstance(i,dict) and i.get('available') for n,i in d.items())
    print('yes' if found else 'no')
except:
    print('no')
" 2>/dev/null)

        if [ "$HAS_CLAUDE" = "no" ]; then
            FAIL_COUNT=$((FAIL_COUNT + 1))
            echo "[$(date +%H:%M:%S)] Claude not available (fail $FAIL_COUNT/3)"
            if [ "$FAIL_COUNT" -ge 3 ]; then
                echo "[$(date +%H:%M:%S)] Claude offline for 90s. Restarting..."
                break
            fi
        else
            FAIL_COUNT=0
        fi
    done
done
