"""Swarm v2 — autonomous agent team for one project.

Fixed version addressing all critical bugs:
- Channel creation via first agent message (no cross-process store import)
- Active polling for registration (no fixed sleep timers)
- Guaranteed orchestrator registered_name before kickoff
- Proper @mention syntax in kickoff prompt
"""

import json
import time
import threading
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from orchestrator.agents import AgentPool, AgentInstance
from orchestrator.chat_bridge import ChatBridge

logger = logging.getLogger(__name__)

ORCHESTRATOR_PROMPT = """Du bist der Orchestrator. Du koordinierst, du schreibst KEINEN Code.
Weise Tasks per @mention zu. Pruefe Ergebnisse. Naechster Task wenn OK.

WORKER:
{workers}

TASKS:
{task_list}

PROJEKT: {project_path}
"""


@dataclass
class SwarmConfig:
    name: str
    project_path: str
    channel: str
    orchestrator_model: str = "zai-coding-plan/glm-5.1"
    worker_models: list[str] = field(default_factory=lambda: [
        "alibaba-coding-plan/qwen3-coder-next",
    ])
    include_claude: bool = False
    initial_task: str = ""
    # Role-based swarm (from KI planner)
    roles: dict = field(default_factory=dict)        # {"Builder": {"model": "...", "why": "..."}, ...}
    tasks: list = field(default_factory=list)         # [{"title": "...", "description": "...", "role": "Builder"}, ...]


@dataclass
class Swarm:
    config: SwarmConfig
    orchestrator: Optional[AgentInstance] = None
    workers: list[AgentInstance] = field(default_factory=list)
    started_at: float = 0.0
    status: str = "stopped"
    error: str = ""


