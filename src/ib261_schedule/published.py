from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import zlib

import httpx
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .source import SOURCE_URL

from .schedule import DaySchedule

GROUP = "ИБ-261"
SCHEMA_VERSION = 2
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_valid_png(image: bytes) -> bool:
    """Validate the PNG container enough to reject truncated or corrupt uploads."""
    signature = b"\x89PNG\r\n\x1a\n"
    if not image.startswith(signature):
        return False
    position = len(signature)
    saw_ihdr = saw_idat = saw_iend = False
    while position < len(image):
        if len(image) - position < 12:
            return False
        length = struct.unpack(">I", image[position : position + 4])[0]
        chunk_type = image[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(image):
            return False
        chunk = image[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", image[position + 8 + length : end])[0]
        if zlib.crc32(chunk_type + chunk) & 0xFFFFFFFF != expected_crc:
            return False
        if not saw_ihdr:
            if chunk_type != b"IHDR" or length != 13:
                return False
            width, height = struct.unpack(">II", chunk[:8])
            if not width or not height:
                return False
            saw_ihdr = True
        elif chunk_type == b"IDAT":
            saw_idat = True
        if chunk_type == b"IEND":
            if length != 0 or not saw_ihdr or not saw_idat or end != len(image):
                return False
            saw_iend = True
            break
        position = end
    return saw_ihdr and saw_idat and saw_iend


def validate_snapshot(payload: Any, image: bytes, *, target: date | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("snapshot must be an object")
    required = {
        "schema_version", "group", "requested_date", "actual_date",
        "schedule_date", "weekday", "week_type", "week_number", "parity",
        "lessons", "empty_schedule_confirmed", "checked_at", "source_url",
        "verified", "data_sha256", "screenshot_sha256",
    }
    if set(payload) != required or payload["schema_version"] != SCHEMA_VERSION:
        raise ValueError("snapshot schema mismatch")
    if payload["group"] != GROUP or not payload["verified"]:
        raise ValueError("snapshot group or verification mismatch")
    if not isinstance(payload["source_url"], str):
        raise ValueError("snapshot source URL is invalid")
    parsed_url = urlparse(payload["source_url"])
    if parsed_url.scheme != "https" or parsed_url.hostname != "cchgeu.ru" or parsed_url.path != urlparse(SOURCE_URL).path:
        raise ValueError("snapshot source URL is not official")
    try:
        snapshot_date = date.fromisoformat(payload["schedule_date"])
        requested_date = date.fromisoformat(payload["requested_date"])
        actual_date = date.fromisoformat(payload["actual_date"])
        checked_at = datetime.fromisoformat(payload["checked_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("snapshot dates are invalid") from exc
    if target is not None and (snapshot_date != target or requested_date != target):
        raise ValueError("snapshot date mismatch")
    if actual_date != snapshot_date or requested_date != snapshot_date:
        raise ValueError("snapshot dates disagree")
    expected_weekday = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")[snapshot_date.weekday()]
    if payload["weekday"] != expected_weekday:
        raise ValueError("snapshot weekday is invalid")
    if not isinstance(payload["week_type"], str) or not payload["week_type"]:
        raise ValueError("snapshot week type is invalid")
    if payload["parity"] != payload["week_type"]:
        raise ValueError("snapshot parity mismatch")
    if payload["week_number"] is not None and not isinstance(payload["week_number"], int):
        raise ValueError("snapshot week number is invalid")
    if not isinstance(payload["lessons"], list):
        raise ValueError("snapshot lessons are invalid")
    if not payload["lessons"] and payload["empty_schedule_confirmed"] is not True:
        raise ValueError("snapshot has no verified schedule")
    if not isinstance(payload["empty_schedule_confirmed"], bool):
        raise ValueError("snapshot empty marker is invalid")
    if checked_at.tzinfo is None or checked_at > datetime.now(timezone.utc).astimezone(checked_at.tzinfo) + timedelta(minutes=5):
        raise ValueError("snapshot timestamp is invalid")
    if not image or len(image) > MAX_IMAGE_BYTES or not _is_valid_png(image):
        raise ValueError("screenshot is not a valid PNG")
    if payload["screenshot_sha256"] != _sha256(image):
        raise ValueError("screenshot hash mismatch")
    canonical = json.dumps(
        {k: payload[k] for k in payload if k not in {"data_sha256", "screenshot_sha256"}},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if payload["data_sha256"] != _sha256(canonical):
        raise ValueError("data hash mismatch")
    return payload


def snapshot_payload(
    schedule: DaySchedule, checked_at: datetime, source_url: str, image: bytes
) -> dict[str, Any]:
    lessons = [asdict(lesson) for lesson in schedule.lessons]
    base = {
        "schema_version": SCHEMA_VERSION,
        "group": schedule.group,
        "requested_date": schedule.schedule_date.isoformat(),
        "actual_date": schedule.schedule_date.isoformat(),
        "schedule_date": schedule.schedule_date.isoformat(),
        "weekday": ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")[schedule.schedule_date.weekday()],
        "week_type": schedule.parity,
        "week_number": None,
        "parity": schedule.parity,
        "empty_schedule_confirmed": schedule.empty_confirmed,
        "checked_at": checked_at.isoformat(),
        "source_url": source_url,
        "lessons": lessons,
        "verified": True,
    }
    canonical = json.dumps(base, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {**base, "data_sha256": _sha256(canonical), "screenshot_sha256": _sha256(image)}


def publish_snapshot(
    root: Path, schedule: DaySchedule, checked_at: datetime, source_url: str, image: bytes
) -> bool:
    payload = snapshot_payload(schedule, checked_at, source_url, image)
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("snapshot JSON is too large")
    validate_snapshot(payload, image, target=schedule.schedule_date)
    day_dir = Path(root) / schedule.schedule_date.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    latest = day_dir / "latest.json"
    root_json = Path(root) / "schedule.json"
    root_png = Path(root) / "schedule.png"
    if latest.is_file():
        old = json.loads(latest.read_text(encoding="utf-8"))
        if (
            old.get("data_sha256") == payload["data_sha256"]
            and old.get("screenshot_sha256") == payload["screenshot_sha256"]
            and root_json.is_file()
            and root_png.is_file()
        ):
            return False
    with tempfile.TemporaryDirectory(prefix=".publish-", dir=day_dir) as temp:
        temp_path = Path(temp)
        staged_json = temp_path / "schedule.json"
        staged_png = temp_path / "schedule.png"
        staged_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        staged_png.write_bytes(image)

        final_json = day_dir / "schedule.json"
        final_png = day_dir / "schedule.png"
        staged_pointer = temp_path / "latest.json"
        staged_pointer.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        staged_root_json = temp_path / "root-schedule.json"
        staged_root_png = temp_path / "root-schedule.png"
        staged_root_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        staged_root_png.write_bytes(image)
        backups: list[tuple[Path, Path]] = []

        def backup(path: Path) -> None:
            if path.exists():
                backup_path = temp_path / f"old-{len(backups)}"
                os.replace(path, backup_path)
                backups.append((backup_path, path))

        try:
            # Move the previous complete publication out of the way first. Every
            # subsequent replace is on the same filesystem and can be rolled back.
            backup(final_json)
            backup(final_png)
            backup(latest)
            os.replace(staged_json, final_json)
            os.replace(staged_png, final_png)
            os.replace(staged_pointer, latest)
            backup(root_json)
            backup(root_png)
            os.replace(staged_root_json, root_json)
            os.replace(staged_root_png, root_png)
        except Exception:
            for path in (final_json, final_png, latest):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            for backup_path, original_path in reversed(backups):
                if backup_path.exists():
                    os.replace(backup_path, original_path)
            raise
    return True


def load_latest(
    root: Path, target: date, *, max_age: timedelta | None = None, now: datetime | None = None
) -> tuple[dict[str, Any], bytes, bool]:
    day_dir = Path(root) / target.isoformat()
    payload = json.loads((day_dir / "latest.json").read_text(encoding="utf-8"))
    image = (day_dir / "schedule.png").read_bytes()
    validate_snapshot(payload, image, target=target)
    age_ok = True
    if max_age is not None:
        checked = datetime.fromisoformat(payload["checked_at"])
        current = now or datetime.now(checked.tzinfo)
        age_ok = current - checked <= max_age
    return payload, image, age_ok


def publication_base_url() -> str:
    """Return the trusted GitHub raw publication root."""
    value = os.environ.get(
        "SCHEDULE_PUBLICATION_BASE_URL",
        "https://raw.githubusercontent.com/Niancat12/ib-261-schedule-button/main/published-schedules",
    ).strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"raw.githubusercontent.com", "github.com"} or not parsed.path:
        raise ValueError("Небезопасный адрес публикации расписания")
    return value


def load_remote_snapshot(
    target: date, *, base_url: str | None = None, timeout: float = 8.0
) -> tuple[dict[str, Any], bytes]:
    base = (base_url or publication_base_url()).rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or parsed.hostname not in {"raw.githubusercontent.com", "github.com"}:
        raise ValueError("Небезопасный адрес публикации расписания")
    prefix = f"{base}/{target.isoformat()}"
    with httpx.Client(
        timeout=timeout,
        follow_redirects=False,
        headers={"Accept": "application/json, image/png"},
    ) as client:
        json_response = client.get(f"{prefix}/schedule.json")
        if json_response.status_code != 200 or len(json_response.content) > MAX_JSON_BYTES:
            raise ValueError("Публикация JSON недоступна")
        image_response = client.get(f"{prefix}/schedule.png")
        if image_response.status_code != 200 or len(image_response.content) > MAX_IMAGE_BYTES:
            raise ValueError("Публикация PNG недоступна")
    try:
        payload = json.loads(json_response.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Публикация JSON повреждена") from exc
    validate_snapshot(payload, image_response.content, target=target)
    return payload, image_response.content
