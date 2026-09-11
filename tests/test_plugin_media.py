import base64
import hashlib
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugin
from plugin.controller import ButtonController


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeBot:
    def __init__(self, events):
        self.events = events

    async def send_message(self, **kwargs):
        self.events.append(("text", kwargs))


class FakeAdapter:
    def __init__(self, events, *, success=True):
        self.events = events
        self.success = success

    async def send_image_file(self, **kwargs):
        self.events.append(("photo", kwargs))
        return SimpleNamespace(success=self.success, error="upload failed" if not self.success else None)


class RaisingAdapter(FakeAdapter):
    async def send_image_file(self, **kwargs):
        self.events.append(("photo", kwargs))
        raise RuntimeError("telegram token=SECRET_VALUE https://example.invalid/auth")


def _payload(path: Path) -> dict[str, object]:
    return {
        "status": "fresh",
        "text": "Сегодня, четверг, 10 сентября 2026, ИБ-261\nНеделя: знаменатель\n\nМатематика",
        "requested_date": "2026-09-10",
        "group": "ИБ-261",
        "cache_version": "verified-publication",
        "screenshot": str(path),
        "screenshot_sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "parity": "знаменатель",
        "checked_at": "2026-09-10T12:34:56+03:00",
        "capture_id": "capture-1",
        "source_url": "https://cchgeu.ru/studentu/onlayn-raspisanie/ИБ-261/2026-09-10/знаменатель",
        "json_sha256": "json-hash",
        "crop_warning": False,
    }


def _week_payload(path: Path) -> dict[str, object]:
    payload = _payload(path)
    payload.update(
        {
            "mode": "week",
            "text": "📅 Расписание ИБ-261 на неделю с 7 по 13 сентября 2026\nНеделя: знаменатель",
            "week_dates": [f"2026-09-{day:02d}" for day in range(7, 14)],
        }
    )
    return payload


@pytest.mark.asyncio
async def test_schedule_result_sends_verified_photo_then_text_with_keyboard(tmp_path, monkeypatch):
    image = tmp_path / "schedule.png"
    image.write_bytes(PNG_BYTES)
    monkeypatch.setattr(plugin, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "_inline_keyboard", lambda: "inline-keyboard")
    events = []
    gate = plugin._RequestGate()
    key = ("telegram", "123", None)
    token = gate.begin(key)

    await plugin._send_result(
        FakeBot(events),
        FakeAdapter(events),
        "123",
        _payload(image),
        date(2026, 9, 10),
        key,
        ButtonController(),
        token,
        gate,
    )

    assert [kind for kind, _ in events] == ["photo", "text"]
    photo = events[0][1]
    assert "ИБ-261" in photo["caption"] and "Знаменатель" in photo["caption"]
    assert photo["metadata"]["schedule_photo_kind"] == "daily_crop"
    assert photo["metadata"]["schedule_photo_sha256"] == hashlib.sha256(PNG_BYTES).hexdigest()
    assert events[1][1]["text"].startswith("Сегодня, четверг, 10 сентября 2026, ИБ-261")
    assert events[1][1]["reply_markup"] == "inline-keyboard"


@pytest.mark.asyncio
async def test_week_route_uses_full_week_payload_for_both_outbound_messages(tmp_path, monkeypatch):
    image = tmp_path / "full-week.png"
    image.write_bytes(PNG_BYTES)
    monkeypatch.setattr(plugin, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "_inline_keyboard", lambda: "inline-keyboard")
    events = []
    gate = plugin._RequestGate()
    key = ("telegram", "123", None)
    token = gate.begin(key)

    await plugin._send_result(
        FakeBot(events), FakeAdapter(events), "123", _week_payload(image),
        date(2026, 9, 11), key, ButtonController(), token, gate, mode="week",
    )

    assert [kind for kind, _ in events] == ["photo", "text"]
    assert events[0][1]["metadata"]["schedule_photo_kind"] == "full_week"
    assert events[1][1]["text"].startswith("📅 Расписание ИБ-261 на неделю")


@pytest.mark.asyncio
async def test_photo_failure_keeps_text_and_explains_failure(tmp_path, monkeypatch):
    image = tmp_path / "schedule.png"
    image.write_bytes(PNG_BYTES)
    monkeypatch.setattr(plugin, "CACHE_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "_inline_keyboard", lambda: "inline-keyboard")
    events = []
    gate = plugin._RequestGate()
    key = ("telegram", "123", None)
    token = gate.begin(key)

    await plugin._send_result(
        FakeBot(events),
        FakeAdapter(events, success=False),
        "123",
        _payload(image),
        date(2026, 9, 10),
        key,
        ButtonController(),
        token,
        gate,
    )

    assert [kind for kind, _ in events] == ["photo", "text", "text"]
    assert events[1][1]["text"] == "Не удалось загрузить изображение расписания"
    assert "Математика" in events[2][1]["text"]


def test_callback_buttons_cover_all_requested_actions():
    assert set(plugin.CALLBACK_TEXT.values()) == {"⬅️ Вчера", "Сегодня", "Завтра", "Неделя", "Обновить", "📅 Другая дата"}
    assert all(name.startswith("ib261:") for name in plugin.CALLBACK_TEXT)


@pytest.mark.asyncio
async def test_photo_error_log_does_not_include_secret_details(tmp_path, monkeypatch, caplog):
    image = tmp_path / "schedule.png"
    image.write_bytes(PNG_BYTES)
    monkeypatch.setattr(plugin, "CACHE_ROOT", tmp_path)
    events = []
    gate = plugin._RequestGate()
    key = ("telegram", "123", None)
    token = gate.begin(key)

    await plugin._send_result(
        FakeBot(events),
        RaisingAdapter(events),
        "123",
        _payload(image),
        date(2026, 9, 10),
        key,
        ButtonController(),
        token,
        gate,
    )

    assert "SECRET_VALUE" not in caplog.text
    assert "https://example.invalid" not in caplog.text


def test_schedule_command_requests_complete_week():
    action = ButtonController().handle("/schedule", ("telegram", "123", None), date(2026, 9, 10))
    assert action is not None
    assert action.kind == "week"
    assert action.mode == "week"
    assert action.target == date(2026, 9, 10)
