import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path


_meta_lock = threading.RLock()


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
               started_at: datetime, extra: dict | None = None) -> "Session":
        dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "id": session_id,
            "title": title,
            "memo": memo,
            "started_at": started_at.isoformat(),
            "ended_at": None,
            **(extra or {}),
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
        tmp = self.dir / "meta.tmp"
        tmp.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / "meta.json")

    def update_meta(self, **values) -> None:
        """Merge into the saved file: a memo edited while recording survives."""
        with _meta_lock:
            try:
                self.meta = Session.load(self.dir).meta
            except (OSError, ValueError):
                pass
            self.meta.update(values)
            self._save_meta()

    def append_segment(self, ts_sec: float, text: str) -> None:
        with open(self.dir / "transcript.md", "a", encoding="utf-8") as f:
            f.write(f"[{_fmt_ts(ts_sec)}] {text}\n")

    def update_memo(self, memo: str) -> None:
        self.update_meta(memo=memo)

    # ---------- notes: the listener's own lines, placed on the audio timeline ----------
    def notes(self) -> list[dict]:
        path = self.dir / "notes.json"
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [n for n in items if isinstance(n, dict) and n.get("text")]

    def _save_notes(self, items: list[dict]) -> None:
        tmp = self.dir / "notes.tmp"
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.dir / "notes.json")

    def add_note(self, text: str, at: float) -> dict:
        note = {"id": uuid.uuid4().hex[:12], "at": at, "text": text}
        with _meta_lock:
            self._save_notes([*self.notes(), note])
        return note

    def delete_note(self, nid: str) -> bool:
        with _meta_lock:
            items = self.notes()
            kept = [n for n in items if n.get("id") != nid]
            if len(kept) == len(items):
                return False
            self._save_notes(kept)
            return True

    def notes_on_timeline(self, live: bool = False) -> list[dict]:
        """Each note with its place in the audio (a note written later goes last).

        live: this is the recording in progress.
        """
        from .photos import position
        out = [{"id": n["id"], "text": n["text"], "pos": round(position(self, n["at"], live), 1)}
               for n in self.notes()]
        return sorted(out, key=lambda n: n["pos"])

    def notes_text(self) -> str:
        lines = [f"[{_fmt_ts(n['pos'])}] {n['text']}" for n in self.notes_on_timeline()]
        return ("\n\n## 聴講者のメモ(発言ではない)\n" + "\n".join(lines)) if lines else ""

    def impressions(self) -> list[str]:
        """The memo and the notes: what the listener thought, for the report."""
        out = [self.meta["memo"]] if self.meta.get("memo") else []
        return out + [n["text"] for n in self.notes_on_timeline()]

    def finalize(self, ended_at: datetime | None = None) -> None:
        ended = ended_at or datetime.now()
        self.update_meta(ended_at=ended.isoformat(), ended_epoch=ended.timestamp())

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

    def summary_export(self) -> str:
        return self._export_head() + "\n\n" + self.summary_text().strip() + "\n"

    def _export_head(self) -> str:
        m = self.meta
        head = [f"[{m['id']}] {m['title']}", f"日時: {m['started_at'][:16].replace('T', ' ')}"]
        if m.get("venue"):
            head.append(f"会場: {m['venue']}")
        return "\n".join(head)

    def transcript_export(self) -> str:
        """Plain text that opens anywhere: English with its translation."""
        rows = [(s["ts"], 0, f"[{_fmt_ts(s['ts'])}]\n{s['text']}"
                 + (f"\n{s['ja']}" if s["ja"] else "")) for s in self.segments()]
        rows += [(n["pos"], 1, f"[{_fmt_ts(n['pos'])}] メモ: {n['text']}")
                 for n in self.notes_on_timeline()]
        blocks = [self._export_head()] + [r[2] for r in sorted(rows, key=lambda r: r[:2])]
        return "\n\n".join(blocks) + "\n"

    def best_transcript(self) -> str:
        live = "\n".join(f"[{_fmt_ts(s['ts'])}] {s['text']}" for s in self.segments())
        path = self.dir / "transcript_hq.md"
        if not path.exists():
            return live or self.transcript_text()
        hq = path.read_text(encoding="utf-8")
        # the second pass can drop sentences when the audio is poor
        n = len(live.split())
        return live if n >= 100 and len(hq.split()) < n * 0.9 else hq

    def summary_text(self) -> str:
        path = self.dir / "summary.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def chat_history(self) -> list[dict]:
        path = self.dir / "chat.md"
        if not path.exists():
            return []
        parts = re.split(r"^\*\*(user|assistant|web)\*\*: ",
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
               started_at: datetime | None = None,
               extra: dict | None = None) -> Session:
        started_at = started_at or datetime.now()
        day = self.data_dir / f"{started_at:%Y-%m-%d}"
        base = f"{started_at:%m%d_%H%M}_{_sanitize(session_id)}"
        dir, n = day / base, 2
        while dir.exists():  # never overwrite an earlier recording
            dir, n = day / f"{base}-{n}", n + 1
        return Session.create(dir, session_id, title, memo, started_at, extra)

    def all(self) -> list[Session]:
        out = []
        for path in self.data_dir.glob("*/*/meta.json"):
            try:
                out.append(Session.load(path.parent))
            except (ValueError, OSError):
                continue  # e.g. being deleted or half-written
        return sorted(out, key=lambda s: s.meta["started_at"])

    @staticmethod
    def _free(path: Path) -> Path:
        out, n = path, 2
        while out.exists():
            out, n = path.with_name(f"{path.name}-{n}"), n + 1
        return out

    def trash(self, session: Session) -> Path:
        """Move aside; nothing is lost until it is deleted from the trash."""
        dst = self._free(self.data_dir / "_trash" / session.dir.parent.name / session.dir.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        session.dir.rename(dst)
        return dst

    def restore(self, session: Session) -> Path:
        dst = self._free(self.data_dir / session.dir.parent.name / session.dir.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        session.dir.rename(dst)
        return dst

    def trashed(self) -> list[Session]:
        out = []
        for path in self.data_dir.glob("_trash/*/*/meta.json"):
            try:
                out.append(Session.load(path.parent))
            except (ValueError, OSError):
                continue
        return sorted(out, key=lambda s: s.meta["started_at"], reverse=True)

    def list_for_date(self, date: str) -> list[Session]:
        """Recordings of that event day (linked schedule date wins)."""
        return [s for s in self.all()
                if (s.meta.get("schedule_date") or s.dir.parent.name) == date]

    def get(self, date: str, dirname: str) -> Session:
        return Session.load(self.data_dir / date / dirname)
