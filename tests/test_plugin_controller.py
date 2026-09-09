from datetime import date

from plugin.controller import ButtonController

KEY = ("1000593689", "1000593689", None)
TODAY = date(2026, 9, 8)


def test_main_button_uses_moscow_today_and_sets_anchor_only_after_display():
    controller = ButtonController()
    action = controller.handle("📅 Расписание ИБ-261", KEY, TODAY)
    assert action.kind == "schedule"
    assert action.target == TODAY
    controller.mark_displayed(KEY, TODAY)
    assert controller.handle("➡️ Завтра", KEY, TODAY).target == date(2026, 9, 9)


def test_previous_and_next_are_relative_to_last_displayed_date():
    controller = ButtonController()
    first = controller.handle("⬅️ Вчера", KEY, TODAY)
    assert first.target == date(2026, 9, 7)
    assert controller.handle("⬅️ Вчера", KEY, TODAY).target == date(2026, 9, 7)
    controller.mark_displayed(KEY, first.target)
    assert controller.handle("⬅️ Вчера", KEY, TODAY).target == date(2026, 9, 6)


def test_requested_date_does_not_advance_anchor_until_successfully_displayed():
    controller = ButtonController()
    controller.handle("📆 Другая дата", KEY, TODAY)
    chosen = controller.handle("15.10.2026", KEY, TODAY)
    assert chosen.target == date(2026, 10, 15)
    assert controller.handle("➡️ Завтра", KEY, TODAY).target == date(2026, 9, 9)
    controller.mark_displayed(KEY, chosen.target)
    assert controller.handle("➡️ Завтра", KEY, TODAY).target == date(2026, 10, 16)


def test_other_date_only_intercepts_the_next_validated_date():
    controller = ButtonController()
    assert controller.handle("📆 Другая дата", KEY, TODAY).kind == "ask_date"
    invalid = controller.handle("31.02.2026", KEY, TODAY)
    assert invalid.kind == "invalid_date"
    chosen = controller.handle("15.10.2026", KEY, TODAY)
    assert chosen.kind == "schedule"
    assert chosen.target == date(2026, 10, 15)
    assert controller.handle("16.10.2026", KEY, TODAY) is None


def test_unrelated_text_is_not_intercepted():
    assert ButtonController().handle("обычное сообщение", KEY, TODAY) is None


def test_setup_command_only_installs_keyboard():
    action = ButtonController().handle("/ib261", KEY, TODAY)
    assert action.kind == "keyboard"
    assert action.target is None
