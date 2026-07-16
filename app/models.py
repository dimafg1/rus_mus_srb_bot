from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import (
    String, Text, DateTime, Boolean, Integer, Float, Column, ForeignKey
)

# --- Events ---
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from datetime import date, datetime


def utcnow_naive() -> datetime:
    """Naive UTC now — замена deprecated datetime.utcnow() с идентичным поведением.
    Naive-формат сохраняем сознательно: сравнения дат в БД и lifecycle.py
    строятся на naive-датах, aware-даты сломали бы работу со старыми записями."""
    return datetime.now(timezone.utc).replace(tzinfo=None)

# ─────────────────────────────────────────────────────────
# City
# ─────────────────────────────────────────────────────────
class City(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(sa_column=Column(String(100), index=True, nullable=False))
    name: str = Field(sa_column=Column(String(200), nullable=False))


# ─────────────────────────────────────────────────────────
# Category (иерархия через parent_id)
# ─────────────────────────────────────────────────────────
class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(sa_column=Column(String(100), index=True, nullable=False))
    name: str = Field(sa_column=Column(String(200), nullable=False))
    # FK переносим внутрь Column; SET NULL, чтоб можно было удалять родителя
    parent_id: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("category.id", ondelete="SET NULL"), index=True, nullable=True)
    )
    fields: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))

# ─────────────────────────────────────────────────────────
# Item (анкета для каталога)
# ─────────────────────────────────────────────────────────
class Item(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    city_id: int = Field(sa_column=Column(Integer, ForeignKey("city.id", ondelete="CASCADE"), index=True, nullable=False))
    category_id: int = Field(sa_column=Column(Integer, ForeignKey("category.id", ondelete="CASCADE"), index=True, nullable=False))

    title: str = Field(sa_column=Column(String(255), nullable=False))
    descr: Optional[str] = Field(default=None, sa_column=Column(Text))
    contact: str = Field(sa_column=Column(String(255), nullable=False))

    is_approved: bool = Field(default=False, sa_column=Column(Boolean, index=True, nullable=False))
    created_at: datetime = Field(default_factory=utcnow_naive, sa_column=Column(DateTime(timezone=True), nullable=False))


# ─────────────────────────────────────────────────────────
# Listing (объявление для барахолки)
# ─────────────────────────────────────────────────────────
class Listing(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    city_id: int = Field(sa_column=Column(Integer, ForeignKey("city.id", ondelete="CASCADE"), index=True, nullable=False))
    category_id: int = Field(sa_column=Column(Integer, ForeignKey("category.id", ondelete="CASCADE"), index=True, nullable=False))

    owner_id: int = Field(sa_column=Column(Integer, nullable=False))  # Telegram user ID автора
    title: str = Field(sa_column=Column(String(255), nullable=False))
    price: Optional[str] = Field(default=None, sa_column=Column(String(100)))
    descr: Optional[str] = Field(default=None, sa_column=Column(Text))
    contact: str = Field(sa_column=Column(String(255), nullable=False))
    photo_file_id: Optional[str] = Field(default=None, sa_column=Column(String(255)))

    is_sold: bool = Field(default=False, sa_column=Column(Boolean, index=True, nullable=False))
    created_at: datetime = Field(default_factory=utcnow_naive, sa_column=Column(DateTime(timezone=True), nullable=False))

    # ───────────────────────────────────────────────────────────────────────
    # Поле type предназначено для возможного разделения объявлений на разные
    # подтипы (например, "sell", "rent" и т.п.). В текущей реализации
    # конкретная логика типа не используется, однако столбец существует в
    # таблице базы данных. Поэтому модель включает атрибут type, чтобы
    # SQLModel корректно сопоставлял его со схемой БД. Тип TEXT выбран для
    # максимальной совместимости.
    type: Optional[str] = Field(default=None, sa_column=Column(Text))

    # ───────────────────────────────────────────────────────────────────────
    # Поле flex хранит сериализованное значение дополнительных полей
    # (flexible/extra fields), которые пользователь может заполнить при
    # создании объявления либо после публикации. Это поле в БД имеет тип
    # TEXT, поэтому в коде используется строковый тип. Значение ожидается
    # в формате JSON (строка), где ключи совпадают с ключами полей,
    # определённых в Category.fields, а значения — ответы пользователя.
    flex: Optional[str] = Field(default=None, sa_column=Column(Text))

    extra_category_id1: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("category.id", ondelete="SET NULL"), index=True, nullable=True),
    )

    extra_category_id2: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, ForeignKey("category.id", ondelete="SET NULL"), index=True, nullable=True),
    )

    # ───────────────────────────────────────────────────────────────────────
    # Жизненный цикл объявления:
    # active   — показывается пользователям
    # archived — скрыто из поиска/выдачи, доступно для админа/аналитики
    status: str = Field(
        default="active",
        sa_column=Column(String(50), index=True, nullable=False),
    )

    # Дата, когда объявление должно быть архивировано
    expires_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    # Дата фактической архивации
    archived_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )

    # Причина архивации:
    # expired / sold / closed / unpublished / user_deleted / admin_removed / event_passed
    archive_reason: Optional[str] = Field(
        default=None,
        sa_column=Column(String(50), index=True, nullable=True),
    )

    # Кто архивировал: user / admin / system
    archived_by: Optional[str] = Field(
        default=None,
        sa_column=Column(String(50), nullable=True),
    )

    # Telegram ID пользователя или админа, выполнившего действие
    archived_by_user_id: Optional[int] = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )

    # Когда отправлялось уведомление о скорой архивации
    reminded_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )




