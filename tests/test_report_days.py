from tests.test_history_slack import LLM, make, record


def _day(c, date):
    return next(d for d in c.get("/api/daily-reports").json() if d["date"] == date)


def test_days_show_made_and_saved_report_reopens(tmp_path):
    llm = LLM("要約")
    with make(tmp_path, llm) as c:
        url = record(c, "A")
        date = url.split("/")[-2]
        d = _day(c, date)
        assert d["recordings"] == 1 and d["made_at"] is None and d["sent_at"] is None
        assert c.get(f"/api/daily-report?date={date}").status_code == 404

        made = c.post(f"/api/daily-report?date={date}", json={"mention": ""}).json()
        calls = len(llm.calls)
        saved = c.get(f"/api/daily-report?date={date}").json()
        assert saved["markdown"] == made["markdown"] and saved["slack"] == made["slack"]
        assert len(llm.calls) == calls  # reopening does not ask the LLM again
        assert _day(c, date)["made_at"] and not _day(c, date)["stale"]


def test_new_recording_after_report_marks_it_stale(tmp_path):
    with make(tmp_path, LLM("要約")) as c:
        date = record(c, "A").split("/")[-2]
        c.post(f"/api/daily-report?date={date}", json={})
        md = tmp_path / date / "daily_summary.md"
        import os
        os.utime(md, (0, 0))  # made long ago
        record(c, "B")
        assert _day(c, date)["stale"] is True


def test_sent_is_recorded_only_on_success(tmp_path):
    from reinvent_scribe.config import Settings
    from reinvent_scribe.server import create_app
    from fastapi.testclient import TestClient
    from tests.test_server import FakeMic, FakeTranscriber, fake_transcribe_file

    sent = []
    app = create_app(settings=Settings(data_dir=tmp_path, gmail_address="a@example.com",
                                       gmail_app_password="x"),
                     mic_factory=FakeMic, transcriber_factory=FakeTranscriber,
                     transcribe_file_fn=fake_transcribe_file, llm=LLM("要約"),
                     model_name="x", streamer_factory=None,
                     mail_sender=lambda *a: sent.append(a))
    with TestClient(app) as c:
        date = record(c, "A").split("/")[-2]
        r = c.post(f"/api/daily-report/send?date={date}",
                   json={"to": "b@example.com", "subject": "s", "body": ""})
        assert r.status_code == 422 and _day(c, date)["sent_at"] is None
        r = c.post(f"/api/daily-report/send?date={date}",
                   json={"to": "b@example.com", "subject": "s", "body": "本文"})
        assert r.status_code == 200 and sent and _day(c, date)["sent_at"]
