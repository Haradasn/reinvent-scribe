import smtplib
from datetime import datetime

from fastapi.testclient import TestClient

import reinvent_scribe.server as server
from reinvent_scribe.config import Settings
from reinvent_scribe.mail import markdown_to_html
from tests.test_server import (FakeLLM, FakeMic, FakeTranscriber,
                               fake_transcribe_file)


def client(tmp_path, sender=None, **kw):
    return TestClient(server.create_app(
        settings=Settings(data_dir=tmp_path, **kw), mic_factory=FakeMic,
        transcriber_factory=FakeTranscriber, transcribe_file_fn=fake_transcribe_file,
        llm=FakeLLM(), model_name="x", streamer_factory=None, mail_sender=sender,
        event_now=lambda: datetime(2026, 11, 25)))


GMAIL = dict(gmail_address="me@example.com", gmail_app_password="pw")


def record(c):
    d = c.post("/api/sessions", json={"session_id": "K", "title": "t"}).json()
    c.post("/api/sessions/stop")
    return d["date"], d["dirname"]


def test_downloads(tmp_path):
    with client(tmp_path) as c:
        d = c.post("/api/sessions", json={"session_id": "K", "title": "t"}).json()
        url = f"/api/sessions/{d['date']}/{d['dirname']}"
        assert c.get(url + "/audio").status_code == 409
        c.post("/api/sessions/stop")
        r = c.get(url + "/audio")
        assert r.content[:4] == b"RIFF" and "attachment" in r.headers["content-disposition"]
        r = c.get(url + "/transcript.txt")
        assert r.content.startswith(b"\xef\xbb\xbf") and "hello from fake" in r.text


def test_send_mail_with_transcript(tmp_path):
    sent = []
    with client(tmp_path, lambda a, p, m: sent.append(m), **GMAIL) as c:
        date, dirname = record(c)
        r = c.post(f"/api/daily-report/send?date={date}", json={
            "to": "a@example.com, b@example.com", "subject": "x\ny", "body": "# 日報",
            "attach_transcript": True})
        assert r.json() == {"ok": True, "attachments": 1}
        m = sent[0]
        assert m["To"] == "a@example.com, b@example.com" and m["Subject"] == "x y"
        assert [p.get_filename() for p in m.iter_attachments()] == [dirname + ".txt"]
        bad = c.post(f"/api/daily-report/send?date={date}",
                     json={"to": "nope", "subject": "", "body": "x"})
        assert bad.status_code == 422 and len(sent) == 1


def test_mail_errors(tmp_path):
    def deny(a, p, m):
        raise smtplib.SMTPAuthenticationError(535, b"no")
    body = {"to": "a@example.com", "subject": "s", "body": "x"}
    with client(tmp_path, deny, **GMAIL) as c:
        r = c.post("/api/daily-report/send?date=2026-11-25", json=body)
        assert r.status_code == 502 and "アプリパスワード" in r.json()["error"]
    with client(tmp_path, deny) as c:
        assert c.post("/api/daily-report/send?date=2026-11-25", json=body).status_code == 503
        assert c.get("/api/status", headers={"host": "evil.example"}).status_code == 400


def test_markdown_html_escapes_and_renders_table():
    h = markdown_to_html("# T\n| a | b |\n| --- | --- |\n| <x> | **y** |\n- item")
    assert "<h2>T</h2>" in h and "<td>&lt;x&gt;</td><td><b>y</b></td>" in h and "<li>item</li>" in h


def item(**kw):
    return {"month": 11, "day": 30, "start": "10:00", "end": "11:00", "title": "A, b",
            "venue": "MGM", "kind": "reserved", **kw}


def test_import_schedule(tmp_path, monkeypatch):
    path = tmp_path / "sessions.csv"
    monkeypatch.setattr(server, "SCHEDULE_CSV", path)
    with client(tmp_path) as c:
        post = lambda items, **kw: c.post("/api/schedule/import", json={"items": items, **kw})
        assert post([item(), item(title="B"), item(title="C")]).json()["total"] == 3
        d = post([item(venue="Wynn"), item(title="B"), item(title="D")]).json()
        assert (d["added"], d["removed"], d["changed"]) == (["D"], ["C"], ["A, b"])
        assert post([]).status_code == 409
        assert post([item()]).json()["confirm"] is True
        assert post([item(end="09:00")] * 3).status_code == 422
        assert len(c.get("/api/schedule").json()["items"]) == 3
        assert post([item()], force=True).json()["total"] == 1
    assert (tmp_path / "sessions.csv.bak").exists()


def test_gmail_hangup_during_login_is_an_auth_error(monkeypatch):
    import pytest
    from reinvent_scribe import mail

    class Smtp:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def login(self, *a): raise smtplib.SMTPServerDisconnected("closed")

    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", Smtp)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        mail.send_gmail("a@example.com", "pw", None)
