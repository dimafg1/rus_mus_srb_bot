"""
app/seed_db.py
--------------
Создаёт (если нужно) SQLite‑базу dev.db и заполняет:
• Города (Белград, Нови Сад)
• Полное дерево категорий каталога

Запускайте:
    poetry run python -m app.seed_db
"""

import asyncio
from sqlalchemy import select
from app.database import init_db, SessionLocal
from app.models import City, Category


# --------------------------------------------------------------------------- #
# Города
# --------------------------------------------------------------------------- #
CITIES = [
    ("belgrade", "Белград"),
    ("novisad",  "Нови Сад"),
    ("other", "Другой город"),
]

# --------------------------------------------------------------------------- #
# Дерево категорий
# --------------------------------------------------------------------------- #
TREE = {
    # ---------------------- Музыканты ----------------------
    "mus": {
        "name": "Музыканты",
        "children": {
            "strings": {
                "name": "Смычковые",
                "children": {
                    "violin": {"name": "Скрипка"},
                    "viola":  {"name": "Альт"},
                    "cello":  {"name": "Виолончель"},
                },
            },
            "guit": {
                "name": "Гитары",
                "children": {
                    "acgtr": {"name": "Акустическая гитара"},
                    "elgtr": {"name": "Электрогитара"},
                },
            },
            "bass": {
                "name": "Басы",
                "children": {
                    "ebass":  {"name": "Бас‑гитара"},
                    "acbass": {"name": "Акуст. бас‑гитара"},
                    "dbass":  {"name": "Контрабас"},
                },
            },
            "perc": {                                     # ➊ новый блок
                "name": "Ударные и перкуссия",
                "children": {
                    "drums":      {"name": "Барабаны"},
                    "cajon":      {"name": "Кахон"},
                    "perc_misc":  {"name": "Перкуссия"},
                },
            },
        },
    },

    # ---------------------- Вокал --------------------------
    "voc": {
        "name": "Вокал",
        "children": {
            "voc_m": {"name": "Мужской вокал"},
            "voc_f": {"name": "Женский вокал"},
        },
    },

    # ---------------- Коллектив / Группа -------------------
    "band": {"name": "Коллектив / Группа"},

    # ---------------- Звук / Продакшн ----------------------
    "prod": {
        "name": "Звук / Продакшн",
        "children": {
            "arr":     {"name": "Аранжировка"},
            "mix":     {"name": "Сведение"},
            "master":  {"name": "Мастеринг"},
            "compose": {"name": "Написание музыки"},
        },
    },

    # ------------------ Преподавание -----------------------
    "teach": {"name": "Преподавание"},

    # --------------- Студии и площадки ---------------------
    "infra": {
        "name": "Студии и площадки",
        "children": {
            "studio":    {"name": "Студии звукозаписи"},
            "rehearsal": {"name": "Репетиционные базы"},
        },
    },

    # ------------------- Оборудование ----------------------
    "equip": {
        "name": "Оборудование",
        "children": {
            "studio_hw": {"name": "Студийное оборудование"},
            "live_pa":   {"name": "Концертное PA"},
            "mics":      {"name": "Микрофоны"},
            "amps":      {"name": "Усилители и кабинеты"},
            "keys_hw":   {"name": "Синтезаторы / клавиши"},
            "fx":        {"name": "Педали / рэковый FX"},
            "cables":    {"name": "Кабели, коммутация"},
            "soft":      {"name": "Софт / плагины"},
        },
    },

    # ------ Организация и менеджмент (без agency) ---------
    "org": {
        "name": "Организация и менеджмент",
        "children": {
            "event": {"name": "Организация мероприятий"},
            "mc":    {"name": "Ведущий / MC"},
        },
    },

    # --------------------- Подкасты ------------------------
    "pod": {"name": "Подкасты"},
}


# --------------------------------------------------------------------------- #
# Рекурсивное сохранение ветки
# --------------------------------------------------------------------------- #
async def save_branch(session, slug: str, node: dict, parent_id: int | None = None):
    cat = Category(slug=slug, name=node["name"], parent_id=parent_id)
    session.add(cat)
    await session.flush()                   # получаем cat.id
    for sub_slug, sub_node in node.get("children", {}).items():
        await save_branch(session, sub_slug, sub_node, cat.id)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
async def run():
    await init_db()

    async with SessionLocal() as session:
        already = (await session.execute(select(City))).scalars().first()
        if already:
            print("DB already populated – nothing to do.")
            return

        # Города
        session.add_all([City(slug=s, name=n) for s, n in CITIES])

        # Категории
        for root_slug, root_node in TREE.items():
            await save_branch(session, root_slug, root_node)

        await session.commit()
        print("DB seeded.")


if __name__ == "__main__":
    asyncio.run(run())
