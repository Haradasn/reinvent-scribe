import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = Path("data")
    whisper_model: str = "small.en"
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    summary_interval_sec: int = 180

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_dir=Path(os.environ.get("DATA_DIR", "data")),
            whisper_model=os.environ.get("WHISPER_MODEL", "small.en"),
            llm_base_url=os.environ.get("LLM_BASE_URL"),
            llm_api_key=os.environ.get("LLM_API_KEY"),
            llm_model=os.environ.get("LLM_MODEL"),
            summary_interval_sec=int(os.environ.get("SUMMARY_INTERVAL_SEC", "180")),
        )
