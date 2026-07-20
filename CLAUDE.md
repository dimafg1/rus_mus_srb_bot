# rus_mus_srb_bot — контекст проекта

Telegram-бот для русскоязычного музыкального сообщества в Сербии.
Разработчик: dimafg@gmail.com. Python 3.13, aiogram 3.x, SQLite (aiosqlite + SQLModel).

## Запуск

```bash
# Бот
python app/main.py

# Локальное веб-приложение (админ категорий, объявления)
python category_admin.py        # http://localhost:8001
# или двойной клик на category_admin.command (macOS)
```

## Структура проекта

```
app/
  main.py              — точка входа бота, регистрация роутеров
  models.py            — SQLModel-модели (все таблицы)
  database.py          — async SQLite-сессия (SessionLocal)
  texts.py             — get_text(code, lang) через SQLModel/async
  states.py            — FSM-состояния aiogram
  keyboards.py         — вспомогательные клавиатуры
  routers/
    utils.py           — общие хелперы: get_text (aiosqlite), render_flex_block,
                         render_flex_compact, safe_edit_or_send, city_by_slug и др.
    market_view.py     — просмотр объявлений барахолки
    market_add.py      — добавление объявления
    market_edit*.py    — редактирование объявлений
    services_*.py      — раздел «Услуги» (профили музыкантов)
    vacancy_*.py       — биржа музыкантов
    events_*.py        — афиша событий
    user_extra_fields.py — редактирование доп. полей объявления пользователем
    admin_panel.py     — проверка is_admin()
    admin_fields.py    — управление полями категорий из бота
    admin_analytics.py — аналитика (DAU/WAU/MAU и др.)
    feedback.py        — обратная связь
    releases.py        — 🎵 Релизы: мастер, лента, карточка, поиск,
                         редактирование, модерация (Р-11)
    artists.py         — 🎤 Исполнители: лента, карточка, редактирование (Р-12)
    partner_view.py    — карточка партнёрской кампании (UNIXOUND)
  features.py          — выключатели функций (feature_flags, is_enabled)
  campaigns.py         — партнёрские кампании: выбор и ротация
  analytics/           — пакет аналитики: __init__.log_event (analytics_events),
                         search_log.py, listing_views.py
  web/
    app.py             — FastAPI веб-приложение (отдельный процесс)
    contact_redirect.py — редирект /go/contact/{id} для отслеживания контактов
    proxy_media.py     — проксирование медиа из Telegram
category_admin.py      — standalone FastAPI (порт 8001): управление категориями,
                         просмотр/редактирование объявлений, аналитика, медиа-прокси
dev.db                 — рабочая БД (SQLite, WAL-режим)
```

## База данных (dev.db)

Ключевые таблицы:

| Таблица | Назначение |
|---|---|
| `BotText` | Тексты сообщений бота. Поля: `code` (unique), `text_ru`, `text_en`, `title`, `updated_at` |
| `menu` | Кнопки меню. Поля: `code`, `parent_code`, `text` (=text_ru), `text_en`, `callback_data`, `order_num`, `visible` |
| `category` | Иерархия категорий (parent_id). Поле `fields` — JSON с описанием flex-полей |
| `listing` | Объявления барахолки. Поле `flex` — JSON с доп. полями. `status`: active/archived |
| `item` | Анкеты каталога (услуги) |
| `vacancy` | Биржа музыкантов |
| `profile` | Профили музыкантов |
| `BotUser` | Пользователи: `user_id`, `username`, `first_seen`, `last_seen` |
| `city` | Города (`slug`, `name`) |
| `listing_views` | Аналитика просмотров объявлений |
| `ContactView` | Воронка — кто написал продавцу |
| `events_meta` | Мета-данные событий афиши |
| `artist` | Исполнители (постоянные карточки, без сроков): имя, тип, фото, доп. поля, `owner_user_id`, `status` active/hidden. Контакт создателя — базовый, неудаляемый |
| `release_meta` | Релизы: `listing_id` (listing.type='release'), `artist_id`, тип, ссылки JSON, клип file_id, свой статус published/hidden/deleted (30-дневный цикл НЕ применяется) |
| `release_track` | Треки альбомов по одному: позиция, название, file_id + file_unique_id |
| `campaign` | Партнёрские кампании (UNIXOUND — обычная кампания, не хардкод) |
| `feature_flags` | Выключатели функций: audience all/admins/список id, кэш 30 с |
| `analytics_events` | Единый поток событий (словарь — app/analytics/__init__.py) |

