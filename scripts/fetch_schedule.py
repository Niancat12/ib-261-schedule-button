from __future__ import annotations

import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ib261_schedule.browser_capture import capture_live
from ib261_schedule.published import publish_snapshot
from ib261_schedule.source import SOURCE_URL

ROOT = Path(__file__).resolve().parents[1] / "published-schedules"


def main() -> int:
    raw = os.environ.get("REQUESTED_DATE", "").strip()
    target = date.fromisoformat(raw) if raw else datetime.now(ZoneInfo("Europe/Moscow")).date()
    with tempfile.TemporaryDirectory(prefix="ib261-capture-") as temp:
        image = Path(temp) / "capture.png"
        schedule, checked_at = capture_live(target, "ИБ-261", image)
        publish_snapshot(ROOT, schedule, checked_at, SOURCE_URL, image.read_bytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
