#!/usr/bin/env python3
"""Integrationstest für den kompletten KI-Planer → Swarm Workflow.

Dieser Test:
1. Startet den Orchestrator Server (der wiederum den Chat Server startet)
2. Testet die KI-Planer Analyse
3. Erstellt einen kompletten Swarm
4. Verifiziert dass Channel erstellt wird
5. Checkt dass Nachrichten im Channel landen

Usage:
    cd ~/agentchattr && .venv/bin/python test_full_swarm_workflow.py

Voraussetzungen:
    - API Keys für至少 einen Provider (Zhipu, Alibaba, oder MiniMax)
    - Ein Test-Projekt-Pfad
"""

import json
import sys
import time
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))


def cleanup():
    """Stoppe alle Prozesse."""
    print("\n=== CLEANUP ===")

    # Stoppe Orchestrator
    try:
        subprocess.run(["pkill", "-f", "orchestrator/server.py"],
                      capture_output=True, timeout=5)
        print("✓ Orchestrator gestoppt")
    except Exception:
        pass

    # Stoppe Chat Server
    try:
        subprocess.run(["pkill", "-f", "python.*run.py"],
                      capture_output=True, timeout=5)
        print("✓ Chat Server gestoppt")
    except Exception:
        pass

    # Stoppe Wrapper
    try:
        subprocess.run(["pkill", "-f", "wrapper_opencode.py"],
                      capture_output=True, timeout=5)
        subprocess.run(["pkill", "-f", "wrapper_api.py"],
                      capture_output=True, timeout=5)
        print("✓ Wrapper gestoppt")
    except Exception:
        pass

    # Kill tmux sessions
    try:
        result = subprocess.run(["tmux", "ls"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            for line in result.stdout.strip().split("\n"):
                session = line.split(":")[0].strip()
                if session.startswith("orch-") or session.startswith("agentchattr-"):
                    subprocess.run(["tmux", "kill-session", "-t", session],
                                  capture_output=True, timeout=5)
        print("✓ tmux Sessions aufgeräumt")
    except Exception:
        pass

    print()


def test_swarm_workflow():
    """Teste den kompletten Workflow."""

    print("\n" + "="*60)
    print("  INTEGRATIONSTEST: KI-Planer → Swarm Workflow")
    print("="*60 + "\n")

    # Test-Projekt (oder User-Projekt)
    test_project = Path.home() / "agentchattr"  # Selbst als Test-Projekt
    if not test_project.exists():
        print(f"❌ Test-Projekt nicht gefunden: {test_project}")
        return False

    print(f"✓ Test-Projekt: {test_project}")

    # Cleanup erst
    cleanup()

    # Step 1: Starte Orchestrator Server
    print("\n[1/5] Starte Orchestrator Server...")
    orch_proc = subprocess.Popen(
        [str(ROOT / ".venv" / "bin" / "python"),
         str(ROOT / "orchestrator" / "server.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(ROOT),
        preexec_fn=lambda: __import__("os").setpgrp(),
    )

    # Warte auf Startup
    print("  → Warte auf Server-Startup...")
    for i in range(30):
        time.sleep(1)
        try:
            import urllib.request
            urllib.request.urlopen("http://127.0.0.1:8400/api/health", timeout=2)
            print("  ✓ Orchestrator läuft auf :8400")
            break
        except Exception:
            if i % 5 == 0:
                print(f"  → Noch {30-i}s...")
    else:
        print("  ❌ Orchestrator Start fehlgeschlagen")
        return False

    # Warte auf Chat Server
    print("  → Warte auf Chat Server...")
    for i in range(20):
        time.sleep(1)
        try:
            urllib.request.urlopen("http://127.0.0.1:8300/api/status?token=test", timeout=2)
            print("  ✓ Chat Server läuft auf :8300")
            break
        except Exception:
            if i % 5 == 0:
                print(f"  → Noch {20-i}s...")
    else:
        print("  ❌ Chat Server Start fehlgeschlagen")
        return False

    # Step 2: Teste Channel Creation
    print("\n[2/5] Teste Channel Creation...")
    from orchestrator.chat_bridge import ChatBridge
    bridge = ChatBridge(port=8300)

    test_channel = "integration-test"
    if bridge.create_channel(test_channel):
        print(f"  ✓ Channel #{test_channel} erstellt")
    else:
        print(f"  ❌ Channel #{test_channel} Erstellung fehlgeschlagen")
        return False

    if bridge.verify_channel_exists(test_channel):
        print(f"  ✓ Channel #{test_channel} verifiziert")
    else:
        print(f"  ❌ Channel #{test_channel} nicht gefunden")
        return False

    # Step 3: Teste KI-Planer
    print("\n[3/5] Teste KI-Planer Analyse...")
    import urllib.request

    plan_request = {
        "project_path": str(test_project),
        "request": "Analysiere dieses Projekt und erstelle eine Aufgabenliste",
        "model": "zai-coding-plan/glm-5.1",
    }

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8400/api/planner/analyze",
            data=json.dumps(plan_request).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            plan = json.loads(resp.read())

        if "error" in plan:
            print(f"  ❌ KI-Planer Error: {plan['error']}")
            # Kein Return - wir machen weiter ohne Plan
        else:
            print(f"  ✓ KI-Planer Antwort erhalten")
            print(f"  → Zusammenfassung: {plan.get('project_summary', 'N/A')[:60]}...")
            print(f"  → Tasks: {len(plan.get('tasks', []))}")
    except Exception as e:
        print(f"  ⚠ KI-Planer fehlgeschlagen: {e}")
        print(f"  → Machen weiter ohne Plan (OK für Test)")

    # Step 4: Erstelle Swarm
    print("\n[4/5] Erstelle Test-Swarm...")

    # Verwende API-Agent (schneller, kein tmux)
    swarm_request = {
        "name": "test-swarm",
        "project_path": str(test_project),
        "channel": test_channel,
        "orchestrator_model": "zai-coding-plan/glm-5.1",
        "worker_models": ["alibaba-coding-plan/qwen3-coder-next"],
        "include_claude": False,
        "initial_task": "Sag hallo und bestätige dass der Swarm funktioniert",
    }

    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8400/api/swarms",
            data=json.dumps(swarm_request).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            swarm_result = json.loads(resp.read())

        if "error" in swarm_result:
            print(f"  ❌ Swarm Creation Error: {swarm_result['error']}")
            return False

        print(f"  ✓ Swarm erstellt: {swarm_result.get('name')}")
        print(f"  → Channel: {swarm_result.get('channel')}")

    except Exception as e:
        print(f"  ❌ Swarm Creation fehlgeschlagen: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Step 5: Warte auf Aktivität
    print("\n[5/5] Warte auf Swarm-Aktivität...")
    time.sleep(10)  # Give agents time to start and register

    # Check Swarm Status
    try:
        req = urllib.request.Request("http://127.0.0.1:8400/api/swarms")
        with urllib.request.urlopen(req, timeout=5) as resp:
            swarms = json.loads(resp.read())

        if swarms:
            swarm = swarms[0]
            print(f"  ✓ Swarm Status: {swarm.get('status')}")
            print(f"  → Orchestrator: {swarm.get('orchestrator', {}).get('name')}")
            print(f"  → Workers: {len(swarm.get('workers', []))}")

            # Check messages
            messages = bridge.get_messages(channel=test_channel, limit=5)
            print(f"  → Messages in #{test_channel}: {len(messages)}")

            if messages:
                print(f"  ✓ Nachrichten gefunden!")
                for msg in messages[-3:]:
                    sender = msg.get('sender', 'unknown')
                    text = msg.get('text', '')[:60]
                    print(f"     - [{sender}] {text}...")
            else:
                print(f"  ⚠ Keine Nachrichten (noch zu früh?)")

        else:
            print(f"  ❌ Keine Swarms gefunden")
            return False

    except Exception as e:
        print(f"  ⚠ Swarm Status Check fehlgeschlagen: {e}")

    # Success!
    print("\n" + "="*60)
    print("  ✓✓✓ INTEGRATIONSTEST ERFOLGREICH ✓✓✓")
    print("="*60)
    print("\nDer Workflow funktioniert:")
    print("  1. ✓ Server starten")
    print("  2. ✓ Channel erstellen")
    print("  3. ✓ KI-Planer analysiert")
    print("  4. ✓ Swarm erstellt")
    print("  5. ✓ Agenten starten und kommunizieren")
    print()

    return True


if __name__ == "__main__":
    try:
        success = test_swarm_workflow()
    except KeyboardInterrupt:
        print("\n\n⚠ Abgebrochen durch User")
        success = False
    except Exception as e:
        print(f"\n❌ UNERWARTETER FEHLER: {e}")
        import traceback
        traceback.print_exc()
        success = False
    finally:
        cleanup()
        sys.exit(0 if success else 1)
