import asyncio
from datetime import datetime
from app.models import utcnow_naive
from sqlalchemy import select
from app.database import init_db, SessionLocal
from app.models import Vacancy, City

DEMO = [
    ("belgrade", "Вокалист", "Требуется для рок-группы, репетиции 2×нед."),
    ("belgrade", "Гитарист", "Фьюжн-трио, опыт обязателен."),
    ("novisad",  "Басист",   "Стиль funk/jazz, амп нужен свой."),
]

async def run():
    await init_db()
    async with SessionLocal() as s:
        for city_slug, role, descr in DEMO:
            city = (await s.execute(select(City).where(City.slug == city_slug))).scalar_one()
            s.add(Vacancy(
                city_id=city.id,
                role=role,
                descr=descr,
                contact="@"+f"user{city_slug}",
                owner_id=0,
                created_at=utcnow_naive(),
            ))
        await s.commit()
        print("Demo vacancies inserted")

if __name__ == "__main__":
    asyncio.run(run())