class SwarmManager:
    def __init__(self, pool: AgentPool, bridge: ChatBridge):
        self.pool = pool
        self.bridge = bridge
        self.swarms: dict[str, Swarm] = {}
        self._lock = threading.Lock()

    def create_swarm(self, config: SwarmConfig) -> Swarm:
        """Create and start a new swarm. Blocks until ready or failed."""
        swarm = Swarm(config=config, started_at=time.time(), status="starting")
        with self._lock:
            self.swarms[config.name] = swarm

        try:
            self._launch_swarm(swarm)
        except Exception as e:
            logger.error("Swarm '%s' failed: %s", config.name, e)
            # Cleanup on failure: stop agents + remove channel
            if swarm.orchestrator:
                self.pool.stop(swarm.orchestrator.id)
            for w in swarm.workers:
                self.pool.stop(w.id)
            if config.channel != "general":
                try:
                    self.bridge.delete_channel(config.channel)
                    logger.info("Cleaned up channel #%s after swarm failure", config.channel)
                except Exception:
                    pass
            with self._lock:
                swarm.status = "error"
                swarm.error = str(e)

        return swarm

    def _launch_swarm(self, swarm: Swarm):
        config = swarm.config

        # Step 1: Start workers
        # Role-based mode: one worker per role from KI planner
        # Legacy mode: one worker per model in worker_models
        worker_labels = []
        worker_roles = {}  # label -> role name

        if config.roles:
            # Role-based: start one worker per role
            for role_name, role_info in config.roles.items():
                model = role_info.get("model", "alibaba-coding-plan/qwen3-coder-next")
                label = f"{role_name} [{config.name}]"
                agent = self.pool.start_opencode(
                    model=model, label=label, cwd=config.project_path,
                    project=config.name, channel=config.channel,
                )
                swarm.workers.append(agent)
                worker_labels.append(label)
                worker_roles[label] = role_name
                logger.info("Worker started: %s (role=%s, model=%s, pid=%s)",
                           label, role_name, model, agent.pid)
        else:
            # Legacy mode: one worker per model
            for model in config.worker_models:
                model_short = model.split("/")[-1]
                label = f"{model_short} [{config.name}]"
                agent = self.pool.start_opencode(
                    model=model, label=label, cwd=config.project_path,
                    project=config.name, channel=config.channel,
                )
                swarm.workers.append(agent)
                worker_labels.append(label)
                logger.info("Worker started: %s (pid=%s)", label, agent.pid)

        if config.include_claude:
            agent = self.pool.start_claude(
                cwd=config.project_path, project=config.name, channel=config.channel,
            )
            swarm.workers.append(agent)
            worker_labels.append("Claude (Max Plan)")
            worker_roles["Claude (Max Plan)"] = "Reviewer"
            logger.info("Claude worker started (pid=%s)", agent.pid)

        # Step 2: Wait for workers to register (active polling, not fixed sleep)
        logger.info("Waiting for %d workers to register...", len(worker_labels))
        worker_map = self.bridge.wait_for_agents(worker_labels, timeout=45)

        # Update agent instances with registered names
        for w in swarm.workers:
            if w.label in worker_map:
                w.registered_name = worker_map[w.label]
                w.status = "running"
            else:
                w.status = "error"
                w.last_error = "Registration timeout"

        registered_workers = []
        for label, name in worker_map.items():
            role = worker_roles.get(label, "")
            if role:
                registered_workers.append(f"@{name} ({label}) — Rolle: {role}")
            else:
                registered_workers.append(f"@{name} ({label})")
        if not registered_workers:
            raise RuntimeError("No workers registered! Cannot start swarm.")

        # Step 3: Start orchestrator
        orch_label = f"Orchestrator [{config.name}]"
        orch = self.pool.start_opencode(
            model=config.orchestrator_model, label=orch_label,
            cwd=config.project_path, project=config.name, channel=config.channel,
        )
        swarm.orchestrator = orch
        logger.info("Orchestrator started: %s (pid=%s)", orch_label, orch.pid)

        # Step 4: Wait for orchestrator to register (active polling)
        orch_map = self.bridge.wait_for_agents([orch_label], timeout=30)
        if orch_label not in orch_map:
            raise RuntimeError("Orchestrator failed to register!")

        orch.registered_name = orch_map[orch_label]
        orch.status = "running"
        logger.info("Orchestrator registered as @%s", orch.registered_name)

        # Step 4.5: Create channel BEFORE kickoff (CRITICAL FIX!)
        logger.info("Creating channel #%s for swarm '%s'...", config.channel, config.name)
        channel_ok = self.bridge.create_channel(config.channel)
        if not channel_ok:
            raise RuntimeError(f"Failed to create channel: {config.channel}")

        # Verify channel exists
        if not self.bridge.verify_channel_exists(config.channel):
            raise RuntimeError(f"Channel not found after creation: {config.channel}")

        logger.info("Channel #%s verified and ready", config.channel)

        # Step 5: Send kickoff trigger to orchestrator's queue
        workers_str = "\n".join(registered_workers)

        # Build task list from KI planner (or empty)
        task_list_str = ""
        if config.tasks:
            lines = []
            for i, t in enumerate(config.tasks, 1):
                role = t.get("role", "Builder")
                title = t.get("title", t.get("t", ""))
                desc = t.get("description", t.get("d", ""))
                priority = t.get("priority", t.get("p", i))
                lines.append(f"{i}. [{role}] {title} — {desc} (P{priority})")
            task_list_str = "\n".join(lines)
        else:
            task_list_str = "(Keine vordefinierten Tasks — Orchestrator analysiert selbst)"

        prompt = ORCHESTRATOR_PROMPT.format(
            workers=workers_str,
            task_list=task_list_str,
            project_path=config.project_path,
        )

        initial_task = config.initial_task or (
            f"Starte mit dem ersten Task. Weise ihn dem passenden Worker zu."
        )

        kickoff_msg = f"{prompt}\n\nSTART: {initial_task}"

        # Post kickoff message to chat channel so the orchestrator can read it.
        # The wrapper's handle_trigger() reads chat messages, NOT the queue data.
        # The queue file only serves as a wake-up signal.
        try:
            self.bridge.send_message(
                sender="orchestrator-system",
                text=kickoff_msg,
                channel=config.channel,
            )
            logger.info("Kickoff posted to #%s chat", config.channel)
        except Exception as e:
            logger.warning("Failed to post kickoff to chat: %s (falling back to queue-only)", e)

        # Also write to queue file as a trigger signal
        queue_file = self.pool._root / "data" / f"{orch.registered_name}_queue.jsonl"
        trigger = json.dumps({
            "sender": "orchestrator-system",
            "text": kickoff_msg,
            "time": time.strftime("%H:%M:%S"),
            "channel": config.channel,
        })
        queue_file.write_text(trigger + "\n", "utf-8")

        logger.info("Swarm '%s' kicked off in #%s with %d workers",
                     config.name, config.channel, len(registered_workers))

        with self._lock:
            swarm.status = "running"

    def stop_swarm(self, name: str, cleanup: bool = True) -> bool:
        with self._lock:
            swarm = self.swarms.get(name)
            if not swarm:
                return False

        if swarm.orchestrator:
            self.pool.stop(swarm.orchestrator.id)
        for w in swarm.workers:
            self.pool.stop(w.id)

        # Cleanup channel + queue files
        if cleanup and swarm.config.channel != "general":
            try:
                self.bridge.delete_channel(swarm.config.channel)
                logger.info("Cleaned up channel #%s for swarm '%s'", swarm.config.channel, name)
            except Exception as e:
                logger.warning("Failed to cleanup channel #%s: %s", swarm.config.channel, e)

        with self._lock:
            swarm.status = "stopped"
        return True

    def stop_all(self):
        with self._lock:
            names = list(self.swarms.keys())
        for name in names:
            self.stop_swarm(name)

    def get_status(self) -> list[dict]:
        result = []
        with self._lock:
            for name, swarm in self.swarms.items():
                orch_info = None
                if swarm.orchestrator:
                    orch_info = {
                        "id": swarm.orchestrator.id,
                        "name": swarm.orchestrator.registered_name,
                        "label": swarm.orchestrator.label,
                        "model": swarm.orchestrator.model,
                        "status": swarm.orchestrator.status,
                    }

                # Build role lookup: label -> role
                role_lookup = {}
                if swarm.config.roles:
                    for label in [w.label for w in swarm.workers]:
                        for role_name in swarm.config.roles:
                            if role_name in label:
                                role_lookup[label] = role_name
                                break

                workers_info = []
                for w in swarm.workers:
                    workers_info.append({
                        "id": w.id,
                        "name": w.registered_name,
                        "label": w.label,
                        "model": w.model,
                        "status": w.status,
                        "role": role_lookup.get(w.label, ""),
                    })

                result.append({
                    "name": name,
                    "project": swarm.config.project_path,
                    "channel": swarm.config.channel,
                    "status": swarm.status,
                    "error": swarm.error,
                    "orchestrator": orch_info,
                    "workers": workers_info,
                    "roles": swarm.config.roles,
                    "tasks": swarm.config.tasks,
                    "uptime": int(time.time() - swarm.started_at) if swarm.started_at else 0,
                })
        return result
