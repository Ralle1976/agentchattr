"""Tests for MessageStore durability."""

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from store import MessageStore  # noqa: E402


def test_add_calls_fsync(tmp_path):
    """store.add() must call os.fsync to ensure durability on crash."""
    store = MessageStore(str(tmp_path / "chat.jsonl"))
    with patch("os.fsync") as mock_fsync:
        store.add("user", "hello")
    mock_fsync.assert_called_once()


def test_delete_calls_fsync(tmp_path):
    """store.delete() rewrites the JSONL file and must call os.fsync."""
    store = MessageStore(str(tmp_path / "chat.jsonl"))
    msg = store.add("user", "to be deleted")
    # One fsync for add, reset mock for delete check
    with patch("os.fsync") as mock_fsync:
        store.delete([msg["id"]])
    mock_fsync.assert_called_once()
