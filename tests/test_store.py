import json
from datetime import datetime
from pathlib import Path

import pytest

from reinvent_scribe.store import SessionStore

STARTED = datetime(2026, 12, 1, 8, 30, 0)


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path)


def test_create_builds_dir_and_meta(store, tmp_path):
    s = store.create("KEY001", "Keynote", memo="front row", started_at=STARTED)
    assert s.dir == tmp_path / "2026-12-01" / "1201_0830_KEY001"
    meta = json.loads((s.dir / "meta.json").read_text())
    assert meta["id"] == "KEY001"
    assert meta["title"] == "Keynote"
    assert meta["memo"] == "front row"
    assert meta["started_at"] == "2026-12-01T08:30:00"
    assert meta["ended_at"] is None


def test_sanitizes_session_id(store):
    s = store.create("KEY/0:01", "x", started_at=STARTED)
    assert s.dir.name == "1201_0830_KEY_0_01"


def test_append_segment_and_transcript_text(store):
    s = store.create("KEY001", "Keynote", started_at=STARTED)
    s.append_segment(0.0, "Hello everyone.")
    s.append_segment(65.5, "Welcome to re:Invent.")
    text = s.transcript_text()
    assert "[00:00:00] Hello everyone." in text
    assert "[00:01:05] Welcome to re:Invent." in text
    assert "# [KEY001] Keynote" in text


def test_update_memo_mutates_meta(store):
    s = store.create("KEY001", "Keynote", started_at=STARTED)
    s.update_memo("great demo at the end")
    meta = json.loads((s.dir / "meta.json").read_text())
    assert meta["memo"] == "great demo at the end"


def test_finalize_sets_ended_at(store):
    s = store.create("KEY001", "Keynote", started_at=STARTED)
    s.finalize(ended_at=datetime(2026, 12, 1, 9, 45, 0))
    meta = json.loads((s.dir / "meta.json").read_text())
    assert meta["ended_at"] == "2026-12-01T09:45:00"


def test_list_for_date(store):
    store.create("A1", "one", started_at=STARTED)
    store.create("B2", "two", started_at=datetime(2026, 12, 1, 13, 0, 0))
    store.create("C3", "other day", started_at=datetime(2026, 12, 2, 9, 0, 0))
    sessions = store.list_for_date("2026-12-01")
    assert [s.meta["id"] for s in sessions] == ["A1", "B2"]
    assert store.list_for_date("2026-12-05") == []


def test_append_chat(store):
    s = store.create("A1", "one", started_at=STARTED)
    s.append_chat("user", "What was the new service?")
    s.append_chat("assistant", "It was called Foo.")
    content = (s.dir / "chat.md").read_text()
    assert "**user**: What was the new service?" in content
    assert "**assistant**: It was called Foo." in content
