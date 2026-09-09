# Progress Log

## Session: 2026-09-08

### Phase 1: Исследование источника и интеграционной точки

- **Status:** complete
- **Started:** 2026-09-08 (Europe/Moscow)
- Actions taken:
  - Создан отдельный каталог проекта.
  - Инициализирован изолированный gated-план `planning-with-files`.
  - Инициализирован локальный Git-репозиторий без удалённого origin.
  - Зафиксированы требования и критерии приёмки.
  - Выполнено первичное чтение страницы ВГТУ и официальной документации Hermes Messaging Gateway.
  - Найден потенциальный официальный XLSX для ИБ-261 и отмечен как неподтверждённый резервный источник.
  - Выявлена необходимость проверить локальную поддержку произвольных Telegram callback-кнопок.
  - Уточнён UX: постоянная reply-кнопка, быстрые даты `Вчера/Завтра`, дальний календарь или ввод.
  - Уточнено отображение: обе подгруппы, скриншот только области выбранного дня.
  - Уточнена деградация: разрешён последний кэш любого возраста с точным временем и заметной маркировкой.
  - Проверено прямое открытие источника в Chromium: получен `403 Forbidden` от nginx, событие внесено в исследовательские риски.
  - По архивной копии исходного HTML подтверждён реальный контракт: jQuery GET XHR на ту же страницу с `date`, `gruppa`, `parity`, `prepodavatel`; ответ — полный HTML, расписание берётся из `#schedule-container`.
  - Реальным чтением ответа для `ИБ-261` и `2026-09-08` подтверждены знаменатель, структура полей и три вторничных занятия.
  - Подтверждён серверный UTC+03:00 (`SERVER_TZ_OFFSET=10800`) и риск browser-local `new Date()` вне `Europe/Moscow`.
  - Подтверждён отдельный официальный XLSX как независимый резерв, а не клиентский источник онлайн-виджета.
  - Зафиксирован способ настоящего скриншота одного дня: live browser, локально скрыть строки других дней, screenshot исходного DOM-контейнера с предварительной сверкой метаданных.
  - Подтверждена штатная архитектура Hermes user-plugin: `register_platform_handler` для PTB Application, `pre_gateway_dispatch` для текстов reply-кнопок, `spawn_task` для асинхронной обработки.
- Files created/modified:
  - `.planning/2026-09-08-telegram-ib-261-schedule/task_plan.md`
  - `.planning/2026-09-08-telegram-ib-261-schedule/findings.md`
  - `.planning/2026-09-08-telegram-ib-261-schedule/progress.md`

### Phase 2: Архитектура и контракты данных

- **Status:** in_progress
- Actions taken:
  - Выбран user-plugin без правки ядра Hermes.
  - Выбран изолированный worker для загрузки, разбора, live-скриншота и атомарного кэша.
  - Для дальней даты выбран валидируемый ввод после кнопки `📆 Другая дата`; callback query не нужен.
- Files created/modified:
  - Нет.

### Phase 3: Реализация получения и нормализации расписания

- **Status:** pending

### Phase 4: Telegram-интеграция

- **Status:** pending

### Phase 5: Тестирование и верификация

- **Status:** pending

### Phase 6: Доставка и эксплуатация

- **Status:** pending

## Test Results

| Test | Input | Expected | Actual | Status |
|------|-------|----------|--------|--------|
| Инициализация planning-with-files | gated plan в отдельном проекте | Три planning-файла и активный plan id | Созданы; plan id `2026-09-08-telegram-ib-261-schedule` | pass |
| Локальный Git | `git init` | Репозиторий готов без удалённого origin | `.planning/` виден как untracked | pass |
| Первичное извлечение страницы источника | URL онлайн-расписания | Доступность и базовая структура | Страница доступна, но строки расписания не видны в статическом извлечении | partial |
| Открытие источника в Chromium | URL онлайн-расписания | Интерактивная форма расписания | `403 Forbidden`, DOM расписания отсутствует | fail |
| Полный test suite | `uv run pytest` | Все тесты проходят | `31 passed` | pass |
| Статический анализ | `uvx ruff check .` и format check | Нет ошибок | Все проверки пройдены | pass |
| Локальный browser capture | локальный HTTP fixture + Chromium | PNG контейнера одного дня и согласованный текст | Пройдено | pass |
| Реальный worker | ВГТУ, ИБ-261, 08.09.2026 | Свежая выдача либо честная недоступность | `unavailable`, кэш отсутствует, источник/время указаны | pass |
| Hermes Plugin Doctor | `hermes plugins doctor ./plugin --ci` | Импорт и регистрация hook успешны | `OK`, 1 hook | pass |
| Установка user-plugin | symlink + config + enable | Плагин обнаружен и включён | `enabled user 0.1.0 ib261-schedule` | pass |

## Error Log

| Timestamp | Error | Attempt | Resolution |
|-----------|-------|---------|------------|
| 2026-09-08 | `git diff --stat`: каталог ещё не был Git-репозиторием | 1 | Выполнен `git init`; ошибка не повторяется тем же способом. |
| 2026-09-08 | Источник вернул Chromium `403 Forbidden` | 1 | Не повторять тот же профиль запроса; исследовать официальный XLSX/API и допустимые заголовки/маршруты. |
| 2026-09-08 | Первый запуск worker не нашёл editable-пакет | 1 | Добавлен build backend `hatchling`; `uv sync` устанавливает пакет. |
| 2026-09-08 | Рестарт gateway из его дочернего процесса заблокирован | 1 | Подготовлен безопасный внешний transient systemd restart после code review/commit. |

## 5-Question Reboot Check

| Question | Answer |
|----------|--------|
| Where am I? | Phase 5: тестирование и верификация; код и плагин установлены, gateway ещё не перезапущен |
| Where am I going? | К E2E-нажатию кнопки, проверке восстановления после рестарта и закрытию оставшегося egress-блокера |
| What's the goal? | Кнопка ИБ-261 с согласованным скриншотом и текстом за локальную дату ВГТУ |
| What have I learned? | Штатный user-plugin поддерживает reply keyboard; live Chromium с текущего egress получает 403, поэтому выдача обязана fail-closed |
| What have I done? | Реализованы parser/browser worker/атомарный кэш/Telegram UI, 31 тест, Plugin Doctor и установка user-plugin |
