import io
import zipfile
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from reinvent_scribe import photos as ph
from reinvent_scribe.config import Settings
from reinvent_scribe.photos import PhotoStore, position, suggest
from reinvent_scribe.server import create_app
from reinvent_scribe.store import SessionStore
from tests.test_server import (FakeMic, FakeTranscriber, fake_transcribe_file)


def image(taken=None, offset=None, fmt="JPEG", color="white", gps=True):
    img = Image.new("RGB", (60, 40), color)
    exif = Image.Exif()
    if taken:
        sub = exif.get_ifd(0x8769)
        sub[36867] = taken
        if offset:
            sub[36881] = offset
    if gps:
        exif.get_ifd(0x8825)[1] = "N"
    buf = io.BytesIO()
    img.save(buf, fmt, exif=exif)
    return buf.getvalue()


def rec(tmp_path, start, end, name="K", **meta):
    s = SessionStore(tmp_path).create(name, "t", started_at=datetime(2026, 11, 30, 10))
    s.update_meta(started_epoch=start, ended_epoch=end, **meta)
    return s


def test_position_skips_pauses(tmp_path):
    s = rec(tmp_path, 1000, 1600, pauses=[[1100, 1200], [1500, None]])
    assert position(s, 1050) == 50
    assert position(s, 1150) == 100  # taken during the pause
    assert position(s, 1300) == 200
    assert position(s, 1590) == 400
    assert position(s, 500) == 0 and position(s, 9999) == 400


def test_suggestion(tmp_path):
    a, b = rec(tmp_path, 1000, 2000, "A"), rec(tmp_path, 2300, 3000, "B")
    pick = lambda t: (lambda r: r and (r["dirname"], r["inside"]))(suggest(t, [a, b]))
    assert pick(1500) == (a.dir.name, True)
    assert pick(2250) == (b.dir.name, False)
    assert pick(9000) is None and pick(None) is None


@pytest.mark.parametrize("fmt", ["JPEG", "HEIF"])
def test_import_reads_time_with_zone_and_strips_gps(tmp_path, fmt):
    if fmt == "HEIF":
        pytest.importorskip("pillow_heif")
    st = PhotoStore(tmp_path, "America/Los_Angeles")
    p = st.add("a.HEIC", image("2026:12:01 09:00:00", "-08:00", fmt))
    assert p["taken"] == datetime(2026, 12, 1, 17, tzinfo=timezone.utc).timestamp()
    assert p["zone_known"]
    with Image.open(st.jpeg(p["id"], st.inbox)) as v:
        assert v.format == "JPEG" and not v.getexif()
    # no zone recorded: event time is assumed
    q = st.add("b.jpg", image("2026:12:01 09:00:00", color="red"))
    assert q["taken"] == p["taken"] and not q["zone_known"]
    assert st.add("c.png", image(fmt="PNG"))["taken"] is None
    with pytest.raises(ValueError):
        st.add("x.jpg", b"not an image")


class LLM:
    last = None

    def available(self):
        return True

    def chat(self, messages, max_tokens=1000):
        LLM.last = messages
        return "売上 10%" if isinstance(messages[0]["content"], list) else "x"


@pytest.fixture
def client(tmp_path):
    app = create_app(settings=Settings(data_dir=tmp_path, gmail_address="a@example.com",
                                       gmail_app_password="x"),
                     mic_factory=FakeMic, transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file, llm=LLM(), model_name="x",
                     streamer_factory=None, mail_sender=lambda a, p, m: sent.append(m))
    with TestClient(app) as c:
        yield c


sent = []


