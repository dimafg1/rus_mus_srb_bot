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
  2. **Дедуп общих фраз-сообщений** (не кнопок) в `BotText` — одна запись
     на уникальный текст, а не одна на место использования.
     **Список кандидатов из исходного плана — ЗАКРЫТ (2026-07-20):**
     «Некорректные данные.» (23→`err_invalid_data`), «Объявление не
     найдено.» (16→`err_listing_404`, включая `events_add.py` — там тоже
     про listing, не про мероприятие), «Можно редактировать только свои
     объявления.» (17→`err_not_owner`), «Можно редактировать только свои
     услуги.» (18→`err_not_owner_service`, новый код), «Некорректная
     ссылка.» (12→`err_invalid_link`, новый), «Фото не найдено.»
     (9→`err_photo_404`, новый), «Неверные данные» (8→`err_bad_data`,
     новый), «Нет доступа.» (7→`err_no_access`, новый; owner-only файлы
     `admin_fields.py`/`admin_panel.py`, но дедуп сделан всё равно).
     Итого 110 мест хардкода убрано. Приятная находка по пути: в БД уже
     был готовый набор `err_*` (создан 2026-07-06, ДО начала проекта,
     видимо владельцем через category_admin, с готовым `text_en`), но
     нигде не подключён в коде — переиспользовали вместо создания
     дублей. **Перед добавлением новой строки всегда проверять, нет ли
     готовой:** `sqlite3 dev.db "SELECT code, text_ru FROM BotText WHERE
     text_ru LIKE '%фраза%'"`.
     Паттерн подключения (везде одинаковый, вызов-обёртка варьируется):
     ```python
     await cb.answer(await get_text("err_listing_404", "ru") or "Объявление не найдено.", show_alert=True)
     ```
     (`get_text` — либо из `app.routers.utils`, либо из `app.texts`,
     сигнатуры совместимы; если в файле не было импорта — добавляли).
     **Дальше — по решению владельца (2026-07-20): переводить в БД ВСЕ
     тексты, не только дубли**, чтобы не возвращаться к этому повторно.
     Значит пункты 2 и 3 плана фактически объединяются: остались
     единичные (неповторяющиеся) фразы-сообщения — они пойдут туда же,
     каждая под свой код. **Новое правило на будущее (постоянное,
     не только для этого прохода): любой новый текст, меню или кнопка,
     добавляемые в код с этого момента, заводятся сразу через `BotText`
     (`get_text`) — не хардкодом**, даже если это разовая строка на
     один экран.
  3. **Файл за файлом — уникальные тексты экранов.**
     `services_add.py`(40) — **готово (2026-07-20)**: 19 новых кодов
     `services_add_*` (invalid_city/city_not_found/invalid_category/
     city_or_cat_gone/ask_title/title_empty/ask_descr_tmpl/ask_descr_back/
     price_prompt_suffix/price_prompt_short/btn_deal_price/btn_photo_skip/
     btn_cancel/missing_field_tmpl/save_error_tmpl/ui_update_failed/
     choose_category/publishing_wait/already_published), плюс переиспользованы
     `vac_edit_all`/`vac_go_listing` (точное совпадение текста кнопок).
     `_deal_price_kb()`/`_photo_skip_kb()` стали async (текст кнопок теперь
     из БД) — оба вызова обёрнуты в `await`.
     `admin_panel.py`(49) — **готово (2026-07-20)**: 60 новых кодов
     `admin_panel_*` (владельческий раздел — вход в панель, категории:
     создание/переименование/удаление/slug-конфликты, кнопки главного меню
     и подменю, обратная связь: списки/пагинация/карточка/удаление,
     список пользователей бота), плюс переиспользованы `btn_cancel`/
     `btn_yes_delete`/`btn_delete` (точное совпадение текста кнопок).
     `get_admin_menu()` стала async (текст кнопок теперь из БД) — оба
     вызова обёрнуты в `await`. Не тронуто намеренно (см. запись выше
     про исключение `admin_panel.py`): персистентная Reply-клавиатура
     «⬅️ Назад» и сверки `message.text == "⬅️ Назад"`; пагинационные
     «◀️ Назад» — другая семантика, как и в остальных файлах.
     `admin_fields.py`(57) — **готово (2026-07-20)**: 63 новых кодов
     `admin_fields_*` (владельческий раздел — меню доп.полей категории,
     добавление/редактирование/удаление/перемещение поля, тумблер
     «доп.категории вкл/выкл», все связанные кнопки и шаблоны текстов),
     плюс переиспользованы `btn_cancel`/`btn_delete`/`admin_panel_btn_ok`
     (точное совпадение текста кнопок).
     `vacancy_add.py`(62) — **готово (2026-07-20)**: 26 новых кодов
     `vac_add_*` (мастер публикации вакансии — шаги город/категория/
     заголовок/описание/цена/доп.поля/предпросмотр/публикация, плашка
     «◀️ Возврат», ошибки и подтверждения), плюс переиспользованы
     `services_add_city_not_found`/`services_add_invalid_category`/
     `services_add_city_or_cat_gone`/`services_add_publishing_wait`/
     `services_add_already_published`/`btn_skip`/`btn_free`/
     `btn_by_agreement`/`admin_panel_btn_no`/`vac_choose_subcat`/
     `vac_choose_cat`/`vac_err_no_data`/`vac_err_no_city`/`vac_cancelled`/
     `vac_published`/`vac_to_menu`/`vac_ask_title` (точное совпадение
     текста — хорошая находка межфайловых дублей с шагом 2 и другими
     файлами шага 3).
     `services_view.py`(70) — **готово (2026-07-20)**: 27 новых кодов
     `services_view_*` (главное меню/город/категория/карточка услуги,
     продление/закрытие/восстановление, «Мои услуги», поиск услуг —
     заголовки/пагинация/ошибки), плюс переиспользованы межфайловые
     дубли из раздела «Вакансии» (`vacancy_contacts_mgmt_label`,
     `vacancy_btn_archive`/`_extend`/`_restore`/`_contact`,
     `vac_edit_all`, `vacancy_extend_data_error`/`_unavailable`,
     `vacancy_close_data_error`, `vacancy_btn_new_search`,
     `vacancy_card_city`) и из общего поиска (`search_typo_correction_note`
     — байт-в-байт совпадение шаблона с опечаткой, включая двойной
     перевод строки). Хорошая находка: карточка вакансии и карточка
     услуги почти зеркальны текстово — почти все переиспользования нашлись
     именно там.
     `services_edit_overview.py`(73) — **готово (2026-07-20)**: 42 новых
     кода `services_edit_*` (обзор редактирования услуги: заголовок/
     город/категория/поля-лейблы, кнопки «Править …», запрос значения
     основного/доп.поля/видео, мини-меню «Доп. категории» — открытие/
     добавление/удаление/выбор ветки категории, ошибки владения и типов
     полей), плюс переиспользованы `err_invalid_id`/`market_edit_title_empty`/
     `vacancy_edit_field_unavailable` (точное совпадение). `_build_overview_text()`
     стала async (была `def`, не `async def`) — единственный вызов обёрнут
     в `await`.
     `market_edit_overview.py`(78) — **готово (2026-07-20)**: 34 новых
     кода `market_edit_*` (обзор редактирования объявления Барахолки:
     заголовок/город/категория/поля-лейблы, кнопки «Править …», запрос
     значения основного/доп.поля text-number/select-checkbox/видео,
     мини-меню «Доп. категории»), плюс переиспользованы `vacancy_edit_field_unavailable`/
     `err_field_type`/`extra_field_not_video_link`/`extra_field_need_video`/
     `services_edit_field_not_found`/`_extra_disabled`/`_btn_add_extra_category`/
     `_extra_slots_full`/`_category_not_found`/`_extra_same_as_main`/`_extra_duplicate`/
     `_removed_toast`/`_added_toast`/`_btn_delete_extra_category_tmpl`/`_extra_menu_tmpl`/
     `_choose_extra_subcategory_tmpl`/`vac_add_checkbox_yes`/`admin_panel_btn_no`/
     `admin_fields_yes`/`admin_fields_no` (точное совпадение — почти зеркало
     `services_edit_overview.py`, разница только в словах «Барахолка»/«объявление»
     vs «Услуги»/«услуга»). `_controls_cancel()` и `_fmt()` стали async (были
     `def`) — все вызовы обёрнуты в `await`.
     `events_view.py`(86) — **готово (2026-07-20)**: 42 новых кода
     `events_view_*` (Афиша — мои события, ближайшие мероприятия,
     карточка события в трёх местах почти зеркальных друг другу,
     поиск по Афише, календарь по всем городам/по одному городу, список
     на выбранную дату), плюс переиспользованы уже готовые «сиротские»
     коды из БД (заведены до начала проекта, 2026-07-06, но раньше нигде
     не подключённые): `btn_back`/`btn_main_menu`/`btn_show_more`/
     `btn_search`/`btn_my_events`/`btn_near_events`/`btn_add_event`/
     `btn_edit`/`btn_delete`/`btn_contact`/`btn_new_search`/
     `af_err_not_found`/`af_err_not_owner`/`events_choose_city`,
     плюс `services_add_city_not_found`/`search_typo_correction_note`
     (межфайловые дубли из более ранних шагов). **Осознанно НЕ перенесены**
     (документируется здесь, не трогать без отдельного решения владельца):
     названия месяцев и дней недели в двух копипаст-функциях календаря
     (`_kb_calendar_month_all`/`_kb_calendar_month_city`, строки с
     массивами `["Пн","Вт",...]` и `["Январь","Февраль",...]`) — та же
     логика, что и с большими отчётами `admin_analytics.py`: 19 фраз ×
     2 копии ради названий месяцев/дней недели, эффект несоразмерен
     объёму работы, эти строки почти никогда не требуют иного перевода
     кроме прямого. `_kb_back_and_main()`/`_kb_list_nav()`/`_kb_my_card()`/
     `_kb_calendar_month_all()`/`_kb_calendar_month_city()`/
     `_moderation_label()` стали async (были `def`) — все вызовы обёрнуты
     в `await`.
     `market_view.py`(89) — **готово (2026-07-20)**: 41 новый код
     `market_view_*` (город/категория/список объявлений, поиск Барахолки —
     заголовки/пустые результаты/устаревшие результаты, «Мои объявления»,
     карточка объявления — категория/видео/раскрыть-свернуть, продление/
     закрытие), плюс переиспользованы межфайловые дубли из вакансий/услуг
     (`vacancy_contacts_mgmt_label`, `vac_edit_all`, `vacancy_btn_archive`/
     `_extend`/`_restore`, `vacancy_extend_data_error`/`_unavailable`,
     `vacancy_close_data_error`, `err_photo_404`, `btn_watch_video`) и
     `search_typo_correction_note`.
     `releases.py`(110) — **готово (2026-07-20)**: 154 новых кода
     `releases_*` (лента релизов, карточка, жалоба и модерация, «Мои
     релизы», мастер добавления — исполнитель/тип/название/обложка/медиа/
     описание/подтверждение/публикация, поиск, редактирование релиза —
     поля/тип/треки), плюс переиспользованы `err_invalid_link`/
     `services_add_publishing_wait`/`search_typo_correction_note`/
     `btn_watch_video`/`btn_main_menu`/`btn_new_search` (точное совпадение).
     `_menu_btn()`/`_nav_row()`(уже была)/`_release_yt_button()`/
     `_release_caption()`/`_release_kb()`/`_release_back()` стали async
     (были `def`) — все вызовы обёрнуты в `await`; тесты в
     `tests/test_music_release_regressions.py` обновлены под async
     (класс `MusicHelperTests` переведён на `IsolatedAsyncioTestCase`).
     **Осознанно НЕ перенесены** (документируется здесь, как и в
     `events_view.py`): словари-константы `RELEASE_TYPES`/`ARTIST_TYPES`/
     `LINK_LABELS`/`REPORT_REASONS` — это enum-подобные наборы (типы
     релиза/исполнителя, площадки-лейблы, причины жалобы), используются
     как синхронные dict-lookup и `in`-проверки в большом числе мест по
     всему файлу; перевод на BotText потребовал бы отдельного асинхронного
     слоя поверх кодов-ключей ради 5–8 значений на словарь — несоразмерно
     объёму. Возврат к этому — по отдельному решению владельца.
     `events_add.py`(154) — **готово (2026-07-20), ПОСЛЕДНИЙ ФАЙЛ СПИСКА**:
     116 новых кодов `af_*` (черновик объявления, обзор редактирования,
     мастер добавления событий — название/дата/время/город/цена/место/
     описание/фото, редактирование каждого поля после публикации,
     публикация/модерация), плюс огромное переиспользование: в БД уже
     лежал готовый набор `af_ask_*`/`af_err_*`/`afisha_choose_city`
     (заведён 2026-07-06, до начала проекта, специально под этот мастер,
     но нигде не подключённый) — почти все toast/prompt тексты мастера
     легли на готовые коды без единой правки текста. `_draft_text_from_data()`/
     `_kb_afisha_edit_overview()`/`_kb_edit_cancel()`/`_kb_edit_photo()`/
     `_kb_edit_price()`/`_kb_city_from_db()` (уже была)/`_kb_skip()`/
     `_kb_price()`/`_kb_preview()`/`_kb_confirm_photo_delete()`/
     `_is_past_or_too_far()` стали async (были `def`) — все вызовы обёрнуты
     в `await`.
     **Проход по пункту 3 плана («файл за файлом») завершён** — все 17
     файлов из списка (`services_add.py` … `events_add.py`) обработаны.
     Осознанно оставленные владельцем на будущее хвосты: секреты в git/логах
     перед деплоем (см. `[[secrets-deferred]]`), словари-константы в
     `releases.py` и названия месяцев/дней недели в `events_view.py`
     (см. записи выше), переключение языка RU/EN/KK (весь этот перенос —
     подготовка к нему, сам переключатель ещё предстоит реализовать).
     Порядок действий на файл, наработанный за проход (пригодится, если
     будут ещё файлы с хардкодом или к переносу вернутся по новой причине):
     > 1. `grep -nE '(send_message|\.answer|edit_message_text|edit_text|caption=)\s*\(' app/routers/<файл>.py`
     >    — найти хардкод-строки; для многострочных f-string блоков и
     >    билдеров карточек (list.append(...) с лейблами) смотреть Read
     >    вокруг находок, а не только однострочные вызовы.
     > 2. Для каждой уникальной фразы: `sqlite3 dev.db "SELECT code, text_ru
     >    FROM BotText WHERE text_ru LIKE '%фраза%'"` — проверить, нет ли
     >    уже готового кода (в БД лежит заранее подготовленный набор `err_*`
     >    и то, что заведено в этой же миграции — коды переиспользуются
     >    между файлами при точном совпадении текста и смысла).
     > 3. Если нет — `sqlite3 dev.db "INSERT INTO BotText (code, title,
     >    text_ru, text_en) VALUES (...)"` (text_en — по возможности сразу,
     >    не пустым, если не уверены в переводе — можно оставить '').
     > 4. В коде: `await get_text("code", "ru") or "исходный текст"`
     >    (тот же fallback-идиом, что и во всём проекте). Добавить `get_text`
     >    в импорт файла, если его там ещё нет (`from app.routers.utils
     >    import ..., get_text` или `from app.texts import get_text` — в
     >    файле уже могут быть оба варианта, смотреть что уже импортировано).
     >    Для шаблонов с переменными — `.format(placeholder=value)`.
     > 5. Если метка используется и для показа, и для парсинга уже
     >    отправленного текста (`if stripped == "..."`) — завести ОДНУ
     >    переменную `label = await get_text(...)` и использовать её в обеих
     >    ролях (см. `vacancy_view.py`, `vac_extend_listing` — пример в коде
     >    и комментарий там же).
     > 6. Проверить: `python3 -m py_compile app/routers/<файл>.py`,
     >    `poetry run python -c "import app.main"` (ОБЯЗАТЕЛЬНО — тесты
     >    одни не ловят циклические импорты, см. инцидент с admin_panel.py
     >    выше), `poetry run python -m unittest discover -s tests` (НЕ pytest).
     > 7. `git add` только тронутые файлы + CLAUDE.md, закоммитить с кратким
     >    описанием (какие коды заведены/переиспользованы).
     > 8. Обновить этот пункт плана (сдвинуть указатель «продолжить отсюда»
     >    на следующий файл) — коротко, без разрастания текста; старые
     >    построчные логи можно сворачивать в сводку, как уже сделано выше
     >    для шага 1 и первой части шага 2.
     > **Остановки:** после каждого файла спросить пользователя, продолжать
     > ли — он лично следит за пятичасовым лимитом использования и сам
     > решает, когда сделать паузу; не полагаться на то, что лимит виден
     > модели (не виден).
     > **Owner-only файлы** (`admin_panel.py`, `admin_fields.py`,
     > `admin_analytics.py`) тоже переносятся, но по остаточному принципу;
     > в `admin_analytics.py` уже сознательно оставлены нетронутыми большие
     > f-string отчёты (см. запись ниже) — та же логика применима и к другим
     > объёмным built-in отчётам/дампам, если такие найдутся.
     > **Отдельно найденный мёртвый код** в `services_edit.py` вынесен
     > отдельным фоновым тудушником (spawn_task) — не блокирует эту работу,
     > не нужно к нему возвращаться в рамках этого плана.

     Решение по схеме БД (2026-07-20): отдельную таблицу под кнопки НЕ
     заводить — `menu` остаётся только для структурной переиспользуемой
     навигации (Назад/Главное меню, есть order_num/parent_code/
     callback_data), а разовые кнопки конкретных экранов идут в `BotText`
     вместе с сообщениями (обеим нужна только пара text_ru/text_en под
     кодом, лишняя таблица не нужна).
     Идём от маленьких файлов к большим (полный список по кол-ву
     `send_message|.answer|edit_message_text|caption=`, отсортирован
     по возрастанию — актуален на 2026-07-20, `for f in app/routers/*.py;
     do echo "$(grep -cE ...) $f"; done | sort -n`):
     `partner_view.py`(3) — **готово**, код `partner_card_unavailable`.
     `user_extra_fields.py`(4) — **готово**, 4 новых кода
     `extra_field_need_number`/`extra_field_not_video_file`/
     `extra_field_not_video_link`/`extra_field_need_video`.
     `events_admin.py`(16) — **готово**, плюс попутно найден и закрыт
     ещё один крупный кросс-файловый дубль, упущенный в шаге 2: «Нет
     доступа» (БЕЗ точки — не путать с «Нет доступа.» из шага 2) —
     46 мест в 4 файлах (`admin_analytics.py`, `admin_fields.py`,
     `admin_panel.py`, `events_admin.py`) → новый код
     `err_no_access_short`. Плюс 4 кода для `events_admin.py`:
     `events_admin_no_pending`/`events_admin_not_found`/
     `events_admin_stale_button`/`events_admin_already_processed`.
     **Урок:** при подсчёте кандидатов для дедупа не фильтровать только
     по точным строкам из плана — проверять и близкие варианты (с
     точкой/без, с restбукв) перед тем как считать шаг «закрытым».
     `admin_analytics.py` — **простые toast-и готовы** (2 новых кода
     `analytics_bad_owner_data`/`analytics_bad_listing_data`), но
     **основной текст файла (177 строк с кириллицей) сознательно НЕ
     тронут** — это заголовки/подписи больших отчётов аналитики,
     собираются в f-строки заранее (не литералы в вызовах `.answer()`),
     owner-only инструмент, объём труда несоразмерен пользе (никогда не
     переводится на другой язык). Возврат к этому — по явному запросу
     владельца, не проактивно.
     `services_edit.py`(23) — **готово**, 11 новых кодов
     (`services_edit_invalid_id`/`_title_prompt`/`_title_len_error`/
     `_title_saved`/`_descr_prompt`/`_descr_saved`/`_price_prompt`/
     `_price_saved`/`_invalid_params`/`_finished`/`_done`; шаблоны
     `_prompt` — с плейсхолдером `{current}`, `.format()`, по образцу
     `sell_choose_category`). **Находка (не наша задача сейчас, просто
     отметили):** часть хендлеров файла (`edit_title_start`,
     `edit_descr_start`, `edit_price_start`, `edit_extras_start`,
     `edit_finish`, callback-паттерн `service_legacy_edit:*`) похожа на
     мёртвый код — ни один callback_data с этим префиксом нигде не
     генерируется, только `service_legacy_edit_overview:` (алиас на
     новый флоу) реально достижим. Тексты перенесены на всякий случай
     (дёшево), но само удаление — отдельное решение владельца.
     `market_edit.py`(25) — **готово**, 4 новых кода: `err_session_lost_listing`
     (кросс-файловый дубль, 7 мест — ещё 1 в `market_edit_overview.py`,
     не пойман в шаге 2 т.к. там раньше было счёт 6 в одном файле),
     `market_edit_saved`, `market_edit_return_to_listing`,
     `market_edit_title_empty` (тоже задело 2-е место в
     `market_edit_overview.py`).
     `vacancy_edit_overview.py`(27) — **готово**: переиспользованы
     `err_no_rights`/`err_field_404` (готовые из старого набора),
     `services_edit_invalid_id`/`market_edit_title_empty` (уже заведены
     на этом проходе), плюс 3 новых: `err_session_lost_vacancy`,
     `vacancy_edit_price_empty`, `vacancy_edit_field_unavailable`.
     `artists.py`(29) — **готово**, 10 новых кодов (`music_*` — общие
     для музыкального слоя, т.к. 5 из 10 фраз дублировались и в
     `releases.py`, подключены сразу в обоих: `music_section_unavailable`,
     `artist_not_found`, `music_card_unavailable`,
     `music_no_rights_or_unavailable`, `music_no_rights_or_field_locked`,
     `music_field_cleared`, `music_link_needs_scheme`,
     `music_save_failed_no_rights`, `music_admin_only`, `music_not_found`).
     Заодно частично продвинули `releases.py` (8 мест этими же кодами) —
     он всё равно следующий в очереди.
     `feedback.py`(34) — **готово (пользовательская часть)**: 12 новых
     кодов (`feedback_removed`/`_thanks_will_answer`/`_thanks`/
     `_unavailable`/`_not_found`/`_reply_later`/`_reply_text_only`/
     `_delete_confirm`/`_deleted`/`_need_reply_thanks`/`_noneed_thanks`/
     `_mine_empty`). **Сознательно отложено:** уведомления АДМИНАМ
     (`_format_admin_notif`, `prompt` в `fb_reply_start`, короткие
     status_line вроде «🔔 Пользователь запросил ответ.») — сложные
     многоместные f-string шаблоны с несколькими `{переменными}`,
     видны только владельцу/админам, не пользователям. Перенос
     потребует продумать формат плейсхолдеров в BotText — вернуться
     отдельным заходом, не блокирует остальной прогресс.
     `market_edit_photos.py`(35)+`services_edit_photos.py`(35) —
     **готово, оба разом**: файлы зеркальны (как и раньше), 12 общих
     кодов `photo_edit_*` (`_session_stale`/`_session_lost`/`_max_3`/
     `_add_prompt`/`_need_one_photo`/`_swap_prompt`/`_need_photo`/
     `_nothing_to_add`/`_nothing_to_replace`/`_no_pending_action`/
     `_save_failed`/`_applied`). Два шаблона с плейсхолдером —
     `.format(count=...)`/`.format(idx=...)`, по тому же паттерну, что
     `services_edit_*_prompt` в `services_edit.py`.
     `vacancy_view.py`(37) — **частично готово**: все toast/простые
     сообщения и поиск (18 новых кодов: `vacancy_unavailable_archived`,
     `vacancy_extend_*` (4), `vacancy_close_*` (3), `vacancy_not_found`,
     `vacancy_invalid_id`, `vacancy_already_deleted`, `vacancy_deleted`,
     `vacancy_search_title`, `search_min_2_chars`,
     `search_typo_correction_note` (переиспользуемый — тот же текст
     будет и в `market_view.py`/`services_view.py`), `search_results_found`
     (шаблон с 3 плейсхолдерами), `vacancy_search_unavailable`; плюс
     переиспользованы `err_no_rights`/`feedback_deleted`).
     **Добито (2026-07-20, второй заход по тому же файлу):** карточки,
     breadcrumbs, кнопки — ещё ~28 новых кодов `vacancy_*`
     (`vacancy_no_title`, `vacancy_card_city`/`_category_path`/
     `_category_root`/`_payment`, `vacancy_contacts_mgmt_label`,
     `vacancy_closed_hidden`/`_closed_restore_hint`, `vacancy_no_city_set`,
     `vacancy_breadcrumb_city`/`_subcat`, `vacancy_choose_listing`,
     `vacancy_category_empty`, `vacancy_my_empty`/`_my_title`,
     `vacancy_delete_confirm_question`, кнопки `vacancy_btn_*` — 11 штук).
     **Важный архитектурный момент, зафиксирован в коде комментарием:**
     `vac_extend_listing`/`vac_close_listing` не просто показывают
     карточку — они ПЕРЕПАРСИВАЮТ уже отрисованный `cb.message.text`
     построчным сравнением с меткой «Контакты/Управление:» (и парой
     других), чтобы вырезать старый блок и вставить новый. Раньше эта
     метка была захардкожена в двух местах на функцию (рендер + проверка)
     — теперь обе стороны берут значение из ОДНОЙ переменной
     (`contacts_mgmt_label = await get_text(...)`), чтобы не разойтись.
     Но сам паттерн (парсинг уже отправленного текста вместо хранения
     состояния) при будущем переключении языка всё равно потребует
     ревизии: если пользователь сменит язык между показом карточки и
     кликом «Продлить», разбор по метке текущего (нового) языка не найдёт
     метку старого языка в уже отрисованном тексте. Не чинили сейчас —
     вне рамок текстового переноса, но это нужно будет учесть при
     реализации переключения языка.
     `⏳ До архивации:` — метка НЕ отсюда, а из `app/lifecycle.py`
     (`days_left_text()`), этот файл вне текущего аудита
     (`app/routers/*.py`) — отдельная будущая задача.
     `vacancy_view.py` теперь **полностью** переведён.
     `vacancy_edit_overview.py`(27), `artists.py`(29), `feedback.py`(34),
     `market_edit_photos.py`/`services_edit_photos.py`(35),
     `vacancy_view.py`(37), `services_add.py`(40), `admin_panel.py`(49),
     `admin_fields.py`(57), `market_add.py`(61), `vacancy_add.py`(62),
     `services_view.py`(70), `services_edit_overview.py`(73),
     `market_edit_overview.py`(78), `events_view.py`(86),
     `market_view.py`(89), `releases.py`(110), `events_add.py`(154).
     Счётчики выше не фильтрованы по кириллице (включают уже переведённые
     вызовы) — реальных строк на перенос меньше, уточнять по ходу.
     `admin_panel.py`/`admin_analytics.py`/`admin_fields.py` — owner-only,
     но по решению владельца тоже переносятся, просто не первыми.
- Публичный сайт для пользователей (веб-интерфейс объявлений)
- Запуск бота в Казахстане после отладки на Сербии
- Basic Auth + nginx для category_admin при деплое на сервер
- Добавление фото в объявление через веб-админ (через Telegram Bot API)
