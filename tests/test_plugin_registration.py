import asyncio
import base64
import hashlib
import os
import signal
from contextlib import suppress
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugin import (
    _moscow_date,
    _open_verified_screenshot,
    _RequestGate,
    _run_worker,
    _validate_worker_payload,
    _worker_env,
    register,
)

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeState:
    def get(self, key, default=None):
        return default

    def set(self, key, value):
        pass


class FakePlatform(Enum):
    TELEGRAM = "telegram"


class FakeContext:
    def __init__(self):
        self.hooks = {}
        self.factories = {}
        self.commands = {}
        self.spawned = []
        self.state = FakeState()

    def get_config(self, key, default=None):
        return {"chat_id": "1000593689"}.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_platform_handler(self, platform, factory):
        self.factories[platform] = factory

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = handler

    def spawn_task(self, coro, **kwargs):
        self.spawned.append(coro)
        return None


def event(text, *, chat_id="1000593689", platform="telegram", chat_type="dm"):
    source = SimpleNamespace(
        platform=platform,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id="1000593689",
        thread_id=None,
    )
    return SimpleNamespace(text=text, source=source, user_id="1000593689")


def test_register_uses_public_plugin_surfaces():
    ctx = FakeContext()
    register(ctx)
    assert "pre_gateway_dispatch" in ctx.hooks
    assert "telegram" in ctx.factories
    assert "ib261" in ctx.commands


def test_hook_ignores_non_target_chats_and_non_telegram():
    ctx = FakeContext()
    register(ctx)
    hook = ctx.hooks["pre_gateway_dispatch"]
    assert hook(event("📅 Расписание ИБ-261", chat_id="999")) is None
    assert hook(event("📅 Расписание ИБ-261", platform="discord")) is None
    assert ctx.spawned == []


def test_hook_skips_llm_and_spawns_supervised_schedule_task_for_button():
    ctx = FakeContext()
    register(ctx)
    ctx.factories["telegram"](SimpleNamespace(bot=SimpleNamespace()), None)
    for coro in ctx.spawned:
        coro.close()
    ctx.spawned.clear()
    result = ctx.hooks["pre_gateway_dispatch"](event("📅 Расписание ИБ-261"))
    try:
        assert result == {"action": "skip", "reason": "ib261-button"}
        assert len(ctx.spawned) == 1
    finally:
        for coro in ctx.spawned:
            coro.close()


def test_hook_accepts_hermes_platform_enum_for_schedule_command():
    ctx = FakeContext()
    register(ctx)
    ctx.factories["telegram"](SimpleNamespace(bot=SimpleNamespace()), None)
    source = SimpleNamespace(
        platform=FakePlatform.TELEGRAM,
        chat_id="1000593689",
        chat_type="dm",
        user_id="1000593689",
        thread_id=None,
    )
    event_value = SimpleNamespace(text="/schedule", source=source, user_id="1000593689")
    result = ctx.hooks["pre_gateway_dispatch"](event_value)
    try:
        assert result == {"action": "skip", "reason": "ib261-button"}
        assert len(ctx.spawned) == 1
    finally:
        for coro in ctx.spawned:
            coro.close()


def test_hook_does_not_capture_arbitrary_date_without_pending_prompt():
    ctx = FakeContext()
    register(ctx)
    assert ctx.hooks["pre_gateway_dispatch"](event("15.10.2026")) is None