# ─────────────────────────────────────────────────────────
# Vacancy (биржа музыкантов)
# ─────────────────────────────────────────────────────────
class Vacancy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    city_id: int = Field(sa_column=Column(Integer, ForeignKey("city.id", ondelete="CASCADE"), index=True, nullable=False))
    role: str = Field(sa_column=Column(String(200), nullable=False))  # Вокалист, Гитарист и т.п.
    descr: Optional[str] = Field(default=None, sa_column=Column(Text))
    contact: str = Field(sa_column=Column(String(255), nullable=False))

    owner_id: int = Field(sa_column=Column(Integer, nullable=False))  # Telegram user ID автора
    created_at: datetime = Field(default_factory=utcnow_naive, sa_column=Column(DateTime(timezone=True), nullable=False))
    is_closed: bool = Field(default=False, sa_column=Column(Boolean, index=True, nullable=False))


# ─────────────────────────────────────────────────────────
# BotText (тексты для сообщений, меню и т.п.)
# Таблица была с нестандартным именем — сохраняем как есть.
# ─────────────────────────────────────────────────────────
class BotText(SQLModel, table=True):
    __tablename__ = "BotText"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(sa_column=Column(String(100), unique=True, nullable=False))
    title: Optional[str] = Field(default=None, sa_column=Column(String(255)))
    text_ru: str = Field(default="", sa_column=Column(Text, nullable=False))
    text_en: str = Field(default="", sa_column=Column(Text, nullable=False))
    updated_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))


