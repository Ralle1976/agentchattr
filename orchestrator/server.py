"""Orchestrator v3 — single dashboard that manages everything.

Features:
- Starts/stops AgentChattr server automatically
- KI Planner: describe what you want → auto-generates swarm
- Multi-project swarms with isolated channels
- Health monitoring + auto-restart
- Clean process management (PID files, process groups)

Usage:
    cd ~/agentchattr && .venv/bin/python orchestrator/server.py
    Dashboard: http://0.0.0.0:8400
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from orchestrator.agents import AgentPool, load_api_keys
from orchestrator.chat_bridge import ChatBridge
from orchestrator.health import HealthMonitor
from orchestrator.planner import analyze_project
from orchestrator.projects import detect_projects, get_opencode_models_cached
from orchestrator.swarm import SwarmManager, SwarmConfig

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Load API keys into environment
for k, v in load_api_keys().items():
    os.environ.setdefault(k, v)

app = FastAPI(title="Orchestrator v3")
bridge = ChatBridge(port=8300)
pool = AgentPool(chat_server_port=8300)
health = HealthMonitor(pool, check_interval=10)
swarm_mgr = SwarmManager(pool, bridge)

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)


# --- Dashboard ---

@app.get("/")
async def index():
    html_path = STATIC_DIR / "dashboard.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Orchestrator v3</h1><p>Dashboard not found</p>")
    return HTMLResponse(html_path.read_text("utf-8"), headers={"Cache-Control": "no-store"})


# --- Server Management ---

@app.get("/api/server/status")
async def server_status():
    return JSONResponse({
        "running": bridge.is_server_running(),
        "port": bridge.port,
        "token": bridge.get_token()[:8] + "..." if bridge.get_token() else "",
    })


@app.post("/api/server/start")
async def server_start():
    ok = bridge.start_server()
    return JSONResponse({"started": ok})


@app.post("/api/server/stop")
async def server_stop():
    bridge.stop_server()
    return JSONResponse({"stopped": True})


@app.post("/api/server/restart")
async def server_restart():
    ok = bridge.restart_server()
    return JSONResponse({"restarted": ok})


# --- Swarm API ---

@app.get("/api/swarms")
async def list_swarms():
    return JSONResponse(swarm_mgr.get_status())


@app.post("/api/swarms")
async def create_swarm(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    project_path = body.get("project_path", "").strip()
    channel = body.get("channel", "").strip()
    orchestrator_model = body.get("orchestrator_model", "zai-coding-plan/glm-5.1")
    worker_models = body.get("worker_models", [])
    include_claude = body.get("include_claude", False)
    initial_task = body.get("initial_task", "")

    if not name or not project_path:
        return JSONResponse({"error": "name and project_path required"}, status_code=400)
    if not channel:
        channel = name.lower().replace(" ", "-").replace("_", "-")

    # Ensure chat server is running
    if not bridge.is_server_running():
        if not bridge.start_server():
            return JSONResponse({"error": "Could not start chat server"}, status_code=500)

    config = SwarmConfig(
        name=name,
        project_path=project_path,
        channel=channel,
        orchestrator_model=orchestrator_model,
        worker_models=worker_models,
        include_claude=include_claude,
        initial_task=initial_task,
    )

    # Launch in background thread
    def _launch():
        swarm_mgr.create_swarm(config)

    threading.Thread(target=_launch, daemon=True).start()
    return JSONResponse({"status": "creating", "name": name, "channel": channel})


@app.post("/api/swarms/{name}/stop")
async def stop_swarm(name: str):
    ok = swarm_mgr.stop_swarm(name)
    return JSONResponse({"stopped": ok})


@app.post("/api/swarms/from-plan")
async def create_swarm_from_plan(request: Request):
    """Create a role-based swarm from KI planner result.

    Body: {
        "name": "project-name",
        "project_path": "/path/to/project",
        "channel": "optional-channel",
        "plan": {
            "tasks": [{"title": "...", "description": "...", "role": "Builder"}],
            "roles": {"Builder": {"model": "...", "why": "..."}},
            "initial_orchestrator_prompt": "..."
        },
        "orchestrator_model": "optional override"
    }
    """
    body = await request.json()
    name = body.get("name", "").strip()
    project_path = body.get("project_path", "").strip()
    plan = body.get("plan", {})
    channel = body.get("channel", "").strip() or name.lower().replace(" ", "-").replace("_", "-")
    orch_model = body.get("orchestrator_model", "zai-coding-plan/glm-5.1")

    if not name or not project_path:
        return JSONResponse({"error": "name and project_path required"}, status_code=400)

    if not plan.get("roles"):
        return JSONResponse({"error": "plan must include roles"}, status_code=400)

    # Ensure chat server is running
    if not bridge.is_server_running():
        if not bridge.start_server():
            return JSONResponse({"error": "Could not start chat server"}, status_code=500)

    config = SwarmConfig(
        name=name,
        project_path=project_path,
        channel=channel,
        orchestrator_model=orch_model,
        roles=plan.get("roles", {}),
        tasks=plan.get("tasks", []),
        initial_task=plan.get("initial_orchestrator_prompt", ""),
    )

    def _launch():
        swarm_mgr.create_swarm(config)

    threading.Thread(target=_launch, daemon=True).start()
    return JSONResponse({
        "status": "creating",
        "name": name,
        "channel": channel,
        "roles": list(plan.get("roles", {}).keys()),
        "task_count": len(plan.get("tasks", [])),
    })


@app.post("/api/stop-all")
async def stop_all():
    swarm_mgr.stop_all()
    pool.stop_all()
    return JSONResponse({"status": "all stopped"})


# --- KI Planner ---

@app.post("/api/planner/analyze")
async def planner_analyze(request: Request):
    body = await request.json()
    project_path = body.get("project_path", "")
    user_request = body.get("request", "")
    model = body.get("model", "zai-coding-plan/glm-5.1")

    if not project_path or not user_request:
        return JSONResponse({"error": "project_path and request required"}, status_code=400)

    # Run analysis in thread (blocking subprocess)
    result = {"status": "analyzing"}

    def _analyze():
        nonlocal result
        result = analyze_project(project_path, user_request, model)

    t = threading.Thread(target=_analyze)
    t.start()
    t.join(timeout=190)  # 3+ minutes for slow first runs

    if t.is_alive():
        return JSONResponse({"error": "Analysis timed out (190s). Versuche es erneut — der zweite Lauf ist schneller."}, status_code=504)

    return JSONResponse(result)


# --- Project & Model API ---

@app.get("/api/projects")
async def list_projects():
    return JSONResponse(detect_projects())


@app.get("/api/models")
async def list_models():
    return JSONResponse(get_opencode_models_cached())


@app.get("/api/agents")
async def list_agents():
    return JSONResponse(pool.get_status())


# --- Health ---

@app.get("/api/health")
async def health_check():
    return JSONResponse({
        "orchestrator": "running",
        "chat_server": bridge.is_server_running(),
        "health_monitor": health._running,
        "swarms": len(swarm_mgr.swarms),
        "agents": len(pool.agents),
    })


# --- Startup ---

@app.on_event("startup")
async def on_startup():
    # Auto-start chat server
    if not bridge.is_server_running():
        logger.info("Starting AgentChattr server...")
        bridge.start_server()
    else:
        logger.info("AgentChattr server already running on :%d", bridge.port)
        bridge._refresh_token()

    health.start()
    logger.info("Orchestrator v3 ready")


def main():
    port = 8400
    host = "0.0.0.0"

    print(f"\n  Orchestrator v3")
    print(f"  Dashboard:    http://{host}:{port}")
    print(f"  Chat Server:  http://{host}:8300 (auto-managed)")
    print(f"  Health:       active (10s interval)")
    print(f"  KI Planner:   POST /api/planner/analyze\n")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
