import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader; real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Settings:
    data_dir: Path = Path("data")
    whisper_model: str = "small.en"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    assemblyai_api_key: str | None = None
    summary_interval_sec: int = 180
    event_tz: str = "America/Los_Angeles"

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(Path(".env"))
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            whisper_model=os.environ.get("WHISPER_MODEL", "small.en"),
            llm_base_url=os.environ.get("LLM_BASE_URL"),
            llm_api_key=os.environ.get("LLM_API_KEY"),
            llm_model=os.environ.get("LLM_MODEL"),
            assemblyai_api_key=os.environ.get("ASSEMBLYAI_API_KEY"),
            event_tz=os.environ.get("EVENT_TZ", "America/Los_Angeles"),
            summary_interval_sec=int(os.environ.get("SUMMARY_INTERVAL_SEC", "180")),
        )