`BotUser.first_source` — deep-link источник первого входа, ставится один раз.
Медиа релизов живёт в Telegram по file_id (пересылка без лимита размера);
скрытое админу помечается 🔴. Перед деплоем: `python scripts/smoke_check.py`.

## i18n (многоязычность)

Схема: **одна строка = одна функция**, колонки `text_ru` / `text_en`.
Казахский (планируется): `ALTER TABLE BotText ADD COLUMN text_kk TEXT NOT NULL DEFAULT ''`.

```python
# app/texts.py — через SQLModel (async)
await get_text("code", lang="ru")

# app/routers/utils.py — через aiosqlite (для роутеров без сессии)
await get_text("code", lang="ru")
```

Язык пользователя хранится в его состоянии FSM. Переключение языков — планируется
по образцу confession_09 бота (`/Users/d/dev/confession_09`).

## Flex-поля (доп. поля объявлений)

`category.fields` — JSON-массив объектов:
```json
[
  {"type": "__meta", "key": "allow_extra_categories", "value": true},
  {"type": "number", "label": "Количество клавиш", "key": "number_of_keys", "required": false},
  {"type": "video",  "label": "Видео", "key": "video", "required": false}
]
```

`listing.flex` — JSON-словарь значений: `{"number_of_keys": 88, "video": "https://..."}`.

**Важно:** поля наследуются по цепочке `parent_id` — дочерняя категория видит
поля всех предков плюс свои собственные. Дочерние перекрывают родительские при
совпадении ключа. Реализовано в `utils.py` → `_flex_labels_for_category` и
`render_flex_block`.

Тип `video` — не отображается как текстовое поле, показывается отдельным плеером.
Тип `__meta` — служебный, не отображается пользователю.

**UX подачи объявления:** мастер спрашивает только базовые поля (название,
цена, описание, фото). Flex-поля предлагаются ПОСЛЕ публикации отдельным
списком — пользователь нажимает только на нужные, каждое поле опционально
(`extras_after_publish` в market_add.py, user_extra_fields.py). Поэтому
количество flex-полей на категорию не ограничено соображениями UX.

## category_admin.py (локальный веб-админ)

- FastAPI + SQLite3 (sync), порт 8001
- Управление категориями: дерево, drag-and-drop, описание полей
- Просмотр и редактирование объявлений (title, descr, price, contact, flex-поля)
- Удаление/скрытие объявлений, удаление фото
- Аналитика: DAU/WAU/MAU, просмотры, конверсия контактов
- Медиа-прокси: `/api/tg_photo/{file_id}` — отдаёт фото/видео из Telegram
- Удалённый доступ (из дома): Tailscale установлен, Mac = `100.104.29.69`,
  адрес админки — `http://100.104.29.69:8001`. Приложение слушает 0.0.0.0,
  middleware-фильтр пускает только localhost и Tailscale-сеть (100.64.0.0/10),
  из офисного LAN — 403. Mac не должен засыпать (Экономия энергии).

## Ключевые паттерны

**Тексты из БД:**
```python
text = await get_text("market_welcome", lang=lang)
```

**Рендер flex-полей для бота:**
```python
flex_block = await render_flex_block(session, listing, lang=lang)
```

**Железобетонное правило навигации (требование владельца, всегда):**
на КАЖДОМ экране и шаге мастера — кнопка «⬅️ Назад» (ровно один шаг назад)
и кнопка «☰ Главное меню». Пользователь, который передумал или перепутал,
всегда должен иметь путь назад. Как во всех существующих разделах.