# ─────────────────────────────────────────────────────────
# Menu (главное/подменю)
# ─────────────────────────────────────────────────────────
class Menu(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(sa_column=Column(String(100), unique=True, nullable=False))
    parent_code: str = Field(sa_column=Column(String(100), index=True, nullable=False))
    text: str = Field(sa_column=Column(Text, nullable=False))       # text_ru (основной)
    text_en: str = Field(default="", sa_column=Column(Text, nullable=False))
    callback_data: str = Field(sa_column=Column(String(255), nullable=False))
    order_num: int = Field(sa_column=Column(Integer, nullable=False))
    visible: int = Field(sa_column=Column(Integer, nullable=False))
    lang: str = Field(default="ru", sa_column=Column(String(10), nullable=False))
    icon: Optional[str] = Field(default=None, sa_column=Column(String(50)))


# ─────────────────────────────────────────────────────────
# BotMessage (сообщения бота для очистки чата после рестарта)
# ─────────────────────────────────────────────────────────
class BotMessage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    chat_id: int = Field(
        sa_column=Column(Integer, index=True, nullable=False)
    )

    message_id: int = Field(
        sa_column=Column(Integer, index=True, nullable=False)
    )

    created_at: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ─────────────────────────────────────────────────────────
# BotUser (трекинг пользователей: ник, id, время последнего захода)
# ─────────────────────────────────────────────────────────
class BotUser(SQLModel, table=True):
    __tablename__ = "BotUser"
    id: Optional[int] = Field(default=None, primary_key=True)

    user_id: int = Field(
        sa_column=Column(Integer, unique=True, index=True, nullable=False)
    )
    username: Optional[str] = Field(
        default=None, sa_column=Column(String(100))
    )
    full_name: Optional[str] = Field(
        default=None, sa_column=Column(String(255))
    )
    last_seen: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    first_seen: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    # Источник первого входа: deep-link параметр /start (unixound, антикафе,
    # канал и т.п.). Заполняется один раз при создании записи, никогда не
    # перезаписывается. NULL — органика либо пользователь старше этой колонки.
    first_source: Optional[str] = Field(
        default=None, sa_column=Column(String(64))
    )


# ─────────────────────────────────────────────────────────
# FeatureFlag — выключатели функций (Strategy v2: всё новое пишется
# «под выключателем», включается по одному). Проверка: app/features.py.
# audience: all | admins | список user_id через запятую.
# ─────────────────────────────────────────────────────────
class FeatureFlag(SQLModel, table=True):
    __tablename__ = "feature_flags"
    id: Optional[int] = Field(default=None, primary_key=True)

    key: str = Field(
        sa_column=Column(String(64), unique=True, index=True, nullable=False)
    )
    enabled: bool = Field(
        default=False, sa_column=Column(Boolean, nullable=False, default=False)
    )
    audience: str = Field(
        default="all", sa_column=Column(String(255), nullable=False, default="all")
    )
    note: Optional[str] = Field(default=None, sa_column=Column(String(255)))
    updated_at: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ─────────────────────────────────────────────────────────
# Музыкальный слой: Исполнители и Релизы (журнал решений Р-11/Р-12).
# Artist — постоянная сущность музыкального проекта (не Услуга!):
# минимальная версия из арки релизов; полноценный раздел — вторая арка.
# Релиз живёт в listing (type='release', карточка/фото/автор общие)
# + release_meta (свой жизненный цикл: без сроков и продлений)
# + release_track (треки альбома по одному, порядок и имена правятся).
# ─────────────────────────────────────────────────────────
class Artist(SQLModel, table=True):
    __tablename__ = "artist"
    id: Optional[int] = Field(default=None, primary_key=True)

    name: str = Field(sa_column=Column(String(128), nullable=False))
    # соло | группа | дуэт | проект | dj | другое
    artist_type: str = Field(
        default="группа", sa_column=Column(String(32), nullable=False, default="группа")
    )
    photo_file_id: Optional[str] = Field(default=None, sa_column=Column(Text))
    # Управляющий пользователь (несколько управляющих — вторая арка, Р-12)
    owner_user_id: int = Field(sa_column=Column(Integer, index=True, nullable=False))
    # active | hidden (скрыт модератором)
    status: str = Field(
        default="active", sa_column=Column(String(16), nullable=False, default="active")
    )
    created_at: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ReleaseMeta(SQLModel, table=True):
    __tablename__ = "release_meta"
    id: Optional[int] = Field(default=None, primary_key=True)

    listing_id: int = Field(sa_column=Column(Integer, unique=True, index=True, nullable=False))
    artist_id: int = Field(sa_column=Column(Integer, index=True, nullable=False))
    # single | ep | album | clip | live
    release_type: str = Field(sa_column=Column(String(16), nullable=False))
    release_date: Optional[str] = Field(default=None, sa_column=Column(String(32)))
    genre: Optional[str] = Field(default=None, sa_column=Column(String(64)))
    recorded_at: Optional[str] = Field(default=None, sa_column=Column(String(128)))  # «где записано»
    # Ссылки на площадки: JSON [{"label": "Spotify", "url": "..."}].
    # YouTube-ссылка дополнительно кладётся в текст карточки → встроенный плеер.
    links: Optional[str] = Field(default=None, sa_column=Column(Text))
    # Прикреплённый клип (видео целиком; аудио живёт в release_track)
    video_file_id: Optional[str] = Field(default=None, sa_column=Column(Text))
    video_file_unique_id: Optional[str] = Field(default=None, sa_column=Column(String(64)))
    # Свой жизненный цикл (НЕ listing.status): published | hidden | deleted
    status: str = Field(
        default="published", sa_column=Column(String(16), nullable=False, default="published")
    )
    created_at: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


class ReleaseTrack(SQLModel, table=True):
    __tablename__ = "release_track"
    id: Optional[int] = Field(default=None, primary_key=True)

    listing_id: int = Field(sa_column=Column(Integer, index=True, nullable=False))
    position: int = Field(sa_column=Column(Integer, nullable=False))
    title: Optional[str] = Field(default=None, sa_column=Column(String(255)))
    file_id: str = Field(sa_column=Column(Text, nullable=False))
    file_unique_id: Optional[str] = Field(default=None, sa_column=Column(String(64)))
    duration: Optional[int] = Field(default=None, sa_column=Column(Integer))  # секунды
    file_name: Optional[str] = Field(default=None, sa_column=Column(String(255)))
    mime_type: Optional[str] = Field(default=None, sa_column=Column(String(64)))


# ─────────────────────────────────────────────────────────
# Campaign — партнёрские кампании (Strategy v2, слой 2 §5).
# UNIXOUND — обычная кампания, не хардкод. Ротация: app/campaigns.py.
# Показы/клики — события partner_shown / partner_opened в analytics_events.
# ─────────────────────────────────────────────────────────
class Campaign(SQLModel, table=True):
    __tablename__ = "campaign"
    id: Optional[int] = Field(default=None, primary_key=True)

    key: str = Field(  # слаг для callback_data и аналитики
        sa_column=Column(String(64), unique=True, index=True, nullable=False)
    )
    partner: str = Field(sa_column=Column(String(128), nullable=False))
    line_text: str = Field(  # текст кнопки-строки в меню
        sa_column=Column(String(64), nullable=False)
    )
    card_text: str = Field(sa_column=Column(Text, nullable=False))  # HTML карточки
    photo_file_id: Optional[str] = Field(default=None, sa_column=Column(Text))
    buttons: Optional[str] = Field(  # JSON: [{"text": ..., "url": ...}]
        default=None, sa_column=Column(Text)
    )
    placement: str = Field(
        default="main_menu", sa_column=Column(String(32), nullable=False, default="main_menu")
    )
    weight: int = Field(default=1, sa_column=Column(Integer, nullable=False, default=1))
    active: bool = Field(default=False, sa_column=Column(Boolean, nullable=False, default=False))
    starts_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    ends_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    created_at: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ─────────────────────────────────────────────────────────
# AnalyticsEvent — единый поток аналитических событий.
# Словарь типов и правила записи: app/analytics.py.
# Открытия карточек/контакты живут в listing_views, поиск — в search_log;
# сюда они не дублируются.
# ─────────────────────────────────────────────────────────
class AnalyticsEvent(SQLModel, table=True):
    __tablename__ = "analytics_events"
    id: Optional[int] = Field(default=None, primary_key=True)

    event_type: str = Field(
        sa_column=Column(String(64), nullable=False, index=True)
    )
    user_id: Optional[int] = Field(
        default=None, sa_column=Column(Integer, index=True)
    )
    section: Optional[str] = Field(  # market / services / vacancy / events
        default=None, sa_column=Column(String(32))
    )
    entity_type: Optional[str] = Field(  # listing / campaign / ...
        default=None, sa_column=Column(String(32))
    )
    entity_id: Optional[int] = Field(default=None, sa_column=Column(Integer))
    source: Optional[str] = Field(default=None, sa_column=Column(String(64)))
    meta: Optional[str] = Field(default=None, sa_column=Column(Text))  # JSON
    created_at: datetime = Field(
        default_factory=utcnow_naive,
        sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )


# ─────────────────────────────────────────────────────────
# Profile
# ─────────────────────────────────────────────────────────
class Profile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    city_id: int = Field(sa_column=Column(Integer, ForeignKey("city.id", ondelete="CASCADE"), index=True, nullable=False))
    category_id: int = Field(sa_column=Column(Integer, ForeignKey("category.id", ondelete="CASCADE"), index=True, nullable=False))
    owner_id: int = Field(sa_column=Column(Integer, nullable=False))  # Telegram user ID автора

    title: str = Field(sa_column=Column(String(255), nullable=False))
    name: Optional[str] = Field(default=None, sa_column=Column(String(255)))
    contact: str = Field(sa_column=Column(String(255), nullable=False))
    price_desc: Optional[str] = Field(default=None, sa_column=Column(String(255)))
    descr: Optional[str] = Field(default=None, sa_column=Column(Text))

    photo_file_ids: Optional[str] = Field(default=None, sa_column=Column(Text))
    video_file_ids: Optional[str] = Field(default=None, sa_column=Column(Text))
    audio_file_ids: Optional[str] = Field(default=None, sa_column=Column(Text))
    portfolio_file_ids: Optional[str] = Field(default=None, sa_column=Column(Text))

    plan: Optional[str] = Field(default="free", sa_column=Column(String(50)))
    order: Optional[int] = Field(default=None, sa_column=Column(Integer))
    rating: Optional[float] = Field(default=None, sa_column=Column(Float))
    moderation_status: Optional[str] = Field(default="pending", sa_column=Column(String(50)))
    is_active: Optional[int] = Field(default=1, sa_column=Column(Integer))

    created_at: datetime = Field(default_factory=utcnow_naive, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=utcnow_naive, sa_column=Column(DateTime(timezone=True), nullable=False))