def test_whole_flow(client, tmp_path):
    c = client
    d = c.post("/api/sessions", json={"session_id": "K", "title": "t"}).json()
    c.post("/api/sessions/pause")
    c.post("/api/sessions/resume")
    c.post("/api/sessions/stop")
    base = f"/api/sessions/{d['date']}/{d['dirname']}"
    meta = c.get(base).json()["meta"]
    assert meta["started_epoch"] <= meta["pauses"][0][0] <= meta["pauses"][0][1] <= meta["ended_epoch"]

    now = datetime.now(timezone.utc).replace(microsecond=0)
    data = image(f"{now:%Y:%m:%d %H:%M:%S}", "+00:00")
    pid = c.post("/api/photos/upload?name=../x.jpg", content=data).json()["id"]
    c.post("/api/photos/upload?name=food.jpg", content=image("2020:01:01 00:00:00", color="red"))
    assert c.post("/api/photos/upload", content=b"bad").status_code == 422
    inbox = c.get("/api/photos/inbox").json()
    assert [bool(i["suggest"]) for i in inbox["items"]] == [False, True]
    assert inbox["items"][1]["name"] == "x.jpg"
    assert c.get(f"/api/photos/inbox/{pid}.jpg").status_code == 200

    r = c.post("/api/photos/commit", json={"items": [{"id": pid, "date": d["date"], "dirname": d["dirname"]}]})
    assert r.json() == {"saved": 1}
    c.delete("/api/photos/inbox")
    assert c.get("/api/photos/inbox").json()["items"] == []
    assert c.post("/api/photos/upload", content=data).json() == {"duplicate": True}
    assert [p["id"] for p in c.get(base).json()["photos"]] == [pid]
    assert c.get(f"/api/sessions?date={d['date']}").json()["sessions"][0]["photos"] == 1

    assert c.post(f"{base}/photos/{pid}/read").json()["text"] == "売上 10%"
    c.post(f"{base}/chat", json={"question": "q"})
    assert "売上 10%" in LLM.last[0]["content"]
    z = zipfile.ZipFile(io.BytesIO(c.get(f"{base}/article.zip").content))
    md = z.read("article_source.md").decode()
    assert f"photos/{pid}.jpg" in md and "売上 10%" in md and f"photos/{pid}.jpg" in z.namelist()

    r = c.post(f"/api/daily-report/send?date={d['date']}", json={
        "to": "b@example.com", "subject": "s", "body": "x",
        "photos": [{"date": d["date"], "dirname": d["dirname"], "id": pid}]})
    assert r.json()["attachments"] == 1
    assert next(sent[-1].iter_attachments()).get_content_type() == "image/jpeg"

    assert c.post(f"{base}/photo-shift", json={"seconds": 60}).status_code == 200
    assert c.get(base).json()["meta"]["photo_shift_sec"] == 60
    assert c.get(f"{base}/photos/..%2f..%2fx.jpg").status_code == 404
    d2 = c.post("/api/sessions", json={"session_id": "K2", "title": "t"}).json()
    c.post("/api/sessions/stop")
    c.post(f"{base}/photos/{pid}/move", json={"date": d2["date"], "dirname": d2["dirname"]})
    assert c.get(base).json()["photos"] == []
    base2 = f"/api/sessions/{d2['date']}/{d2['dirname']}"
    assert c.get(base2).json()["photos"][0]["text"] == "売上 10%"
    assert c.delete(f"{base2}/photos/{pid}").status_code == 200
    assert c.get(base2).json()["photos"] == []


def test_memo_edited_while_recording_survives_stop(client):
    d = client.post("/api/sessions", json={"session_id": "K", "title": "t"}).json()
    base = f"/api/sessions/{d['date']}/{d['dirname']}"
    client.patch(base + "/memo", json={"memo": "大事"})
    client.post("/api/sessions/stop")
    meta = client.get(base).json()["meta"]
    assert meta["memo"] == "大事" and meta["ended_at"]


def test_local_takeover_keeps_audio_time(tmp_path):
    class Dies:
        def __init__(self, on_segment, on_partial): self.n = 0
        def start(self): pass
        def feed(self, pcm):
            self.n += 1
            if self.n > 1: raise ConnectionError
        def stop(self): pass

    class Mic(FakeMic):
        def start(self):
            import numpy as np
            self.on_frames(np.zeros(16000 * 5, dtype=np.int16))
            self.on_frames(np.zeros(1600, dtype=np.int16))

    class L(LLM):
        def available(self): return False

    app = create_app(settings=Settings(data_dir=tmp_path), mic_factory=Mic,
                     transcriber_factory=FakeTranscriber, transcribe_file_fn=fake_transcribe_file,
                     llm=L(), model_name="x", streamer_factory=Dies)
    with TestClient(app) as c:
        c.post("/api/sessions", json={"session_id": "K", "title": "t"})
        c.post("/api/sessions/stop")
    assert "[00:00:05] hello from fake" in next(tmp_path.glob("*/*/transcript.md")).read_text()


def test_summary_can_be_rebuilt_with_slides(client):
    d = client.post("/api/sessions", json={"session_id": "K", "title": "t"}).json()
    base = f"/api/sessions/{d['date']}/{d['dirname']}"
    assert client.post(base + "/resummarize").status_code == 409
    client.post("/api/sessions/stop")
    import time
    while client.get("/api/status").json()["hq_pending"]:
        time.sleep(0.02)
    assert client.post(base + "/resummarize").json() == {"summary": "x"}


def test_non_slide_photos_do_not_reach_the_ai_context(tmp_path):
    s = rec(tmp_path, 1000, 1600)
    st = PhotoStore(tmp_path, "America/Los_Angeles")
    p = st.add("a.jpg", image())
    st.move(p["id"], st.inbox, st.folder(s))
    st.set_text(p["id"], st.folder(s), "スライドの情報はありません")
    assert st.slide_text(s) == ""
    st.set_text(p["id"], st.folder(s), "売上 10%")
    assert "売上 10%" in st.slide_text(s)
