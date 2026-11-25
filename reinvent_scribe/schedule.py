import csv
import logging
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