**Железное правило чата (обязательно для каждого нового экрана/раздела):**
перед показом нового экрана удалять предыдущие сообщения бота, а каждое
отправленное сообщение регистрировать и в памяти, и в БД (переживает рестарт):
```python
await clear_bot_messages(chat_id, bot)               # чистка: БД-слой + кэши
msg = await bot.send_message(...)
last_bot_messages.setdefault(chat_id, []).append(msg.message_id)
await register_bot_messages(chat_id, [msg.message_id])  # БД-слой (BotMessage)
```

**Безопасное редактирование сообщения (избегает ошибки "message not modified"):**
```python
await safe_edit_or_send(bot, message, text, reply_markup=kb)
```

**Путь категории (читаемый):**
```python
path = await render_category_path(session, category_id)
# → "Музыкальные инструменты → Синтезаторы / Клавишные"
```

## Бэкапы и безопасность данных

- **Бэкап только скриптом**: `python scripts/backup_db.py` → `backups/dev_*.db`
  (sqlite backup API + integrity_check, хранятся последние 10).
  **Никогда не копировать dev.db как файл** — база в WAL-режиме,
  свежие данные лежат в dev.db-wal, `cp` даёт неполный снимок.
- `dev.db`, WAL-файлы, `backups/`, `logs/` — вне git (.gitignore).
  История: git filter-repo однажды перезаписал рабочую базу старой копией.
- Архивы проекта — только `git archive` либо с исключениями:
  `.env*`, `*.db*`, `backups/`, `logs/`, `__pycache__/`, `.git/`.
- Логи: бот → `logs/bot.log`, админка → `logs/admin.log` (ротация 5МБ x 5).
- `foreign_keys=ON` в SQLite НЕ включать: в DDL `listing.category_id`
  без ON DELETE — сломается удаление категорий с объявлениями.

## К деплою на сервер

- category_admin — только за Basic Auth / nginx, ни минуты на публичном
  интерфейсе без авторизации (редактирует всё, проксирует медиа через токен).
- Все процессы (бот, web, админка) + SQLite — строго на одной машине,
  по сети SQLite не разделять.

## FSM и конкурентность (июль 2026)

- FSM живёт в SQLite (`app/fsm_storage.py`, таблица `fsmstate`) — шаг мастера
  и данные переживают рестарт. Кэш в памяти — «источник скорости», БД —
  «источник правды после рестарта»; сбой записи в БД логируется, но не
  роняет бота (осознанный компромисс, описан в модуле).
- `Dispatcher(events_isolation=SimpleEventIsolation())` — апдейты одного
  пользователя обрабатываются последовательно. Новые обработчики могут
  полагаться на это: две параллельных гонки одного юзера невозможны.
- Публикации во всех мастерах — под asyncio.Lock + проверка состояния
  (паттерн: services_add._service_publish_locks). Новым мастерам делать так же.
- Тесты: `poetry run python -m unittest tests.<модуль>` (unittest, НЕ pytest;
  pytest в окружении нет). 88 тестов, перед «готово» гонять все.

## Планы / незакрытые задачи

- Стратегия проекта (Strategy v2, 2026-07-15): `docs/strategy/00_strategy_map.md`
  — карта + 4 документа по слоям; журнал решений — `04_open_decisions.md`.
  Старые `docs/monetization_plan.md` и `monetization_review_claude.md` — архив.
  Код по стратегии — только по явному «да» владельца.
- Хвосты аудита Codex (согласованно отложены, некритичны): TTL для брошенных
  черновиков fsmstate (копятся бессрочно); альбом фото за ~1 с до рестарта
  теряется (кэш в памяти); два процесса бота одновременно недопустимы
  (замки FSM внутрипроцессные) — systemd должен гарантировать один инстанс.
- Секреты перед деплоем: .env в git + токен в logs/admin.log — отложено
  владельцем, вернуться ОБЯЗАТЕЛЬНО при переносе на сервер.
