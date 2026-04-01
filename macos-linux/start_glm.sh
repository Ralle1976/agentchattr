#!/usr/bin/env sh
# agentchattr - starts server (if not running) + GLM-5.1 API agent wrapper
cd "$(dirname "$0")/.."

PYTHON_BIN=""
if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    echo "Python 3 is required but was not found on PATH."
    exit 1
fi

ensure_venv() {
    if [ -d ".venv" ] && [ ! -x ".venv/bin/python" ]; then
        echo "Recreating .venv for this platform..."
        rm -rf .venv
    fi

    if [ ! -x ".venv/bin/python" ]; then
        echo "Creating virtual environment..."
        "$PYTHON_BIN" -m venv .venv || {
            echo "Error: failed to create .venv with $PYTHON_BIN."
            exit 1
        }
        .venv/bin/python -m pip install -q -r requirements.txt || {
            echo "Error: failed to install Python dependencies."
            exit 1
        }
    fi
}

is_server_running() {
    lsof -i :8300 -sTCP:LISTEN >/dev/null 2>&1 || \
    ss -tlnp 2>/dev/null | grep -q ':8300 '
}

# Auto-load API key from OpenCode auth if not set
if [ -z "$ZHIPU_API_KEY" ] && [ -f "./load_api_keys.sh" ]; then
    . ./load_api_keys.sh
fi
if [ -z "$ZHIPU_API_KEY" ]; then
    echo "Error: ZHIPU_API_KEY not set and not found in OpenCode auth."
    echo "Get an API key at https://z.ai/subscribe"
    exit 1
fi

ensure_venv

if ! is_server_running; then
    .venv/bin/python run.py > data/server.log 2>&1 &
    i=0
    while [ "$i" -lt 30 ]; do
        if is_server_running; then break; fi
        sleep 0.5
        i=$((i + 1))
    done
fi

.venv/bin/python wrapper_api.py glm
