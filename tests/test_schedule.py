from datetime import datetime
import json

import pytest
from fastapi.testclient import TestClient

import reinvent_scribe.server as srv
from reinvent_scribe.config import Settings
from reinvent_scribe.schedule import load_schedule
from reinvent_scribe.summarize import build_daily_report
from reinvent_scribe.store import SessionStore
from tests.test_server import FakeLLM, FakeMic, FakeTranscriber, fake_transcribe_file

CSV = '''date,start,end,id,title,venue,kind
2026-12-01,12:00,13:00,,"Cost, on Bedrock","MGM | 363",reserved
2026-12-01,08:30,10:45,KEY001,CEO Keynote,Venetian,favorite
bad,row,,,x,,
2026-12-01,12:00,13:00,,Clash,,personal
'''


@pytest.fixture
def path(tmp_path, monkeypatch):
    p = tmp_path / "sessions.csv"
    p.write_text(CSV, encoding="utf-8")
    monkeypatch.setattr(srv, "SCHEDULE_CSV", p)
    return p


def client(tmp_path, now):
    return TestClient(srv.create_app(
        settings=Settings(data_dir=tmp_path / "data"), mic_factory=FakeMic,
        transcriber_factory=FakeTranscriber,
        transcribe_file_fn=fake_transcribe_file, llm=FakeLLM(),
        model_name="x", streamer_factory=None, event_now=lambda: now))


def test_load_sorts_skips_bad_rows_and_ids_are_unique(path):
    items = load_schedule(path)
    assert [i["title"] for i in items] == ["CEO Keynote", "Cost, on Bedrock", "Clash"]
    assert items[0]["id"] == "KEY001" and items[1]["id"] == "TUE1200"
    assert len({i["key"] for i in items}) == 3


def test_missing_file_is_empty(tmp_path):
    assert load_schedule(tmp_path / "none.csv") == []


def test_start_from_schedule_links_recording(tmp_path, path):
    with client(tmp_path, datetime(2026, 12, 1, 11, 58)) as c:
        sch = c.get("/api/schedule").json()
        assert sch["today"] == "2026-12-01" and sch["now"].startswith("2026-12-01T11:58")
        item = sch["items"][1]
        r = c.post("/api/sessions", json={"schedule_key": item["key"]})
        assert r.status_code == 200
        c.post("/api/sessions/stop")
        meta = c.get("/api/sessions?date=2026-12-01").json()["sessions"][0]["meta"]
        assert meta["title"] == "Cost, on Bedrock" and meta["venue"] == "MGM | 363"
        assert meta["schedule_key"] == item["key"]
        assert [i["recorded"] for i in c.get("/api/schedule").json()["items"]] == [False, True, False]


def test_recording_on_another_day_is_a_test(tmp_path, path):
    with client(tmp_path, datetime(2026, 11, 25, 12, 0)) as c:
        item = c.get("/api/schedule").json()["items"][1]
        meta = c.post("/api/sessions", json={"schedule_key": item["key"]}).json()["meta"]
        c.post("/api/sessions/stop")
        assert meta["test"] is True and "schedule_key" not in meta
        assert c.get("/api/sessions?date=2026-12-01").json()["sessions"] == []
        assert not any(i["recorded"] for i in c.get("/api/schedule").json()["items"])


def test_unknown_key_404_and_missing_title_422(tmp_path, path):
    with client(tmp_path, datetime(2026, 12, 1)) as c:
        assert c.post("/api/sessions", json={"schedule_key": "nope"}).status_code == 404
        assert c.post("/api/sessions", json={}).status_code == 422
        assert c.get("/api/status").json()["recording"] is False


def test_same_item_twice_in_a_minute_keeps_both(tmp_path):
    st = SessionStore(tmp_path)
    t = datetime(2026, 12, 1, 12, 0)
    a, b = st.create("A", "t", started_at=t), st.create("A", "t", started_at=t)
    assert a.dir != b.dir


def test_report_uses_planned_time_and_venue(tmp_path):
    s = SessionStore(tmp_path).create("TUE1200", "T", started_at=datetime(2026, 12, 2, 5, 1))
    s.meta.update(venue="MGM | 363", planned_start="12:00", planned_end="13:00",
                  schedule_date="2026-12-01")
    md = build_daily_report([s], FakeLLM())
    assert "12:00–13:00" in md and "MGM / 363" in md and "# 出張日報 2026-12-01" in md
