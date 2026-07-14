# -*- coding: utf-8 -*-
"""Миграция каталога на структуру v2 (docs/catalog_v2_plan.md).

Запуск:  python scripts/migrate_catalog_v2.py [путь_к_БД]   (по умолчанию dev.db)

Делает: переименования, переносы, новые категории, слияния (с переносом
объявлений), flex-поля по плану (legacy-ключи сохраняются — старые значения
объявлений продолжают отображаться). Раздел Афиша (root 100) не трогает.
"""
import json
import sqlite3
import sys
from pathlib import Path

DB = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent / "dev.db")


# ── конструкторы полей ──────────────────────────────────────────────
def t(label, key):
    return {"type": "text", "label": label, "key": key, "required": False}


def n(label, key):
    return {"type": "number", "label": label, "key": key, "required": False}


def sel(label, key, *opts):
    return {"type": "select", "label": label, "key": key, "options": list(opts), "required": False}


def chk(label, key):
    return {"type": "checkbox", "label": label, "key": key, "required": False}


def vid():
    return {"type": "video", "label": "Видео", "key": "video", "required": False}


# ── план: изменения существующих категорий ──────────────────────────
# id: {name, parent, fields, order}; поле отсутствует — не трогаем.
# fields мержатся с существующими: legacy-ключи, которых нет в новом
# наборе (включая __meta), сохраняются в конце списка.
UPDATES = {
    # Корни разделов: наследуемые поля
    30: {"fields": [
        sel("Состояние", "condition", "новое", "как новое", "б/у", "на запчасти"),
        t("Бренд", "brand"), n("Год выпуска", "year"),
        chk("Торг уместен", "negotiable"), vid(),
    ]},
    80: {"fields": [vid(), n("Опыт, лет", "experience_years"), t("Языки", "languages")]},
    90: {"fields": [
        sel("Формат", "employment_format", "постоянный состав", "разовый проект",
            "сессионная работа", "подмена"),
        t("Жанр", "genre"), t("Репетиции: район и частота", "rehearsals"),
    ]},

    # ── Барахолка ──
    102: {"name": "🎸 Гитары", "parent": 30, "order": 10, "fields": [
        chk("Для левши", "lefty"), chk("Чехол/кейс в комплекте", "case_included")]},
    330: {"name": "Электрогитары", "order": 10, "fields": [
        sel("Звукосниматели", "pickups", "SSS", "HSS", "HH", "P90", "другое"),
        n("Ладов", "frets"), t("Мензура", "scale")]},
    331: {"name": "Акустические гитары", "order": 20, "fields": [
        chk("Со звукоснимателем", "has_pickup"),
        sel("Корпус", "body_type", "дредноут", "джамбо", "фолк", "парлор", "другое")]},
    103: {"name": "Бас-гитары", "parent": 102, "order": 40, "fields": [
        sel("Струн", "strings_count", "4", "5", "6"), chk("Безладовый", "fretless")]},

    101: {"name": "🎹 Клавишные", "parent": 30, "order": 20, "fields": [
        n("Количество клавиш", "number_of_keys"),
        sel("Клавиатура", "key_action", "невзвешенная", "полувзвешенная", "молоточковая")]},
    348: {"order": 10, "fields": [n("Полифония", "polyphony"), chk("Секвенсор", "sequencer")]},
    349: {"name": "Сценические пиано / электропиано", "order": 20},
    350: {"name": "Рабочие станции / грувбоксы", "order": 30, "fields": [n("Пэдов", "pads_count")]},
    352: {"name": "MIDI-клавиатуры / контроллеры", "order": 40, "fields": [
        chk("Фейдеры/энкодеры", "faders_encoders")]},

    107: {"name": "🥁 Ударные и перкуссия", "parent": 30, "order": 30, "fields": []},
    108: {"name": "Перкуссия", "parent": 107, "order": 40},

    105: {"name": "🎻 Смычковые", "parent": 30, "order": 40, "fields": [
        sel("Размер", "size", "4/4", "3/4", "1/2", "1/4", "1/8"),
        chk("Смычок и футляр в комплекте", "bow_case"),
        sel("Происхождение", "origin", "мастеровая", "фабричная")]},
    104: {"name": "🎷 Духовые", "parent": 30, "order": 50, "fields": [
        t("Строй", "wind_key"), chk("Кейс в комплекте", "case_included"),
        chk("Мундштук в комплекте", "mouthpiece")]},
    106: {"name": "🪗 Аккордеоны / Баяны", "parent": 30, "order": 60, "fields": [
        sel("Басов", "bass_count", "48", "72", "96", "120"), n("Регистров", "registers")]},

    33: {"name": "🎤 Микрофоны", "order": 70, "fields": [
        sel("Назначение", "mic_purpose", "вокал", "инструменты", "студия", "стрим")]},
    321: {"name": "Динамические", "order": 10},
    322: {"name": "Конденсаторные", "order": 20, "fields": [
        sel("Диафрагма", "diaphragm", "большая", "малая")]},

    353: {"name": "🎚 Студийное оборудование", "order": 80},
    32: {"name": "Аудиоинтерфейсы / звуковые карты", "parent": 353, "order": 10, "fields": [
        n("Входов", "inputs"), n("Выходов", "outputs"),
        sel("Подключение", "connection", "USB", "Thunderbolt", "другое")]},

    37: {"name": "🎸 Усилители / Кабинеты", "order": 100, "fields": [
        sel("Тип", "amp_type", "ламповый", "транзисторный", "гибрид", "моделирующий"),
        n("Мощность, Вт", "power_watt")]},
    335: {"name": "Гитарные усилители", "order": 10, "fields": [
        sel("Формат", "amp_format", "голова", "комбо"), n("Каналов", "channels_count")]},
    336: {"name": "Басовые усилители", "order": 20, "fields": [
        sel("Формат", "amp_format", "голова", "комбо")]},
    337: {"name": "Кабинеты", "order": 30, "fields": [
        t("Конфигурация", "cab_config"), sel("Сопротивление, Ом", "impedance", "4", "8", "16")]},

    36: {"name": "🎛 Педали / Процессоры", "order": 110, "fields": []},
    345: {"name": "Процессоры / Multi-FX", "order": 20, "fields": [
        chk("Педаль экспрессии", "expression_pedal")]},
    346: {"name": "Педалборды / питание", "order": 30, "fields": [
        sel("Тип", "board_type", "педалборд", "блок питания", "кейс")]},

    34: {"name": "🎧 Наушники", "order": 120, "fields": [
        sel("Тип", "hp_type", "закрытые", "открытые", "полуоткрытые"),
        n("Импеданс, Ом", "impedance_ohm")]},
    47: {"name": "🎛️ DJ-оборудование", "order": 130, "fields": [
        sel("Тип", "dj_type", "контроллер", "проигрыватель", "микшер", "комплект"),
        n("Каналов", "dj_channels")]},
    38: {"name": "🖥 Софт / Плагины", "order": 140, "fields": [
        sel("Платформа", "platform", "Win", "Mac", "обе"), t("Тип лицензии", "license_type")]},
    46: {"name": "🔌 Кабели / Коммутация", "order": 150, "fields": [
        t("Разъёмы", "connectors"), n("Длина, м", "length_m")]},
    44: {"name": "🧰 Аксессуары / Расходники", "order": 160, "fields": [
        sel("Тип", "accessory_type", "стойка", "кейс-чехол", "струны", "трости",
            "пластики", "крепёж", "другое")]},
    99: {"name": "❓ Другое", "order": 999},

    # ── Услуги ──
    200: {"name": "🎼 Музыканты", "order": 10, "fields": [
        t("Жанры", "genres"), chk("Свой инструмент", "own_instrument"),
        sel("Формат", "work_format", "живые выступления", "студийная запись", "всё")]},
    201: {"name": "Клавишники", "order": 10},
    208: {"name": "Гитаристы", "order": 20},
    209: {"name": "Басисты", "order": 30},
    210: {"name": "Барабанщики / перкуссионисты", "order": 40},
    202: {"name": "Смычковые", "order": 50},
    203: {"name": "Духовые", "order": 60},

    220: {"name": "🎤 Вокалисты", "order": 20, "fields": [
        t("Жанры", "genres"), chk("Бэк-вокал", "backing_vocals")]},
    18: {"name": "Мужской вокал", "order": 10},
    19: {"name": "Женский вокал", "order": 20},

    230: {"name": "👥 Коллективы / Группы", "order": 40, "fields": [
        t("Жанр", "genre"), n("Человек в составе", "members_count"),
        chk("Своя аппаратура", "own_gear"), n("Часов программы", "program_hours")]},
    260: {"name": "🎓 Преподаватели", "order": 50, "fields": [
        sel("Формат", "lesson_format", "онлайн", "у преподавателя", "выезд"),
        sel("Ученики", "students", "дети", "взрослые", "все"),
        chk("Пробное занятие бесплатно", "free_trial")]},
    261: {"name": "Вокал", "order": 10},
    262: {"name": "Инструменты", "order": 20},

    240: {"name": "🏢 Студии / Площадки", "order": 60},
    28: {"order": 10, "fields": [chk("Инструменты/бэклайн на месте", "backline_onsite")]},
    29: {"order": 20, "fields": [chk("Бэклайн на месте", "backline_onsite")]},

    290: {"name": "🎚 Продакшн", "order": 70},
    291: {"order": 10, "fields": [t("DAW / инструментарий", "daw_tools")]},
    250: {"name": "Композиторы / поэты / битмейкеры", "parent": 290, "order": 30},

    300: {"name": "🎙 Ведущие / MC", "order": 100},
    310: {"name": "🎪 Организация мероприятий", "order": 110},
    320: {"name": "📸 Медиа", "order": 120, "fields": [t("Тип услуг", "media_services")]},

    # ── Вакансии ──
    324: {"name": "🎤 Вокалисты", "order": 10, "fields": [
        sel("Пол", "gender", "мужской", "женский", "не важно"), t("Диапазон", "diapazone")]},
    323: {"name": "✍️ Композиторы / Аранжировщики", "order": 110},
}

