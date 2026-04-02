# Orchestrator v2 - Redesign Plan

## Lessons Learned (v1 Probleme)
1. Server-Restart = alle Agents tot (Token-Invalidierung)
2. tmux-Nesting bricht bei OpenCode TUI
3. wrapper_opencode.py (non-interaktiv) funktioniert stabil
4. Zombie-Prozesse akkumulieren bei pkill
5. Claude CLI braucht tmux + Watchdog (Max Plan nur über CLI)
6. Agent-Registration ist stateful und fragil
7. Kein sauberes Process-Lifecycle-Management

## Design-Prinzipien v2
- **Stateless Agent-Kommunikation**: Agents authentifizieren sich per Heartbeat, nicht per Token
- **PID-File Management**: Jeder Agent hat eine PID-Datei, sauberes Cleanup
- **Non-Interactive First**: Alle Agents über `opencode run` oder API-Calls, kein TUI
- **Claude Sonderbehandlung**: Eigener stabiler Wrapper mit Auto-Reconnect
- **Multi-Projekt**: Jedes Projekt = eigener Channel + eigene Agent-Gruppe
- **Kein Server-Restart nötig**: Orchestrator läuft separat vom Chat-Server

## Architektur

```
┌─────────────────────────────────────┐
│     AgentChattr Server (:8300)      │  ← Bleibt unverändert
│     Chat, Channels, MCP            │
└──────────────┬──────────────────────┘
               │ HTTP API
┌──────────────┴──────────────────────┐
│     Orchestrator v2 (:8400)         │  ← NEU, separater Prozess
│                                     │
│  ┌─ Agent Pool ──────────────────┐  │
│  │  PID-tracked, auto-restart    │  │
│  │  - Claude (tmux + watchdog)   │  │
│  │  - OpenCode GLM-5.1 (run)    │  │
│  │  - OpenCode Qwen 3.5+ (run)  │  │
│  │  - API: MiniMax, GLM direct   │  │
│  └───────────────────────────────┘  │
│                                     │
│  ┌─ Project Manager ─────────────┐  │
│  │  - Project A: #channel-a      │  │
│  │  - Project B: #channel-b      │  │
│  │  Per project: agent assignment │  │
│  └───────────────────────────────┘  │
│                                     │
│  ┌─ Health Monitor ──────────────┐  │
│  │  - Heartbeat check (5s)       │  │
│  │  - Auto-restart on failure    │  │
│  │  - Rate limit detection       │  │
│  │  - Failover chains            │  │
│  └───────────────────────────────┘  │
│                                     │
│  Dashboard: :8400/                  │
└─────────────────────────────────────┘
```

## Dateien
- orchestrator/server.py      - FastAPI auf :8400
- orchestrator/agents.py      - Agent-Pool mit PID-Management
- orchestrator/health.py      - Health Monitor + Auto-Restart
- orchestrator/projects.py    - Multi-Projekt-Verwaltung
- orchestrator/static/        - Dashboard UI
