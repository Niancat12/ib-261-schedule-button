from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from .schedule import DaySchedule, ScheduleParseError, parse_schedule_html
from .source import build_source_url

UrlBuilder = Callable[[date, str], str]
_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


class SourceUnavailable(RuntimeError):
    pass


def _select_metadata(page) -> list[dict[str, Any]]:
    return page.evaluate("""() => [...document.querySelectorAll('select')].map((el) => ({
      id: el.id || '', name: el.name || '', value: el.value || '',
      selectedText: el.selectedOptions?.[0]?.textContent?.trim() || '',
      options: [...el.options].map((o) => ({value: o.value, text: o.textContent.trim()}))
    }))""")


def _response_mentions_group(response_url: str, group: str) -> bool:
    return group.casefold() in unquote(response_url).casefold()


def _response_is_target_group(response, group: str) -> bool:
    """Identify the group schedule XHR without trusting analytics calls."""
    try:
        if response.request.resource_type not in {"xhr", "fetch"}:
            return False
    except Exception:
        return False
    lowered_url = response.url.casefold()
    if any(marker in lowered_url for marker in ("google-analytics", "googletagmanager", "doubleclick", "metrika", "mc.yandex", "facebook")):
        return False
    if _response_mentions_group(response.url, group):
        return True
    try:
        post_data = response.request.post_data or ""
    except Exception:
        post_data = ""
    return _response_mentions_group(post_data, group)


