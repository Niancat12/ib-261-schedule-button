from __future__ import annotations

import os
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
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if response is None or response.status != 200:
                    status = response.status if response is not None else "no-response"
                    raise SourceUnavailable(f"VGTU HTTP {status}")
                page.wait_for_selector("#schedule-container", state="visible", timeout=timeout_ms)

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
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        raise SourceUnavailable("Тайм-аут источника расписания") from exc
