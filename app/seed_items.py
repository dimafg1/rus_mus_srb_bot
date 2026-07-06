"""
Заполняет таблицу Item тестовыми записями.
Запускайте один раз:
    poetry run python -m app.seed_items
"""

import asyncio, random
from datetime import datetime
from app.models import utcnow_naive
from sqlalchemy import select
from app.database import init_db, SessionLocal
from app.models import City, Category, Item

SAMPLES = [
    # title, descr, city_slug, cat_slug
    ("Сессионный барабанщик", "Опыт 10 лет, играю с клик-треками.", "belgrade", "drums"),
    ("Саксофонист (альт)", "Jazz / funk, собственный инструмент.", "novisad", "voc_m"),
    ("Звукорежиссёр • сведение", "Pro Tools, UAD, работаю удалённо.", "belgrade", "mix"),
]

async def run():
    await init_db()
    async with SessionLocal() as s:
        for title, descr, city_slug, cat_slug in SAMPLES:
            city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
            cat =  (await s.execute(select(Category).where(Category.slug == cat_slug))).scalar_one()
            s.add(Item(
                city_id=city.id,
                category_id=cat.id,
                title=title,
                descr=descr,
                contact=f"@user{random.randint(1000,9999)}",
                created_at=utcnow_naive(),
            ))
        await s.commit()
        print("Dummy items inserted")

if __name__ == "__main__":
    asyncio.run(run())
