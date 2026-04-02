# Orchestrator v3 - Kompletter Workflow Fix

## Das Problem

Der KI-Planer → Swarm Workflow hat eine kritische Lücke:
1. KI analysiert Projekt ✓
2. Swarm startet Workers ✓
3. Swarm startet Orchestrator ✓
4. **Channel wird nie erstellt** ❌
5. Orchestrator postet in nicht-existenten Channel ❌
6. Nachrichten gehen verloren ❌

## Die Lösung

### 1. Channel-Erstellung über REST API (NEU)

Channel muss erstellt werden BEVOR der Orchestrator seinen ersten @mention sendet.

**Option A: Direkter Settings-Ingress**
```python
# In chat_bridge.py
def create_channel(self, channel_name: str) -> bool:
    """Create channel by modifying room_settings directly."""

    # Option A: Direkter REST-Call
    token = self.get_token()
    req = urllib.request.Request(
        f"http://127.0.0.1:{self.port}/api/settings",
        method="POST",
        data=json.dumps({
            "token": token,
            "action": "create_channel",
            "name": channel_name,
        }).encode(),
        headers={"Content-Type": "application/json"},
    )
    # ...
```

**Option B: AgentChattr erweitern (SAUBER)**
In `app.py` neuen Endpoint:
```python
@app.post("/api/channels")
async def create_channel(request: Request):
    body = await request.json()
    name = body.get("name", "").strip().lower()

    if not name or not _CHANNEL_NAME_RE.match(name):
        return JSONResponse({"error": "invalid channel name"}, 400)

    if name in room_settings["channels"]:
        return JSONResponse({"exists": True})

    room_settings["channels"].append(name)
    _save_settings()
    await broadcast_settings()
    return JSONResponse({"created": True, "name": name})
```

### 2. Swarm-Workflow erweitern

In `swarm.py` `_launch_swarm()`:

```python
# Step 2.5: Create Channel BEFORE kickoff
logger.info("Creating channel #%s...", config.channel)
ok = self.bridge.create_channel(config.channel)
if not ok:
    raise RuntimeError(f"Failed to create channel: {config.channel}")

# Verify channel exists
status = self.bridge.get_status()
channels = status.get("channels", [])
if config.channel not in channels:
    raise RuntimeError(f"Channel not found after creation: {config.channel}")

logger.info("Channel #%s verified", config.channel)
```

### 3. Initial System Message

Optional: Send eine "System-Join" Nachricht, die:
1. Channel erstellt (falls noch nicht passiert)
2. Allen Agenten signalisiert "Channel is ready"

```python
# In swarm.py nach Channel-Erstellung
initial_msg = {
    "sender": "system",
    "text": f"Swarm '{config.name}' started in #{config.channel}",
    "type": "system",
    "timestamp": time.time(),
}
# Send via MCP oder REST
```

## Implementierungs-Plan

### Phase 1: Minimal Fix (GET IT WORKING)

1. **ChatBridge.create_channel()** implementieren
   - Direkter REST-Call zu AgentChattr
   - Token-Refresh auf Fehler
   - Verify exists nach create

2. **Swarm._launch_swarm()** erweitern
   - Channel erstellen Step 2.5
   - Verify exists
   - Log deutlich machen

3. **Testen**
   - orch server starten
   - swarm über dashboard erstellen
   - prüfen ob channel in web ui appears

### Phase 2: Robustness

1. **Retry Logic**
   - Wenn Channel create fehlschlägt: 3x retry
   - Wenn Orchestrator timeout: Cleanup workers

2. **Error Handling**
   - Klare Fehlermeldungen
   - Partial failure cleanup (wenn Orchestrator startet aber Channel creation fails)

3. **Monitoring**
   - Channel exists in SwarmStatus
   - UI zeigt "Creating channel..." während Warten

## Files zu ändern

1. `orchestrator/chat_bridge.py`
   - `create_channel()` implementieren
   - `_verify_channel_exists()` helper

2. `orchestrator/swarm.py`
   - `_launch_swarm()` Step 2.5 einfügen
   - Error handling verbessern

3. OPTIONAL: `app.py`
   - `/api/channels` Endpoint (sauberer)

## Test-Plan

```bash
# 1. Start Orchestrator
cd ~/agentchattr && .venv/bin/python orchestrator/server.py

# 2. Dashboard öffnen
firefox http://localhost:8400

# 3. Neuen Swarm erstellen
POST /api/swarms
{
  "name": "test-thai",
  "project_path": "/mnt/c/Users/tango/Desktop/thai",
  "channel": "thai",
  "orchestrator_model": "zai-coding-plan/glm-5.1",
  "worker_models": ["alibaba-coding-plan/qwen3-coder-next"],
  "initial_task": "Analysiere das Projekt"
}

# 4. Verify
# - Channel "thai" erscheint in Web UI
# - Orchestrator postet in #thai
# - Workers antworten in #thai
```

## Success Criteria

✓ Channel wird erstellt bevor Orchestrator postet
✓ Alle Nachrichten landen im richtigen Channel
✓ Keine "channel not found" Errors mehr
✓ Swarm läuft komplett autonom
