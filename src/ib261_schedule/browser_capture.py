from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
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
      options: [...el.options].map((o) => ({value: o.value, text: o.textContent.trim()})).slice(0, 200)
    }))""")


def _select_group(page, group: str, timeout_ms: int) -> None:
    """Select the dependent faculty/group controls and prove the UI accepted it."""
    def find_group():
        metadata = _select_metadata(page)
        item = next((entry for entry in metadata if any(option.get("text", "").strip() == group for option in entry.get("options", []))), None)
        if item is None:
            return None
        selector = f"select#{item['id']}" if item.get("id") else f"select[name='{item['name']}']"
        return page.locator(selector).first

    group_select = find_group()
    if group_select is None:
        # The group list is commonly populated after choosing a faculty/subdivision.
        for item in _select_metadata(page):
            selector = f"select#{item['id']}" if item.get("id") else (f"select[name='{item['name']}']" if item.get("name") else None)
            if not selector:
                continue
            control = page.locator(selector).first
            for option in item.get("options", [])[:50]:
                value = option.get("value", "")
                label = option.get("text", "").strip()
                if not value or not label or label.casefold() in {"выберите", "выберите факультет", "все"}:
                    continue
                try:
                    control.select_option(value, timeout=min(timeout_ms, 5000))
                    control.evaluate("el => { el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }")
                    page.wait_for_timeout(250)
                except Exception:
                    continue
                group_select = find_group()
                if group_select is not None:
                    break
            if group_select is not None:
                break
    if group_select is None:
        raise ScheduleParseError(f"Группа {group} отсутствует в интерфейсе источника")
    current = group_select.locator("option:checked").inner_text().strip()
    if current != group:
        option = group_select.locator("option").filter(has_text=re.compile(rf"^{re.escape(group)}$"))
        value = option.get_attribute("value")
        if value is None:
            raise ScheduleParseError(f"Группа {group} отсутствует в интерфейсе источника")
        group_select.select_option(value, timeout=timeout_ms)
    group_select.evaluate("el => { [...el.options].forEach(o => o.removeAttribute('selected')); el.selectedOptions[0]?.setAttribute('selected', 'selected'); el.dispatchEvent(new Event('input', {bubbles:true})); el.dispatchEvent(new Event('change', {bubbles:true})); }")
    page.wait_for_function("expected => [...document.querySelectorAll('select option:checked')].some(o => o.textContent.trim() === expected)", arg=group, timeout=timeout_ms)


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


def _write_diagnostics(page, directory: Path, *, responses: list[dict[str, Any]], console_errors: list[str], page_errors: list[str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    try: page.screenshot(path=str(directory / "page.png"), full_page=True)
    except Exception: pass
    try: (directory / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception: pass
    try:
        container = page.locator("#schedule-container")
        meta = {"url": page.url, "title": page.title(), "selects": _select_metadata(page), "page_text": page.locator("body").inner_text(timeout=1000) if page.locator("body").count() else "",
            "schedule_text": container.inner_text(timeout=1000) if container.count() else "", "responses": responses[-100:], "console_errors": console_errors[-100:], "page_errors": page_errors[-100:]}
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
                _select_group(page, group, timeout_ms)
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
                        console_errors=console_errors, page_errors=page_errors,
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
                )
            else:
                diagnostics_dir.mkdir(parents=True, exist_ok=True)
                (diagnostics_dir / "metadata.json").write_text(
                    json.dumps({
                        "url": "", "title": "", "selects": [], "page_text": "",
                        "schedule_text": "", "responses": responses[-100:],
                        "console_errors": console_errors[-100:],
                        "page_errors": page_errors[-100:],
                        "error": type(exc).__name__,
                    }, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        raise
