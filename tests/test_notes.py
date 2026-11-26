import zipfile
import io

from reinvent_scribe.store import SessionStore
from reinvent_scribe.summarize import build_daily_report, build_slack_post
from tests.test_photos import client, rec  # noqa: F401  (fixture)
from tests.test_summarize import FakeLLM


def test_note_placed_on_audio_time_skipping_pauses(tmp_path):
    s = rec(tmp_path, 1000, 1600, pauses=[[1100, 1200]])
    s.add_note("あとで調べる", 1300)
    s.add_note("終わってから", 9999)  # written after the talk: goes last
    s.add_note("最初", 1010)
    assert [(n["text"], n["pos"]) for n in s.notes_on_timeline()] == [
        ("最初", 10), ("あとで調べる", 200), ("終わってから", 500)]
    assert "[00:03:20] あとで調べる" in s.notes_text()


def test_delete_note(tmp_path):
    s = rec(tmp_path, 1000, 1600)
    n = s.add_note("x", 1100)
    assert s.delete_note(n["id"]) and s.notes() == []
    assert not s.delete_note(n["id"])


def test_notes_go_to_report_export_and_zip(tmp_path):
    s = rec(tmp_path, 1000, 1600, memo="全体の感想")
    s.append_segment(5, "Hello.")
    s.add_note("料金が下がる", 1100)
    sessions = SessionStore(tmp_path).all()
    md = build_daily_report(sessions, FakeLLM(up=False))
    assert "全体の感想" in md and "料金が下がる" in md
    assert "料金が下がる" in build_slack_post(sessions, FakeLLM(up=False), md, "")
    text = s.transcript_export()
    assert text.index("Hello.") < text.index("[00:01:40] メモ: 料金が下がる")
    from reinvent_scribe.photos import PhotoStore
    z = zipfile.ZipFile(io.BytesIO(PhotoStore(tmp_path, "UTC").article_zip(s)))
    assert "**メモ**: 料金が下がる" in z.read("article_source.md").decode()


def test_note_api_while_recording(client):  # noqa: F811
    d = client.post("/api/sessions", json={"session_id": "K", "title": "t"}).json()
    base = f"/api/sessions/{d['date']}/{d['dirname']}"
    assert client.post(base + "/notes", json={"text": "  "}).status_code == 400
    assert client.post(base + "/notes", json={"text": "x" * 2001}).status_code == 400
    n = client.post(base + "/notes", json={"text": "重要"}).json()
    assert n["text"] == "重要" and n["pos"] >= 0
    client.post("/api/sessions/stop")
    assert [x["text"] for x in client.get(base).json()["notes"]] == ["重要"]
    assert client.delete(f"{base}/notes/{n['id']}").status_code == 200
    assert client.delete(f"{base}/notes/{n['id']}").status_code == 404
    assert client.delete(f"{base}/notes/..%2Fmeta").status_code == 404
    assert client.get(base).json()["notes"] == []
