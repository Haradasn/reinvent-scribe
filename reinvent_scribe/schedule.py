import csv
import io
import logging
import os
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def load_schedule(path: Path) -> list[dict]:
    """Read sessions.csv (date,start,end,id,title,venue,kind); bad rows are skipped.

    Times are event-local. Keys depend only on date, start and title, so
    re-importing a schedule keeps existing recordings linked.
    """
    path = Path(path)
    if not path.exists():
        return []
    items, seen = [], set()
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            row = {k: (v or "").strip() for k, v in row.items() if isinstance(k, str)
                   and not isinstance(v, list)}
            try:
                begin = datetime.strptime(f"{row['date']} {row['start']}", "%Y-%m-%d %H:%M")
                end = datetime.strptime(f"{row['date']} {row['end']}", "%Y-%m-%d %H:%M")
            except (KeyError, ValueError):
                log.warning("skipping bad schedule row: %s", row)
                continue
            title = row.get("title", "")
            if not title or end <= begin:
                log.warning("skipping bad schedule row: %s", row)
                continue
            key = f"{begin:%Y%m%d%H%M}-" + re.sub(r"\W+", "-", title.lower())[:80]
            if key in seen:
                continue
            seen.add(key)
            items.append({
                "key": key, "date": f"{begin:%Y-%m-%d}", "start": f"{begin:%H:%M}",
                "end": f"{end:%H:%M}", "title": title,
                "id": row.get("id") or f"{_DAYS[begin.weekday()]}{begin:%H%M}",
                "venue": row.get("venue", ""), "kind": row.get("kind") or "personal",
            })
    rank = {"reserved": 0, "favorite": 1}
    return sorted(items, key=lambda i: (i["date"], i["start"], rank.get(i["kind"], 2)))


FIELDS = ["date", "start", "end", "id", "title", "venue", "kind"]


def save_schedule(path: Path, items: list[dict]) -> dict:
    """Replace sessions.csv (keeping a .bak) and report what changed."""
    path = Path(path)
    old = load_schedule(path)
    ids = {(i["date"], i["title"]): i["id"] for i in old
           if not re.fullmatch(r"(MON|TUE|WED|THU|FRI|SAT|SUN)\d{4}", i["id"])}
    buf = io.StringIO()
    w = csv.DictWriter(buf, FIELDS, lineterminator="\n")
    w.writeheader()
    for i in items:
        w.writerow({**i, "id": ids.get((i["date"], i["title"]), "")})
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(buf.getvalue(), encoding="utf-8")
    new = load_schedule(tmp)
    if path.exists():
        os.replace(path, path.with_name(path.name + ".bak"))
    os.replace(tmp, path)

    a, b = ({i["key"]: i for i in x} for x in (old, new))
    return {"total": len(b),
            "added": [b[k]["title"] for k in b if k not in a],
            "removed": [a[k]["title"] for k in a if k not in b],
            "changed": [b[k]["title"] for k in b if k in a and a[k] != b[k]]}