# ── новые категории: (slug, name, parent_ref, order, fields) ──
# parent_ref: число (существующий id) или строка-slug новой категории.
NEW = [
    # Барахолка
    ("classic_guitars", "Классические гитары", 102, 30, [
        sel("Размер", "size", "4/4", "3/4", "1/2"), chk("Массив верхней деки", "solid_top")]),
    ("ukulele_folk", "Укулеле / народные струнные", 102, 50, [t("Тип инструмента", "instrument_type")]),
    ("acoustic_pianos", "Акустические пианино / рояли", 101, 50, [chk("Настроено", "tuned")]),
    ("drums_acoustic", "Акустические ударные", 107, 10, [
        t("Конфигурация", "drum_config"), chk("Железо в комплекте", "hardware_included")]),
    ("drums_electronic", "Электронные ударные", 107, 20, [
        n("Пэдов", "pads_count"), chk("Меш-пэды", "mesh_pads")]),
    ("cymbals", "Тарелки / железо", 107, 30, [
        n("Диаметр, дюймы", "diameter_inch"),
        sel("Тип", "cymbal_type", "hi-hat", "crash", "ride", "china", "splash", "эффект")]),
    ("mic_wireless", "Радиосистемы / беспроводные", 33, 30, [
        t("Частотный диапазон", "freq_range"), n("Каналов", "channels")]),
    ("studio_monitors", "Студийные мониторы", 353, 20, [
        sel("Активные/пассивные", "active_passive", "активные", "пассивные"),
        n("Динамик, дюймы", "speaker_inch"), sel("Цена за", "price_per", "пару", "штуку")]),
    ("preamp_processing", "Предусилители / обработка", 353, 30, [
        sel("Тип прибора", "device_type", "предусилитель", "компрессор", "эквалайзер",
            "канальная линейка", "другое")]),
    ("recorders", "Рекордеры / портастудии", 353, 40, [n("Дорожек", "tracks")]),
    ("concert_pa", "🔊 Концертный звук (PA)", 30, 90, []),
    ("pa_mixers", "Микшерные пульты", "concert_pa", 20, [
        n("Каналов", "channels_count"), sel("Цифровой/аналоговый", "digital_analog",
            "цифровой", "аналоговый"), chk("Встроенные эффекты", "builtin_fx")]),
    ("pa_power_amps", "Усилители мощности", "concert_pa", 30, [
        n("Мощность на канал, Вт", "power_per_channel"), n("Каналов", "amp_channels")]),
    ("pa_stage_monitors", "Сценические мониторы", "concert_pa", 40, [
        sel("Активный/пассивный", "active_passive", "активный", "пассивный"),
        n("Мощность, Вт", "power_watt")]),
    ("pa_light", "Световое оборудование", "concert_pa", 50, [
        sel("Тип", "light_type", "вращ. головы", "пар", "лазер", "строб",
            "дым-машина", "управление", "другое")]),
    ("pedals_fx", "Педали эффектов", 36, 10, [
        sel("Тип эффекта", "fx_type", "overdrive", "distortion", "fuzz", "delay", "reverb",
            "chorus", "phaser", "flanger", "compressor", "wah", "EQ", "booster",
            "looper", "tuner", "другое"),
        chk("True bypass", "true_bypass")]),
    # Услуги
    ("svc_accordion", "Аккордеонисты", 200, 70, []),
    ("svc_dj", "🎧 Диджеи", 80, 30, [
        t("Жанры", "genres"), chk("Своя аппаратура", "own_gear"), chk("Свет в комплекте", "own_light")]),
    ("svc_theory", "Теория / сольфеджио", 260, 30, []),
    ("svc_venues", "Концертные площадки / клубы", 240, 30, [
        n("Вместимость", "capacity"), chk("Свой звук", "own_sound"), chk("Свой свет", "own_light")]),
    ("svc_live_sound", "Звукорежиссёр на мероприятие", 290, 20, [chk("Своя аппаратура", "own_gear")]),
    ("svc_repair", "🔧 Ремонт / Настройка инструментов", 80, 80, [
        t("Специализация", "specialization"), chk("Выезд на дом", "home_visit")]),
    ("svc_rental", "📦 Аренда оборудования / Бэклайн", 80, 90, [
        chk("Доставка", "delivery"), chk("Залог", "deposit")]),
    ("svc_other", "❓ Другое", 80, 999, []),
    # Вакансии
    ("vac_guitar", "🎸 Гитаристы", 90, 20, []),
    ("vac_bass", "🪕 Басисты", 90, 30, []),
    ("vac_keys", "🎹 Клавишники", 90, 40, []),
    ("vac_drums", "🥁 Барабанщики / Перкуссионисты", 90, 50, []),
    ("vac_strings", "🎻 Смычковые", 90, 60, []),
    ("vac_winds", "🎷 Духовые", 90, 70, []),
    ("vac_accordion", "🪗 Аккордеонисты", 90, 80, []),
    ("vac_dj", "🎧 Диджеи", 90, 90, []),
    ("vac_sound", "🎚 Звукорежиссёры / Техперсонал", 90, 100, []),
    ("vac_teachers", "🎓 Преподаватели", 90, 120, []),
    ("vac_other", "❓ Другое", 90, 999, []),
]

