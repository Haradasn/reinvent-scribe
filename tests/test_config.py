from pathlib import Path

from reinvent_scribe.config import Settings


def test_from_env_defaults(monkeypatch):
    for var in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "DATA_DIR",
                "WHISPER_MODEL", "SUMMARY_INTERVAL_SEC"):
        monkeypatch.delenv(var, raising=False)
    s = Settings.from_env()
    assert s.data_dir == Path("data")
    assert s.whisper_model == "small.en"
    assert s.llm_base_url is None
    assert s.llm_api_key is None
    assert s.llm_model is None
    assert s.summary_interval_sec == 180


def test_from_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_MODEL", "gpt-4o-mini")
    monkeypatch.setenv("DATA_DIR", "/tmp/scribe-data")
    monkeypatch.setenv("WHISPER_MODEL", "base.en")
    monkeypatch.setenv("SUMMARY_INTERVAL_SEC", "60")
    s = Settings.from_env()
    assert s.llm_base_url == "https://api.example.com/v1"
    assert s.llm_api_key == "sk-test"
    assert s.llm_model == "gpt-4o-mini"
    assert s.data_dir == Path("/tmp/scribe-data")
    assert s.whisper_model == "base.en"
    assert s.summary_interval_sec == 60