def test_worker_environment_does_not_forward_gateway_secrets(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    env = _worker_env()
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["PYTHONPATH"].endswith("/src")


def test_moscow_date_advances_at_moscow_midnight():
    instant = datetime(2026, 9, 7, 21, 30, tzinfo=UTC)
    assert _moscow_date(instant).isoformat() == "2026-09-08"


def valid_payload(tmp_path: Path) -> dict[str, object]:
    image = tmp_path / "schedule.png"
    image.write_bytes(PNG_BYTES)
    return {
        "status": "fresh",
        "text": "schedule",
        "requested_date": "2026-09-08",
        "group": "ИБ-261",
        "cache_version": "version-1",
        "screenshot": str(image),
        "screenshot_sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
        "parity": "знаменатель",
        "capture_id": "version-1",
        "checked_at": "2026-09-08T09:01:00+03:00",
        "source_url": "https://cchgeu.ru/studentu/onlayn-raspisanie/%D0%98%D0%B1-261/2026-09-08/%D0%B7%D0%BD%D0%B0%D0%BC%D0%B5%D0%BD%D0%B0%D1%82%D0%B5%D0%BB%D1%8C",
        "json_sha256": "payload-hash",
        "crop_warning": False,
    }


def test_worker_payload_validator_is_dict_only_exact_and_status_aware(tmp_path: Path, monkeypatch):
    import plugin

    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    monkeypatch.setattr(plugin, "CACHE_ROOT", cache_root)
    payload = valid_payload(cache_root)
    assert _validate_worker_payload(payload, date(2026, 9, 8)) == payload

    for invalid in (
        [],
        {**payload, "extra": True},
        {**payload, "status": "ok"},
        {**payload, "requested_date": "2026-09-09"},
        {**payload, "group": "ИБ261"},
        {**payload, "screenshot_sha256": "bad"},
        {
            **payload,
            "status": "unavailable",
            "cache_version": None,
            "screenshot_sha256": None,
        },
    ):
        with pytest.raises(ValueError):
            _validate_worker_payload(invalid, date(2026, 9, 8))

    unavailable = {
        **payload,
        "status": "unavailable",
        "cache_version": None,
        "screenshot": None,
        "screenshot_sha256": None,
        "parity": None,
        "capture_id": None,
        "checked_at": None,
        "source_url": None,
        "json_sha256": None,
    }
    assert _validate_worker_payload(unavailable, date(2026, 9, 8)) == unavailable


def test_verified_screenshot_upload_uses_trusted_fd_and_rejects_symlinks(tmp_path: Path):
    root = tmp_path / "cache"
    version = root / "ИБ-261" / "2026-09-08" / "version-1"
    version.mkdir(parents=True)
    image = version / "schedule.png"
    image.write_bytes(PNG_BYTES)
    digest = hashlib.sha256(PNG_BYTES).hexdigest()

    with _open_verified_screenshot(root, image, digest) as trusted:
        moved = version / "original.png"
        image.replace(moved)
        image.symlink_to(tmp_path / "outside.png")
        assert trusted.read() == PNG_BYTES

    with pytest.raises(ValueError), _open_verified_screenshot(root, image, digest):
        pass


class HangingProcess:
    pid = 32123

    def __init__(self):
        self.returncode = None
        self.finished = asyncio.Event()

    async def communicate(self):
        await asyncio.Event().wait()

    async def wait(self):
        await self.finished.wait()
        return self.returncode


def test_worker_process_group_is_cleaned_on_timeout(monkeypatch):
    process = HangingProcess()
    launch: dict[str, object] = {}
    signals: list[tuple[int, signal.Signals]] = []

    async def create(*args, **kwargs):
        launch.update(kwargs)
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -int(sig)
        process.finished.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(os, "killpg", killpg)
    payload = asyncio.run(_run_worker(date(2026, 9, 8), timeout=0.01))

    assert launch["start_new_session"] is True
    assert signals == [(process.pid, signal.SIGTERM)]
    assert payload["status"] == "unavailable"


def test_worker_process_group_is_cleaned_on_cancellation(monkeypatch):
    process = HangingProcess()
    signals: list[tuple[int, signal.Signals]] = []

    async def create(*args, **kwargs):
        return process

    def killpg(pid, sig):
        signals.append((pid, sig))
        process.returncode = -int(sig)
        process.finished.set()

    async def scenario():
        task = asyncio.create_task(_run_worker(date(2026, 9, 8), timeout=60))
        await asyncio.sleep(0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    monkeypatch.setattr(os, "killpg", killpg)
    asyncio.run(scenario())
    assert signals == [(process.pid, signal.SIGTERM)]


def test_request_gate_cancels_previous_task_and_rejects_old_completion():
    gate = _RequestGate()

    class Task:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    key = ("chat", "user", None)
    first = Task()
    first_token = gate.begin(key)
    gate.attach(key, first_token, first)
    second_token = gate.begin(key)

    assert first.cancelled is True
    assert gate.is_current(key, first_token) is False
    assert gate.is_current(key, second_token) is True