# ── слияния: old_id → target_ref (объявления и extra-ссылки переносятся, категория удаляется) ──
MERGES = [
    (332, 103), (333, 105), (334, 103),          # бас: электро/контрабас/акустик → Бас-гитары и Смычковые
    (351, 101),                                   # органы → Клавишные
    (339, "pedals_fx"), (340, "pedals_fx"), (341, "pedals_fx"),
    (342, "pedals_fx"), (343, "pedals_fx"), (344, "pedals_fx"),
    (347, 346),                                   # блоки питания → Педалборды/питание
    (338, 335),                                   # комбо → Гитарные усилители
    (3, 202), (4, 202), (5, 202),                 # скрипка/альт/виолончель → Смычковые (услуги)
    (7, 208), (8, 208),                           # гитары (услуги) → Гитаристы
    (10, 209), (11, 209), (12, 209),              # бас (услуги) → Басисты
    (14, 210), (15, 210), (16, 210),              # барабаны/кахон/перкуссия → Барабанщики
    (325, 324), (326, 324),                       # вакансии вокал М/Ж → Вокалисты (пол — поле)
]

DISSOLVE_EMPTY = [31]  # зонтик «Музыкальные инструменты»: дети уже перевешены, объявлений нет


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    existing_slugs = {r[0] for r in cur.execute("SELECT slug FROM category")}

    def unique_slug(s):
        base, i = s, 2
        while s in existing_slugs:
            s = f"{base}_{i}"; i += 1
        existing_slugs.add(s)
        return s

    def merge_fields(cat_id, new_fields):
        """Новые поля + legacy-ключи, которых нет в новом наборе (сохраняем данные)."""
        old_raw = cur.execute("SELECT fields FROM category WHERE id=?", (cat_id,)).fetchone()
        try:
            old = json.loads(old_raw[0]) if old_raw and old_raw[0] else []
        except Exception:
            old = []
        new_keys = {f.get("key") for f in new_fields}
        merged = list(new_fields) + [f for f in old if f.get("key") not in new_keys]
        return json.dumps(merged, ensure_ascii=False)

    # 1) обновления существующих
    for cid, up in UPDATES.items():
        sets, vals = [], []
        if "name" in up:
            sets.append("name=?"); vals.append(up["name"])
        if "parent" in up:
            sets.append("parent_id=?"); vals.append(up["parent"])
        if "order" in up:
            sets.append("order_num=?"); vals.append(up["order"])
        if "fields" in up:
            sets.append("fields=?"); vals.append(merge_fields(cid, up["fields"]))
        if sets:
            vals.append(cid)
            cur.execute(f"UPDATE category SET {', '.join(sets)} WHERE id=?", vals)
    print(f"1. Обновлено категорий: {len(UPDATES)}")

    # 2) новые категории (двумя проходами: сначала с числовыми родителями)
    new_ids = {}
    pending = list(NEW)
    while pending:
        rest = []
        for slug, name, parent_ref, order, fields in pending:
            parent_id = parent_ref if isinstance(parent_ref, int) else new_ids.get(parent_ref)
            if parent_id is None:
                rest.append((slug, name, parent_ref, order, fields)); continue
            cur.execute(
                "INSERT INTO category (slug, name, parent_id, fields, order_num) VALUES (?,?,?,?,?)",
                (unique_slug(slug), name, parent_id,
                 json.dumps(fields, ensure_ascii=False) if fields else None, order),
            )
            new_ids[slug] = cur.lastrowid
        if len(rest) == len(pending):
            raise RuntimeError(f"Не удалось разрешить родителей: {[p[0] for p in rest]}")
        pending = rest
    print(f"2. Создано категорий: {len(new_ids)}")

    # 2б) обновления, зависящие от новых категорий:
    # 35 «Акустические системы» переезжает под новый «Концертный звук (PA)»
    cur.execute(
        "UPDATE category SET name=?, parent_id=?, order_num=?, fields=? WHERE id=35",
        ("Акустические системы / сабвуферы", new_ids["concert_pa"], 10,
         merge_fields(35, [
             sel("Активная/пассивная", "active_passive", "активная", "пассивная"),
             n("Мощность, Вт", "power_watt"),
             n("НЧ-динамик, дюймы", "lf_speaker_inch"),
         ])),
    )
    print("2б. Категория 35 перенесена под Концертный звук (PA)")

    # 3) слияния
    moved = 0
    for old_id, target_ref in MERGES:
        target = target_ref if isinstance(target_ref, int) else new_ids[target_ref]
        moved += cur.execute("UPDATE listing SET category_id=? WHERE category_id=?", (target, old_id)).rowcount
        cur.execute("UPDATE listing SET extra_category_id1=? WHERE extra_category_id1=?", (target, old_id))
        cur.execute("UPDATE listing SET extra_category_id2=? WHERE extra_category_id2=?", (target, old_id))
        # детей (если вдруг есть) перевешиваем на цель
        cur.execute("UPDATE category SET parent_id=? WHERE parent_id=?", (target, old_id))
        cur.execute("DELETE FROM category WHERE id=?", (old_id,))
    print(f"3. Слияний: {len(MERGES)}, перенесено объявлений: {moved}")

    # 4) роспуск пустых зонтиков
    for cid in DISSOLVE_EMPTY:
        cnt = cur.execute("SELECT COUNT(*) FROM listing WHERE category_id=?", (cid,)).fetchone()[0]
        kids = cur.execute("SELECT COUNT(*) FROM category WHERE parent_id=?", (cid,)).fetchone()[0]
        if cnt or kids:
            raise RuntimeError(f"Категория {cid} не пуста (объявл.: {cnt}, детей: {kids}) — стоп")
        cur.execute("DELETE FROM category WHERE id=?", (cid,))
    print(f"4. Распущено зонтиков: {len(DISSOLVE_EMPTY)}")

    # 5) проверки целостности
    orphans = cur.execute("""SELECT COUNT(*) FROM listing l
        LEFT JOIN category c ON c.id=l.category_id WHERE c.id IS NULL""").fetchone()[0]
    bad_extra = cur.execute("""SELECT COUNT(*) FROM listing l
        LEFT JOIN category c1 ON c1.id=l.extra_category_id1
        LEFT JOIN category c2 ON c2.id=l.extra_category_id2
        WHERE (l.extra_category_id1 IS NOT NULL AND c1.id IS NULL)
           OR (l.extra_category_id2 IS NOT NULL AND c2.id IS NULL)""").fetchone()[0]
    bad_parent = cur.execute("""SELECT COUNT(*) FROM category ch
        LEFT JOIN category p ON p.id=ch.parent_id
        WHERE ch.parent_id IS NOT NULL AND p.id IS NULL""").fetchone()[0]
    bad_json = 0
    for r in cur.execute("SELECT id, fields FROM category WHERE fields IS NOT NULL"):
        try:
            json.loads(r[1])
        except Exception:
            bad_json += 1
    print(f"5. Проверки: объявлений-сирот {orphans}, битых extra {bad_extra}, "
          f"битых parent {bad_parent}, битых JSON {bad_json}")
    if orphans or bad_extra or bad_parent or bad_json:
        conn.rollback()
        raise RuntimeError("Проверки не пройдены — откат, БД не изменена")

    conn.commit()

    # 6) итоговое дерево
    rows = cur.execute("SELECT id, name, parent_id, order_num FROM category").fetchall()
    counts = {r[0]: r[1] for r in cur.execute(
        "SELECT category_id, COUNT(*) FROM listing GROUP BY category_id")}
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    for pid in by_parent:
        by_parent[pid].sort(key=lambda r: (r["order_num"] or 0, r["name"]))

    def tree(pid, ind=0):
        for r in by_parent.get(pid, []):
            c = counts.get(r["id"], 0)
            print("  " * ind + f'{r["id"]}: {r["name"]}' + (f"  [{c} объявл.]" if c else ""))
            tree(r["id"], ind + 1)

    for root, title in ((30, "БАРАХОЛКА"), (80, "УСЛУГИ"), (90, "ВАКАНСИИ")):
        print(f"\n===== {title} =====")
        tree(root)
    conn.close()
    print("\nМиграция завершена успешно.")


if __name__ == "__main__":
    main()
