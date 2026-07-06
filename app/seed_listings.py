import asyncio
from datetime import datetime
from app.models import utcnow_naive

from sqlalchemy import select, text
from sqlalchemy.exc import NoResultFound
from app.database import init_db, SessionLocal
from app.models import Listing, City, Category

# Демо-объявления: (city_slug, category_slug, title, price, descr, contact, photo_file_id?)
DEMO = [
    ("belgrade", "studio_hw", "Focusrite Scarlett 2i2", "120 €",
     "Лучший звук для home-studio", "@demo_user1", None),
    ("belgrade", "guit",      "Fender Stratocaster",    "450 €",
     "Почти новая, 2 года",     "@demo_user2", None),
    ("novisad",  "drums",      "Pearl Export",           "600 €",
     "Полный комплект",          "@demo_user3", None),
    ("novisad",  "keys_hw",    "Yamaha MX49",            "300 €",
     "Синтезатор для живых выступлений", "@demo_user4", None),
]

async def run():
    # 1) создаём таблицы
    await init_db()

    async with SessionLocal() as session:
        # 2) очищаем старые демо-объявления
        await session.execute(text("DELETE FROM listing"))
        await session.commit()

        # 3) вставляем новые
        for city_slug, cat_slug, title, price, descr, contact, photo in DEMO:
            # город
            try:
                city = (
                    await session.execute(
                        select(City).where(City.slug == city_slug)
                    )
                ).scalar_one()
            except NoResultFound:
                print(f"⚠️ City '{city_slug}' not found, skipping '{title}'")
                continue

            # категория
            try:
                category = (
                    await session.execute(
                        select(Category).where(Category.slug == cat_slug)
                    )
                ).scalar_one()
            except NoResultFound:
                print(f"⚠️ Category '{cat_slug}' not found, skipping '{title}'")
                continue

            listing = Listing(
                city_id=city.id,
                category_id=category.id,
                owner_id=0,               # фиктивный владелец для демо
                title=title,
                price=price,
                descr=descr,
                contact=contact,
                photo_file_id=photo,
                created_at=utcnow_naive(),
                is_sold=False,
            )
            session.add(listing)

        await session.commit()
        print("✅ Demo listings inserted")

if __name__ == "__main__":
    asyncio.run(run())
