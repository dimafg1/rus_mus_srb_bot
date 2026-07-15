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