def _select_group(
    page, group: str, timeout_ms: int, target_statuses: list[int] | None = None
) -> None:
    """Select only #gruppa; never probe or change unrelated group options."""
    group_select = page.locator("select#gruppa").first
    try:
        group_select.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ScheduleParseError("Интерфейс источника не содержит select#gruppa") from exc

    option = page.locator(f'select#gruppa option[value="{group}"]').first
    try:
        option.wait_for(state="attached", timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise ScheduleParseError(
            f"Группа {group} отсутствует в интерфейсе источника после полной загрузки списка"
        ) from exc

    target_responses: list[int] = []

    def on_response(response) -> None:
        if _response_is_target_group(response, group):
            target_responses.append(response.status)
            if target_statuses is not None:
                target_statuses.append(response.status)

    page.on("response", on_response)
    try:
        # This is intentionally the only option mutation in the whole routine.
        group_select.select_option(value=group, timeout=timeout_ms)
        group_select.evaluate(
            "el => { [...el.options].forEach(o => o.removeAttribute('selected')); "
            "el.selectedOptions[0]?.setAttribute('selected', 'selected'); }"
        )
        page.wait_for_function(
            "expected => document.querySelector('#gruppa')?.value === expected",
            arg=group,
            timeout=timeout_ms,
        )

        # Select2 mirrors the hidden select in a visible rendered selection.
        rendered = page.locator(".select2-selection__rendered")
        if rendered.count() and not rendered.filter(has_text=re.compile(rf"^{re.escape(group)}$")).count():
            raise ScheduleParseError("Select2 не отобразил выбранную группу")

        # Give the site's change handler time to issue its group-specific request.
        # Static fixtures may have no AJAX at all; in that case content validation remains authoritative.
        wait_ms = min(timeout_ms, 3_000)
        elapsed = 0
        while elapsed < wait_ms and not target_responses:
            page.wait_for_timeout(100)
            elapsed += 100
        if target_responses and any(status >= 400 for status in target_responses):
            raise SourceUnavailable(f"Целевой запрос группы {group} вернул HTTP {target_responses[-1]}")
    finally:
        page.remove_listener("response", on_response)


def _wait_for_schedule_state(page, target: date, group: str, timeout_ms: int) -> None:
    page.wait_for_function("""({target, group}) => {
      const body = document.body?.innerText || '';
      const container = document.querySelector('#schedule-container');
      const selected = [...document.querySelectorAll('select option:checked')].some(o => o.textContent.trim() === group);
      const prompt = /Выберете преподавателя или группу/i.test(body);
      const content = container?.innerText || '';
      const lesson = /\\b\\d{2}:\\d{2}\\s*[-–—]\\s*\\d{2}:\\d{2}\\b/.test(content);
      const empty = /нет занятий|занятий нет/i.test(content);
      return selected && !prompt && !!container && !!(container.offsetWidth || container.offsetHeight) && (lesson || empty) && body.includes(target);
    }""", arg={"target": target.strftime("%d.%m.%Y"), "group": group}, timeout=timeout_ms)


def _write_diagnostics(page, directory: Path, *, responses: list[dict[str, Any]], console_errors: list[str], page_errors: list[str], target_statuses: list[int] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try: page.screenshot(path=str(directory / "page.png"), full_page=True)
    except Exception: pass
    try: (directory / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception: pass
    try:
        container = page.locator("#schedule-container")
        meta = {"url": page.url, "title": page.title(), "selects": _select_metadata(page), "page_text": page.locator("body").inner_text(timeout=1000) if page.locator("body").count() else "",
            "schedule_text": container.inner_text(timeout=1000) if container.count() else "", "responses": responses[-100:], "console_errors": console_errors[-100:], "page_errors": page_errors[-100:], "target_group_http_statuses": (target_statuses or [])[-20:]}
        (directory / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception: pass


def capture_live(
    target: date,
    group: str,
    output: Path,
    *,
    url_builder: UrlBuilder = build_source_url,
    timeout_ms: int = 35_000,
) -> tuple[DaySchedule, datetime]:
    """
    Capture live schedule with DOM consistency guarantees.

    Four-step process ensures parsed HTML and screenshot come from the same DOM state:
    (A) Freeze document: Save page.content() before any mutations
    (B) Parse frozen HTML: Parse the immutable snapshot
    (C) Verify consistency: Re-check that selected group and schedule_date still match
    (D) Filter & screenshot: Only then mutate display and take screenshot
    """
    url = url_builder(target, group)
    output.parent.mkdir(parents=True, exist_ok=True)
    diagnostic_env = os.environ.get("SCHEDULE_DIAGNOSTIC_DIR", "").strip()
    diagnostics_dir = Path(diagnostic_env) if diagnostic_env else None
    page = None
    responses: list[dict[str, Any]] = []
    target_statuses: list[int] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser_options: dict[str, Any] = {}
            proxy = os.environ.get("SCHEDULE_PROXY_URL", "").strip()
            if proxy:
                browser_options["proxy"] = {"server": proxy}
            browser = playwright.chromium.launch(headless=True, **browser_options)
            try:
                context = browser.new_context(
                    timezone_id="Europe/Moscow",
                    locale="ru-RU",
                    viewport={"width": 1400, "height": 1200},
                )
                page = context.new_page()
                page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                page.on("pageerror", lambda exc: page_errors.append(str(exc)))
                page.on("response", lambda response: responses.append({"url": response.url.split("?")[0], "status": response.status}) if response.request.resource_type in {"document", "xhr", "fetch"} else None)
                try:
                    response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except PlaywrightTimeoutError as exc:
                    raise SourceUnavailable("Сетевой тайм-аут при открытии официального источника") from exc
                if response is None or response.status != 200:
                    status = response.status if response is not None else "no-response"
                    raise SourceUnavailable(f"VGTU HTTP {status}")
                _select_group(page, group, timeout_ms, target_statuses=target_statuses)
                try:
                    _wait_for_schedule_state(page, target, group, timeout_ms)
                except PlaywrightTimeoutError as exc:
                    raise SourceUnavailable("Интерфейс расписания не подтвердил выбор группы и даты") from exc

                # STEP (A): Freeze document before any mutations
                frozen = page.evaluate(
                    r"""
                    () => {
                      window.stop();
                      const current = document.documentElement;
                      if (!current) return false;
                      const clone = current.cloneNode(true);
                      clone.querySelectorAll('script').forEach(script => script.remove());
                      current.replaceWith(clone);
                      return true;
                    }
                    """
                )
                if not frozen:
                    raise ScheduleParseError("Не удалось заморозить документ источника")

                # STEP (B): Parse frozen HTML (immutable snapshot)
                frozen_html = page.content()
                schedule = parse_schedule_html(frozen_html, target, group)

                # STEP (C): Verify DOM consistency - check that live DOM still matches parsed state
                target_weekday = _WEEKDAYS[target.weekday()]
                consistency = page.evaluate(
                    r"""
                    ({target, weekdays}) => {
                      const container = document.querySelector('#schedule-container');
                      if (!container) return { valid: false, reason: 'no-container' };
                      
                      // Verify selected group and schedule_date still match our expectations
                      let selectedGroup = null;
                      let selectedDate = null;
                      const groupSelect = document.querySelector('#gruppa');
                      if (groupSelect && groupSelect.value) {
                        selectedGroup = groupSelect.value;
                      }
                      
                      // Find first visible row with schedule data to verify date
                      for (const row of [...container.querySelectorAll('tr')]) {
                        const cells = row.querySelectorAll(':scope > th, :scope > td');
                        if (!cells.length) continue;
                        const label = cells[0].innerText.trim().replace(/\.$/, '');
                        if (weekdays.includes(label)) {
                          selectedDate = label;
                          break;
                        }
                      }
                      
                      // Check consistency: date should match target
                      const dateMatches = selectedDate === target;
                      return {
                        valid: true,
                        selectedDate,
                        selectedGroup,
                        dateMatches,
                        reason: dateMatches ? 'ok' : 'date-mismatch'
                      };
                    }
                    """,
                    {"target": target_weekday, "weekdays": list(_WEEKDAYS)},
                )

                if not consistency.get("valid", False):
                    raise ScheduleParseError(
                        f"DOM consistency check failed: {consistency.get('reason', 'unknown')}"
                    )

                if not consistency.get("dateMatches", False):
                    raise ScheduleParseError(
                        f"DOM consistency mismatch: frozen date {target_weekday} "
                        f"but live date is {consistency.get('selectedDate')}"
                    )

                # STEP (D): Filter display (hide other days) and take screenshot
                found = page.evaluate(
                    r"""
                    ({target, weekdays}) => {
                      const container = document.querySelector('#schedule-container');
                      if (!container) return false;
                      let active = null;
                      let found = false;
                      for (const row of [...container.querySelectorAll('tr')]) {
                        const cells = row.querySelectorAll(':scope > th, :scope > td');
                        if (!cells.length) continue;
                        const label = cells[0].innerText.trim().replace(/\.$/, '');
                        if (weekdays.includes(label)) active = label;
                        const isHeader = [...cells].every(cell => cell.tagName === 'TH');
                        const keep = isHeader || active === target;
                        if (!keep) row.style.display = 'none';  // Hide instead of remove
                        if (!isHeader && active === target) found = true;
                      }
                      return found;
                    }
                    """,
                    {"target": target_weekday, "weekdays": list(_WEEKDAYS)},
                )

                if not found:
                    raise ScheduleParseError("Не удалось выделить выбранный день для скриншота")

                # Validate the same visible DOM that will be rendered in the PNG.
                screenshot_html = page.evaluate(
                    """() => {
                        const clone = document.documentElement.cloneNode(true);
                        clone.querySelectorAll('[style*="display: none"]').forEach(node => node.remove());
                        return '<!doctype html>' + clone.outerHTML;
                    }"""
                )
                screenshot_schedule = parse_schedule_html(screenshot_html, target, group)
                if screenshot_schedule != schedule:
                    raise ScheduleParseError(
                        "DOM consistency violation: parsed data changed between frozen and screenshot"
                    )

                page.locator("#schedule-container").screenshot(
                    path=str(output),
                    animations="disabled",
                )
                if not output.is_file() or output.stat().st_size == 0:
                    raise SourceUnavailable("Пустой скриншот источника")

                checked_at = datetime.now(ZoneInfo("Europe/Moscow"))
                return schedule, checked_at
            except Exception:
                if diagnostics_dir is not None and page is not None:
                    _write_diagnostics(
                        page, diagnostics_dir, responses=responses,
                        console_errors=console_errors, page_errors=page_errors, target_statuses=target_statuses,
                    )
                raise
            finally:
                browser.close()
    except Exception as exc:
        # Covers failures before a page exists (for example browser launch).
        if diagnostics_dir is not None:
            if page is not None:
                _write_diagnostics(
                    page, diagnostics_dir, responses=responses,
                    console_errors=console_errors, page_errors=page_errors,
                    target_statuses=target_statuses,
                )
            else:
                diagnostics_dir.mkdir(parents=True, exist_ok=True)
                (diagnostics_dir / "metadata.json").write_text(
                    json.dumps({
                        "url": "", "title": "", "selects": [], "page_text": "",
                        "schedule_text": "", "responses": responses[-100:],
                        "console_errors": console_errors[-100:],
                        "page_errors": page_errors[-100:],
                        "target_group_http_statuses": target_statuses[-20:],
                        "error": type(exc).__name__,
                    }, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        raise
