from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime

# ————————————————————————————————————————————————————————————
# Модель города
# ————————————————————————————————————————————————————————————
class City(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, nullable=False)
    name: str


# ————————————————————————————————————————————————————————————
# Модель категории (с иерархией через parent_id)
# ————————————————————————————————————————————————————————————
class Category(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    slug: str = Field(index=True, nullable=False)
    name: str
    parent_id: Optional[int] = Field(default=None, foreign_key="category.id")


# ————————————————————————————————————————————————————————————
# Анкета (Item) для каталога
# ————————————————————————————————————————————————————————————
class Item(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    city_id: int = Field(foreign_key="city.id", nullable=False)
    category_id: int = Field(foreign_key="category.id", nullable=False)

    title: str
    descr: Optional[str] = None
    contact: str

    is_approved: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ————————————————————————————————————————————————————————————
# Объявление (Listing) для барахолки
# ————————————————————————————————————————————————————————————
class Listing(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    city_id: int = Field(foreign_key="city.id", nullable=False)
    category_id: int = Field(foreign_key="category.id", nullable=False)

    owner_id: int  # Telegram user ID автора
    title: str
    price: Optional[str] = None
    descr: Optional[str] = None
    contact: str
    photo_file_id: Optional[str] = None

    is_sold: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ————————————————————————————————————————————————————————————
# Вакансия (Vacancy) для биржи музыкантов
# ————————————————————————————————————————————————————————————
class Vacancy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    city_id: int = Field(foreign_key="city.id", nullable=False)
    role: str              # e.g. "Вокалист", "Гитарист" и т.п.
    descr: Optional[str] = None
    contact: str

    owner_id: int          # Telegram user ID автора
    created_at: datetime = Field(default_factory=datetime.utcnow)
    is_closed: bool = Field(default=False)
