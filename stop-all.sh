#!/usr/bin/env bash
# AgentChattr - Stop everything
cd "$(dirname "$0")"

echo "Stopping agents..."
pkill -f "wrapper_api.py" 2>/dev/null || true
tmux kill-server 2>/dev/null || true
sleep 1

echo "Stopping server..."
pkill -f "agentchattr.*run.py" 2>/dev/null || true

sleep 1
echo "All stopped."
