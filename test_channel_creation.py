#!/usr/bin/env python3
"""Test Channel-Erstellung für Orchestrator v3.

Usage:
    cd ~/agentchattr && .venv/bin/python test_channel_creation.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from orchestrator.chat_bridge import ChatBridge


def test_channel_creation():
    """Teste Channel-Erstellung mit aktuellem Server."""

    print("\n=== Channel Creation Test ===\n")

    # 1. Check if server is running
    bridge = ChatBridge(port=8300)
    if not bridge.is_server_running():
        print("❌ Server is NOT running. Starte ihn zuerst:")
        print("   cd ~/agentchattr && .venv/bin/python run.py &")
        return False

    print("✓ Server is running")

    # 2. Get current status
    status = bridge.get_status()
    current_channels = status.get("channels", [])
    print(f"✓ Current channels: {current_channels}")

    # 3. Create a test channel
    test_channel = "test-orch-creation"
    print(f"\n→ Creating channel #{test_channel}...")

    if test_channel in current_channels:
        print(f"⚠ Channel #{test_channel} already exists, deleting first...")
        # Note: Wir haben keine delete_channel Methode, also manuell
        import json
        settings_file = ROOT / "data" / "settings.json"
        if settings_file.exists():
            settings = json.loads(settings_file.read_text())
            if test_channel in settings.get("channels", []):
                settings["channels"].remove(test_channel)
                settings_file.write_text(json.dumps(settings, indent=2))
                print(f"✓ Deleted existing channel")
                time.sleep(1)

    # Create
    success = bridge.create_channel(test_channel)
    if not success:
        print(f"❌ Failed to create channel #{test_channel}")
        return False

    print(f"✓ create_channel() returned True")

    # 4. Verify it exists
    time.sleep(1)  # Give it time to propagate
    exists = bridge.verify_channel_exists(test_channel)
    if not exists:
        print(f"❌ Channel #{test_channel} not found after creation!")
        return False

    print(f"✓ Channel #{test_channel} verified")

    # 5. Check it's in the status
    status_after = bridge.get_status()
    channels_after = status_after.get("channels", [])
    if test_channel not in channels_after:
        print(f"❌ Channel #{test_channel} not in status.channels!")
        print(f"   Status channels: {channels_after}")
        return False

    print(f"✓ Channel #{test_channel} in status: {channels_after}")

    # 6. Cleanup
    print(f"\n→ Cleaning up...")
    settings_file = ROOT / "data" / "settings.json"
    if settings_file.exists():
        settings = json.loads(settings_file.read_text())
        if test_channel in settings.get("channels", []):
            settings["channels"].remove(test_channel)
            settings_file.write_text(json.dumps(settings, indent=2))
            print(f"✓ Cleaned up test channel")

    print("\n=== ALL TESTS PASSED ✓ ===\n")
    return True


if __name__ == "__main__":
    try:
        success = test_channel_creation()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
