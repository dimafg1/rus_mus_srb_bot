from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import (
    String, Text, DateTime, Boolean, Integer, Float, Column, ForeignKey
)

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
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))


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
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))


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
    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    is_closed: bool = Field(default=False, sa_column=Column(Boolean, index=True, nullable=False))


# ─────────────────────────────────────────────────────────
# BotText (тексты для сообщений, меню и т.п.)
# Таблица была с нестандартным именем — сохраняем как есть.
# ─────────────────────────────────────────────────────────
class BotText(SQLModel, table=True):
    __tablename__ = "BotText"

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(sa_column=Column(String(100), index=True, nullable=False))
    title: Optional[str] = Field(default=None, sa_column=Column(String(255)))
    text: str = Field(sa_column=Column(Text, nullable=False))
    lang: str = Field(default="ru", sa_column=Column(String(10), index=True, nullable=False))
    updated_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))


# ─────────────────────────────────────────────────────────
# Menu (главное/подменю)
# ─────────────────────────────────────────────────────────
class Menu(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(sa_column=Column(String(100), index=True, nullable=False))
    parent_code: str = Field(sa_column=Column(String(100), index=True, nullable=False))
    text: str = Field(sa_column=Column(Text, nullable=False))
    callback_data: str = Field(sa_column=Column(String(255), nullable=False))
    order_num: int = Field(sa_column=Column(Integer, nullable=False))
    visible: int = Field(sa_column=Column(Integer, nullable=False))  # оставляю int, как было
    lang: str = Field(sa_column=Column(String(10), nullable=False))
    icon: Optional[str] = Field(default=None, sa_column=Column(String(50)))


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

    created_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=datetime.utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
