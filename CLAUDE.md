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

## category_admin.py (локальный веб-админ)

- FastAPI + SQLite3 (sync), порт 8001
- Управление категориями: дерево, drag-and-drop, описание полей
- Просмотр и редактирование объявлений (title, descr, price, contact, flex-поля)
- Удаление/скрытие объявлений, удаление фото
- Аналитика: DAU/WAU/MAU, просмотры, конверсия контактов
- Медиа-прокси: `/api/tg_photo/{file_id}` — отдаёт фото/видео из Telegram
- Для удалённого доступа (из дома): Tailscale VPN, запуск с `host="0.0.0.0"`

## Ключевые паттерны

**Тексты из БД:**
```python
text = await get_text("market_welcome", lang=lang)
```

**Рендер flex-полей для бота:**
```python
flex_block = await render_flex_block(session, listing, lang=lang)
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

## Планы / незакрытые задачи

- Переключение языка пользователем в боте (RU/EN, потом KZ)
- Публичный сайт для пользователей (веб-интерфейс объявлений)
- Запуск бота в Казахстане после отладки на Сербии
- Basic Auth + nginx для category_admin при деплое на сервер
- Добавление фото в объявление через веб-админ (через Telegram Bot API)