- Переключение языка пользователем в боте (RU/EN, потом KZ)
- **В процессе (начато 2026-07-20): перенос хардкод-текстов в БД (подготовка
  к RU/EN/KZ).** ~600 пользовательских строк на кириллице в `send_message`/
  `.answer`/`edit_message_text`/`caption=` разбросаны по 27 файлам
  `app/routers/*.py` (плюс отдельно ~сотни дублей в текстах кнопок вроде
  «⬅️ Назад» — 103 раза, «Нет доступа» — 46, и т.д.). Важно: сам перенос
  **ничего не меняет для пользователей**, пока не построено переключение
  языка (см. пункт выше) — это чистая подготовка.
  План (3 шага, делать последовательно, файл за файлом, с прогоном тестов
  после каждого):
  1. **Кнопки «Назад»/«Главное меню» — ЗАВЕРШЕНО (2026-07-20).** Хардкод
     `text="⬅️ Назад"` (103 места по всем `app/routers/*.py`) сведён на
     `menu` (таблица) + `get_common_menu_button(code, lang)`
     (`app/keyboards.py`). Паттерн:
     ```python
     back_btn = await get_common_menu_button('back')
     if back_btn:                        # может вернуть None (нет строки в menu)
         back_btn.callback_data = "свой_callback"
     ```
     где кнопка обязательна по правилу навигации — фолбэк
     `or InlineKeyboardButton(text="⬅️ Назад", callback_data=...)`.
     В нескольких файлах с повторяющимся паттерном заведён локальный
     хелпер (`_back_btn`/`_back_row`/`_nav_row` — по одному на файл,
     не общий, т.к. сигнатуры и наборы кнопок отличаются).
     **Единственное сознательное исключение:** `admin_panel.py` (~строка
     457) — персистентная Reply-клавиатура `KeyboardButton`,
     синхронизированная с проверками `message.text == "⬅️ Назад"` в
     нескольких хендлерах; риск рассинхронизации не оправдан для
     owner-only экрана, оставлено в хардкоде.
     **Найденный и исправленный баг:** правка `admin_panel.py` сначала
     создала циклический импорт `app.keyboards` ↔ `app.routers.admin_panel`
     (keyboards.py импортирует `is_admin` оттуда) — падал `import
     app.main`, но не тесты (у них другой порядок импорта, маскирует
     цикл). Исправлено `import app.keyboards as _keyboards` вместо
     `from app.keyboards import get_common_menu_button` (коммит 067a633).
     **Урок на будущее:** после правок в `app/keyboards.py` или
     `admin_panel.py` проверять `python -c "import app.main"` —
     тестов недостаточно, они импортируют модули в другом порядке.
     (Отдельный вариант «◀️ Назад» в
     `admin_panel.py`/`admin_analytics.py`/`events_view.py`/`events_add.py` —
     это пагинация «пред./след.», НЕ трогать, другая семантика.)
  2. **Дедуп общих фраз-сообщений** (не кнопок) в `BotText` — одна запись на
     уникальный текст, а не одна на место использования. Кандидаты по частоте:
     «Некорректные данные.» (23), «Можно редактировать только свои услуги.»
     (18), «Объявление не найдено.» (16), «Некорректная ссылка.» (12),
     «Фото не найдено.» (9), «Неверные данные» (8), «Нет доступа.» (7) и т.д.
     Не начато.
  3. **Файл за файлом — уникальные тексты экранов**, начиная с крупных:
     `events_add.py`(84), `releases.py`(65), `admin_fields.py`(52),
     `market_edit_overview.py`(49), `services_edit_overview.py`(41),
     `vacancy_add.py`(32), `events_view.py`(30), `admin_panel.py`(30),
     `market_view.py`(28), и далее по убыванию (полный список: команда
     `grep -cE "(send_message|\.answer|edit_message_text|caption=)\s*\(" app/routers/*.py`
     + фильтр по кириллице). Не начато. `admin_panel.py`/`admin_analytics.py`/
     `admin_fields.py` — внутренние инструменты владельца, перевод им не
     нужен, можно оставить в хардкоде или сделать последними.
- Публичный сайт для пользователей (веб-интерфейс объявлений)
- Запуск бота в Казахстане после отладки на Сербии
- Basic Auth + nginx для category_admin при деплое на сервер
- Добавление фото в объявление через веб-админ (через Telegram Bot API)
