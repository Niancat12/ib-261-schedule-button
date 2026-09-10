from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date
from pathlib import Path

from ib261_schedule.published import validate_snapshot


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("published-schedules")
    json_path = root / "schedule.json"
    png_path = root / "schedule.png"
    if not json_path.is_file() or not png_path.is_file():
        raise SystemExit("published schedule files are missing")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    image = png_path.read_bytes()
    raw_target = os.environ.get("REQUESTED_DATE", "").strip()
    target = date.fromisoformat(raw_target) if raw_target else None
    validate_snapshot(payload, image, target=target)
    print(f"{json_path} {json_path.stat().st_size} bytes sha256={hashlib.sha256(json_path.read_bytes()).hexdigest()}")
    print(f"{png_path} {png_path.stat().st_size} bytes sha256={hashlib.sha256(image).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
