import json
import re
from datetime import datetime
from pathlib import Path


def _sanitize(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", session_id)


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


class Session:
    def __init__(self, dir: Path, meta: dict):
        self.dir = dir
        self.meta = meta

    @classmethod
    def create(cls, dir: Path, session_id: str, title: str, memo: str,
               started_at: datetime) -> "Session":
        dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": session_id,
            "title": title,
            "memo": memo,
            "started_at": started_at.isoformat(),
            "ended_at": None,
        }
        session = cls(dir, meta)
        session._save_meta()
        header = f"# [{session_id}] {title}\n\n- 開始: {started_at.isoformat()}\n\n"
        (dir / "transcript.md").write_text(header, encoding="utf-8")
        return session

    @classmethod
    def load(cls, dir: Path) -> "Session":
        meta = json.loads((dir / "meta.json").read_text(encoding="utf-8"))
        return cls(dir, meta)

    def _save_meta(self) -> None:
        (self.dir / "meta.json").write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_segment(self, ts_sec: float, text: str) -> None:
        with open(self.dir / "transcript.md", "a", encoding="utf-8") as f:
            f.write(f"[{_fmt_ts(ts_sec)}] {text}\n")

    def update_memo(self, memo: str) -> None:
        self.meta["memo"] = memo
        self._save_meta()

    def finalize(self, ended_at: datetime | None = None) -> None:
        self.meta["ended_at"] = (ended_at or datetime.now()).isoformat()
        self._save_meta()

    def transcript_text(self) -> str:
        path = self.dir / "transcript.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def segments(self) -> list[dict]:
        """Realtime captions with their translation, for display."""
        out: list[dict] = []
        for line in self.transcript_text().splitlines():
            m = re.match(r"\[(\d+):(\d\d):(\d\d)\] (.*)", line)
            if m:
                ts = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
                if m[4].startswith("(JA) "):
                    # translations arrive in order: first untranslated match
                    hit = next((s for s in out if s["ts"] == ts and not s["ja"]), None)
                    if hit:
                        hit["ja"] = m[4][5:]
                else:
                    out.append({"ts": ts, "text": m[4], "ja": ""})
            elif out and line.strip():  # continuation of a multi-line translation
                target = next((s for s in reversed(out) if s["ja"]), None)
                if target:
                    target["ja"] += "\n" + line
        return out

    def best_transcript(self) -> str:
        hq = self.dir / "transcript_hq.md"
        return hq.read_text(encoding="utf-8") if hq.exists() else self.transcript_text()

    def summary_text(self) -> str:
        path = self.dir / "summary.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def chat_history(self) -> list[dict]:
        path = self.dir / "chat.md"
        if not path.exists():
            return []
        parts = re.split(r"^\*\*(user|assistant)\*\*: ",
                         path.read_text(encoding="utf-8"), flags=re.M)
        return [{"role": r, "content": t.strip()}
                for r, t in zip(parts[1::2], parts[2::2])]

    def append_chat(self, role: str, text: str) -> None:
        with open(self.dir / "chat.md", "a", encoding="utf-8") as f:
            f.write(f"**{role}**: {text}\n\n")


class SessionStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def create(self, session_id: str, title: str, memo: str = "",
               started_at: datetime | None = None) -> Session:
        started_at = started_at or datetime.now()
        dirname = f"{started_at:%m%d_%H%M}_{_sanitize(session_id)}"
        dir = self.data_dir / f"{started_at:%Y-%m-%d}" / dirname
        return Session.create(dir, session_id, title, memo, started_at)

    def list_for_date(self, date: str) -> list[Session]:
        day_dir = self.data_dir / date
        if not day_dir.is_dir():
            return []
        sessions = [Session.load(d) for d in sorted(day_dir.iterdir())
                    if (d / "meta.json").exists()]
        return sessions

    def get(self, date: str, dirname: str) -> Session:
        return Session.load(self.data_dir / date / dirname)
