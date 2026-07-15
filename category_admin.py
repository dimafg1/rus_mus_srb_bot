"""
Локальный админ для управления категориями.
Запуск: python category_admin.py   (или двойной клик на category_admin.command)
Открыть: http://localhost:8001
"""
import sqlite3, json, datetime, os, re
from pathlib import Path
from collections import defaultdict
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional, List, Any
import httpx, uvicorn

# Load bot token for Telegram media proxy
_env_path = Path(__file__).parent / ".env"
BOT_TOKEN = ""
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        if line.startswith("BOT_TOKEN="):
            BOT_TOKEN = line.split("=", 1)[1].strip()

DB_PATH = Path(__file__).parent / "dev.db"
ROOT_IDS = {"market": 30, "services": 80, "vacancy": 90}
ROOT_NAMES = {"market": "Барахолка", "services": "Услуги", "vacancy": "Вакансии"}

# Логи в файл с ротацией: logs/admin.log, 5 МБ x 5 файлов
import logging
from logging.handlers import RotatingFileHandler
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)
_fh = RotatingFileHandler(_log_dir / "admin.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_fh, logging.StreamHandler()])

app = FastAPI()

# ── Доступ только с этой машины и из Tailscale-сети ──────────────────────────
# Приложение слушает 0.0.0.0 (для удалённого доступа через Tailscale),
# но админка без авторизации не должна быть видна из офисного Wi-Fi.
# Tailscale выдаёт адреса из CGNAT-диапазона 100.64.0.0/10.
import ipaddress
_ALLOWED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),      # localhost
    ipaddress.ip_network("::1/128"),           # localhost IPv6
    ipaddress.ip_network("100.64.0.0/10"),     # Tailscale
]


@app.middleware("http")
async def _ip_allowlist(request: Request, call_next):
    client_ip = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(client_ip)
        if not any(ip in net for net in _ALLOWED_NETS):
            logging.warning("Отклонён запрос с недоверенного IP: %s %s", client_ip, request.url.path)
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("Forbidden", status_code=403)
    except ValueError:
        pass  # нераспознанный адрес (unix socket и т.п.) — пропускаем
    return await call_next(request)


# ─────────────────────────── DB helpers ───────────────────────────

def db():
    # timeout + busy_timeout: бот и админка работают с базой параллельно,
    # без них конкурентная запись даёт "database is locked".
    # foreign_keys НЕ включаем: в DDL listing.category_id без ON DELETE,
    # включение сломает удаление категорий с объявлениями.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    return conn


def migrate():
    with db() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(category)").fetchall()]
        if "order_num" not in cols:
            conn.execute("ALTER TABLE category ADD COLUMN order_num INTEGER DEFAULT 0")
            rows = conn.execute("SELECT id, parent_id FROM category ORDER BY id").fetchall()
            by_parent = defaultdict(list)
            for r in rows:
                by_parent[r["parent_id"]].append(r["id"])
            for ids in by_parent.values():
                for i, cid in enumerate(ids):
                    conn.execute("UPDATE category SET order_num=? WHERE id=?", (i * 10, cid))
            conn.commit()


migrate()


# ─────────────────────────── API ───────────────────────────

@app.get("/api/tree/{section}")
def get_tree(section: str):
    if section not in ROOT_IDS:
        raise HTTPException(404)
    root_id = ROOT_IDS[section]
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, slug, parent_id, order_num, fields FROM category"
        ).fetchall()
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT category_id, COUNT(*) FROM listing WHERE is_sold=0 OR is_sold IS NULL GROUP BY category_id"
        ).fetchall()}
    all_cats = [dict(r) for r in rows]

    def build(pid):
        ch = [c for c in all_cats if c["parent_id"] == pid]
        ch.sort(key=lambda x: (x.get("order_num") or 0, x["name"] or ""))
        return [{
            "id": c["id"], "name": c["name"], "slug": c["slug"],
            "order_num": c.get("order_num") or 0,
            "count": counts.get(c["id"], 0),
            "has_fields": bool(c.get("fields") and c["fields"] not in ("[]", "null", "")),
            "children": build(c["id"]),
        } for c in ch]

    return build(root_id)


@app.get("/api/all_categories")
def all_categories():
    with db() as conn:
        rows = conn.execute("SELECT id, name, slug, parent_id, order_num FROM category ORDER BY order_num, name").fetchall()
    return [dict(r) for r in rows]


class CatCreate(BaseModel):
    name: str
    slug: str
    parent_id: int

class CatUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    parent_id: Optional[int] = None

class MoveDir(BaseModel):
    direction: str  # "up" | "down"

class FieldsDef(BaseModel):
    fields: List[Any]

class ReorderSiblings(BaseModel):
    parent_id: int
    ids: List[int]

class BatchField(BaseModel):
    ids: List[int]
    field: Any


@app.post("/api/categories")
def create_category(body: CatCreate):
    with db() as conn:
        if conn.execute("SELECT id FROM category WHERE slug=?", (body.slug,)).fetchone():
            raise HTTPException(400, f"Slug «{body.slug}» уже занят")
        max_order = conn.execute(
            "SELECT COALESCE(MAX(order_num),0) FROM category WHERE parent_id=?", (body.parent_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO category (name, slug, parent_id, order_num) VALUES (?,?,?,?)",
            (body.name.strip(), body.slug.strip(), body.parent_id, max_order + 10),
        )
        conn.commit()
        return {"id": cur.lastrowid}


@app.patch("/api/categories/{cat_id}")
def update_category(cat_id: int, body: CatUpdate):
    with db() as conn:
        if not conn.execute("SELECT id FROM category WHERE id=?", (cat_id,)).fetchone():
            raise HTTPException(404)
        if body.slug:
            if conn.execute("SELECT id FROM category WHERE slug=? AND id!=?", (body.slug, cat_id)).fetchone():
                raise HTTPException(400, f"Slug «{body.slug}» уже занят")
        fields, vals = [], []
        if body.name is not None:
            fields.append("name=?"); vals.append(body.name.strip())
        if body.slug is not None:
            fields.append("slug=?"); vals.append(body.slug.strip())
        if body.parent_id is not None:
            fields.append("parent_id=?"); vals.append(body.parent_id)
        if fields:
            vals.append(cat_id)
            conn.execute(f"UPDATE category SET {', '.join(fields)} WHERE id=?", vals)
            conn.commit()
    return {"ok": True}


@app.post("/api/categories/{cat_id}/move")
def move_category(cat_id: int, body: MoveDir):
    with db() as conn:
        row = conn.execute("SELECT id, parent_id, order_num FROM category WHERE id=?", (cat_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        siblings = conn.execute(
            "SELECT id, order_num FROM category WHERE parent_id=? ORDER BY order_num, id",
            (row["parent_id"],)
        ).fetchall()
        ids = [s["id"] for s in siblings]
        orders = [s["order_num"] or 0 for s in siblings]
        idx = ids.index(cat_id)
        if body.direction == "up" and idx > 0:
            swap = idx - 1
        elif body.direction == "down" and idx < len(ids) - 1:
            swap = idx + 1
        else:
            return {"ok": True}
        conn.execute("UPDATE category SET order_num=? WHERE id=?", (orders[swap], ids[idx]))
        conn.execute("UPDATE category SET order_num=? WHERE id=?", (orders[idx], ids[swap]))
        conn.commit()
    return {"ok": True}


@app.delete("/api/categories/{cat_id}")
def delete_category(cat_id: int):
    with db() as conn:
        if conn.execute("SELECT COUNT(*) FROM category WHERE parent_id=?", (cat_id,)).fetchone()[0]:
            raise HTTPException(400, "Сначала удалите подкатегории")
        listings = conn.execute(
            "SELECT COUNT(*) FROM listing WHERE category_id=? AND (is_sold=0 OR is_sold IS NULL)", (cat_id,)
        ).fetchone()[0]
        if listings:
            raise HTTPException(400, f"В категории {listings} активных объявлений")
        conn.execute("DELETE FROM category WHERE id=?", (cat_id,))
        conn.commit()
    return {"ok": True}


@app.get("/api/categories/{cat_id}/fields")
def get_fields(cat_id: int):
    with db() as conn:
        row = conn.execute("SELECT fields FROM category WHERE id=?", (cat_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    try:
        return json.loads(row["fields"] or "[]") or []
    except Exception:
        return []


@app.put("/api/categories/{cat_id}/fields")
def save_fields(cat_id: int, body: FieldsDef):
    with db() as conn:
        if not conn.execute("SELECT id FROM category WHERE id=?", (cat_id,)).fetchone():
            raise HTTPException(404)
        conn.execute("UPDATE category SET fields=? WHERE id=?",
                     (json.dumps(body.fields, ensure_ascii=False), cat_id))
        conn.commit()
    return {"ok": True}


@app.post("/api/categories/reorder_siblings")
def reorder_siblings_ep(body: ReorderSiblings):
    with db() as conn:
        for i, cid in enumerate(body.ids):
            conn.execute(
                "UPDATE category SET order_num=? WHERE id=? AND parent_id=?",
                (i * 10, cid, body.parent_id),
            )
        conn.commit()
    return {"ok": True}


@app.post("/api/categories/batch/add_field")
def batch_add_field(body: BatchField):
    with db() as conn:
        for cid in body.ids:
            row = conn.execute("SELECT fields FROM category WHERE id=?", (cid,)).fetchone()
            if not row:
                continue
            try:
                fields = json.loads(row["fields"] or "[]") or []
            except Exception:
                fields = []
            # avoid duplicate keys
            if not any(f.get("key") == body.field.get("key") for f in fields):
                fields.append(body.field)
            conn.execute("UPDATE category SET fields=? WHERE id=?",
                         (json.dumps(fields, ensure_ascii=False), cid))
        conn.commit()
    return {"ok": True}


# ─────────────────────────── Analytics API ───────────────────────────

def _fill_days(rows_dict: dict, days: int = 30) -> list:
    today = datetime.date.today()
    result = []
    for i in range(days):
        d = (today - datetime.timedelta(days=days - 1 - i)).isoformat()
        result.append({"date": d[5:], "count": rows_dict.get(d, 0)})
    return result


@app.get("/api/analytics/overview")
def analytics_overview():
    with db() as conn:
        def q(sql, default=0):
            try:
                return conn.execute(sql).fetchone()[0] or default
            except Exception:
                return default
        return {
            "users": {
                "total":    q("SELECT COUNT(*) FROM BotUser"),
                "dau":      q("SELECT COUNT(*) FROM BotUser WHERE last_seen >= datetime('now','start of day')"),
                "wau":      q("SELECT COUNT(*) FROM BotUser WHERE last_seen >= datetime('now','-7 days')"),
                "mau":      q("SELECT COUNT(*) FROM BotUser WHERE last_seen >= datetime('now','-30 days')"),
                "new_week": q("SELECT COUNT(*) FROM BotUser WHERE first_seen >= datetime('now','-7 days')"),
                "new_prev": q("SELECT COUNT(*) FROM BotUser WHERE first_seen >= datetime('now','-14 days') AND first_seen < datetime('now','-7 days')"),
            },
            "listings": {
                "total":    q("SELECT COUNT(*) FROM listing"),
                "active":   q("SELECT COUNT(*) FROM listing WHERE is_sold=0 OR is_sold IS NULL"),
                "new_week": q("SELECT COUNT(*) FROM listing WHERE created_at >= datetime('now','-7 days')"),
                "new_prev": q("SELECT COUNT(*) FROM listing WHERE created_at >= datetime('now','-14 days') AND created_at < datetime('now','-7 days')"),
            },
            "search": {
                "total":     q("SELECT COUNT(*) FROM search_log"),
                "no_result": q("SELECT COUNT(*) FROM search_log WHERE results_count=0"),
            },
            "views": {
                "opens":    q("SELECT COUNT(*) FROM listing_views WHERE action='open'"),
                "contacts": q("SELECT COUNT(*) FROM listing_views WHERE action='contact'"),
            },
        }


@app.get("/api/analytics/daily")
def analytics_daily():
    with db() as conn:
        def safe(sql):
            try:
                return {r[0]: r[1] for r in conn.execute(sql).fetchall()}
            except Exception:
                return {}
        users = safe(
            "SELECT date(first_seen),COUNT(*) FROM BotUser"
            " WHERE first_seen>=datetime('now','-30 days') GROUP BY date(first_seen)"
        )
        listings = safe(
            "SELECT date(created_at),COUNT(*) FROM listing"
            " WHERE created_at>=datetime('now','-30 days') GROUP BY date(created_at)"
        )
    return {"users": _fill_days(users), "listings": _fill_days(listings)}


@app.get("/api/analytics/top_categories")
def analytics_top_categories():
    with db() as conn:
        try:
            rows = conn.execute("""
                SELECT c.name,
                    COUNT(CASE WHEN lv.action='open'    THEN 1 END) AS opens,
                    COUNT(CASE WHEN lv.action='contact' THEN 1 END) AS contacts,
                    COUNT(DISTINCT l.id) AS listings
                FROM category c
                LEFT JOIN listing l        ON l.category_id=c.id AND (l.is_sold=0 OR l.is_sold IS NULL)
                LEFT JOIN listing_views lv ON lv.listing_id=l.id
                WHERE c.parent_id IS NOT NULL
                GROUP BY c.id, c.name
                HAVING listings>0 OR opens>0
                ORDER BY opens DESC, listings DESC
                LIMIT 20
            """).fetchall()
        except Exception:
            rows = []
    return [{"name": r[0], "opens": r[1] or 0, "contacts": r[2] or 0, "listings": r[3] or 0} for r in rows]


@app.get("/api/analytics/top_searches")
def analytics_top_searches():
    with db() as conn:
        try:
            rows = conn.execute("""
                SELECT query_raw, COUNT(*) AS cnt,
                       SUM(CASE WHEN results_count>0 THEN 1 ELSE 0 END) AS with_res
                FROM search_log
                WHERE query_raw IS NOT NULL AND trim(query_raw)!=''
                GROUP BY lower(trim(query_raw))
                ORDER BY cnt DESC
                LIMIT 30
            """).fetchall()
        except Exception:
            rows = []
    return [{"query": r[0], "count": r[1], "with_results": r[2] or 0} for r in rows]


@app.get("/api/analytics/growth")
def analytics_growth():
    with db() as conn:
        def safe(sql, params=()):
            try:
                return conn.execute(sql, params).fetchall()
            except Exception:
                return []
        today = datetime.date.today()
        days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
        user_rows = safe("SELECT date(first_seen), COUNT(*) FROM BotUser WHERE first_seen>=datetime('now','-6 days') GROUP BY date(first_seen)")
        listing_rows = safe("SELECT date(created_at), COUNT(*) FROM listing WHERE created_at>=datetime('now','-6 days') GROUP BY date(created_at)")
        user_map = {r[0]: r[1] for r in user_rows}
        listing_map = {r[0]: r[1] for r in listing_rows}
        def scl(sql):
            try: return conn.execute(sql).fetchone()[0] or 0
            except: return 0
        return {
            "days": days,
            "users_by_day": [user_map.get(d, 0) for d in days],
            "listings_by_day": [listing_map.get(d, 0) for d in days],
            "new_users_week": scl("SELECT COUNT(*) FROM BotUser WHERE first_seen>=datetime('now','-7 days')"),
            "new_users_prev": scl("SELECT COUNT(*) FROM BotUser WHERE first_seen>=datetime('now','-14 days') AND first_seen<datetime('now','-7 days')"),
            "new_listings_week": scl("SELECT COUNT(*) FROM listing WHERE created_at>=datetime('now','-7 days')"),
            "new_listings_prev": scl("SELECT COUNT(*) FROM listing WHERE created_at>=datetime('now','-14 days') AND created_at<datetime('now','-7 days')"),
        }


@app.get("/api/analytics/sections")
def analytics_sections():
    with db() as conn:
        def safe(sql):
            try: return [dict(r) for r in conn.execute(sql).fetchall()]
            except: return []
        search_rows = safe("""
            SELECT section, COUNT(*) AS searches,
                   SUM(CASE WHEN results_count=0 THEN 1 ELSE 0 END) AS no_results,
                   COUNT(DISTINCT user_id) AS search_users
            FROM search_log GROUP BY section""")
        open_rows = safe("""
            SELECT section, COUNT(*) AS opens,
                   COUNT(DISTINCT user_id) AS open_users,
                   SUM(CASE WHEN source='search' THEN 1 ELSE 0 END) AS search_opens,
                   SUM(CASE WHEN source='catalog' THEN 1 ELSE 0 END) AS catalog_opens
            FROM listing_views WHERE action='open' GROUP BY section""")
    data = {}
    for r in search_rows:
        s = r.get("section") or ""
        data.setdefault(s, {}).update(searches=r["searches"], no_results=r["no_results"], search_users=r["search_users"])
    for r in open_rows:
        s = r.get("section") or ""
        data.setdefault(s, {}).update(opens=r["opens"], open_users=r["open_users"], search_opens=r["search_opens"], catalog_opens=r["catalog_opens"])
    result = []
    for sec, v in sorted(data.items(), key=lambda x: (x[1].get("opens",0)+x[1].get("searches",0)), reverse=True):
        result.append({"section": sec, **{k: v.get(k, 0) for k in ("searches","no_results","search_users","opens","open_users","search_opens","catalog_opens")}})
    return result


@app.get("/api/analytics/no_results")
def analytics_no_results():
    with db() as conn:
        try:
            rows = conn.execute("""
                SELECT COALESCE(NULLIF(query_effective,''),NULLIF(query_normalized,''),query_raw) AS q,
                       section, COUNT(*) AS cnt
                FROM search_log WHERE results_count=0
                GROUP BY q, section ORDER BY cnt DESC, q ASC LIMIT 30
            """).fetchall()
        except Exception:
            rows = []
    return [{"query": r[0], "section": r[1], "count": r[2]} for r in rows]


@app.get("/api/analytics/search_quality")
def analytics_search_quality():
    with db() as conn:
        def scl(sql):
            try: return conn.execute(sql).fetchone()[0] or 0
            except: return 0
        total = scl("SELECT COUNT(*) FROM search_log")
        no_results = scl("SELECT COUNT(*) FROM search_log WHERE results_count=0")
        try:
            mode_rows = conn.execute("""
                SELECT match_mode, COUNT(*) AS cnt FROM search_log
                GROUP BY match_mode ORDER BY cnt DESC, match_mode ASC
            """).fetchall()
        except Exception:
            mode_rows = []
    return {"total": total, "no_results": no_results,
            "modes": [{"mode": r[0] or "unknown", "count": r[1]} for r in mode_rows]}


@app.get("/api/analytics/top_cards")
def analytics_top_cards():
    with db() as conn:
        try:
            rows = conn.execute("""
                SELECT lv.listing_id, lv.section,
                       COUNT(*) AS opens, COUNT(DISTINCT lv.user_id) AS users,
                       SUM(CASE WHEN lv.source='search' THEN 1 ELSE 0 END) AS search_opens,
                       SUM(CASE WHEN lv.source='catalog' THEN 1 ELSE 0 END) AS catalog_opens,
                       COALESCE(l.title,'Без названия') AS title,
                       l.owner_id, l.contact, l.price, l.photo_file_id,
                       l.is_sold
                FROM listing_views lv
                LEFT JOIN listing l ON l.id=lv.listing_id
                WHERE lv.action='open'
                GROUP BY lv.listing_id ORDER BY opens DESC, users DESC LIMIT 20
            """).fetchall()
        except Exception:
            rows = []
    return [{"id": r[0], "section": r[1], "opens": r[2], "users": r[3],
             "search_opens": r[4] or 0, "catalog_opens": r[5] or 0,
             "title": r[6], "owner_id": r[7], "contact": r[8] or "",
             "price": r[9] or "", "photo_file_id": r[10] or "",
             "is_sold": bool(r[11])} for r in rows]


@app.get("/api/analytics/sources")
def analytics_sources():
    with db() as conn:
        try:
            rows = conn.execute("""
                SELECT section, source, COUNT(*) AS opens, COUNT(DISTINCT user_id) AS users
                FROM listing_views WHERE action='open'
                GROUP BY section, source ORDER BY opens DESC, section ASC
            """).fetchall()
        except Exception:
            rows = []
    return [{"section": r[0], "source": r[1], "opens": r[2], "users": r[3]} for r in rows]


@app.get("/api/analytics/search_conversion")
def analytics_search_conversion():
    with db() as conn:
        try:
            s_rows = conn.execute("SELECT section, COUNT(*) FROM search_log GROUP BY section").fetchall()
            o_rows = conn.execute("SELECT section, COUNT(*) FROM listing_views WHERE action='open' AND source='search' GROUP BY section").fetchall()
        except Exception:
            s_rows, o_rows = [], []
    searches = {r[0] or "": r[1] for r in s_rows}
    opens = {r[0] or "": r[1] for r in o_rows}
    sections = sorted(set(searches) | set(opens))
    return [{"section": s, "searches": searches.get(s, 0), "opens": opens.get(s, 0)} for s in sections]


@app.get("/api/analytics/cities")
def analytics_cities():
    with db() as conn:
        rows = conn.execute("""
            SELECT c.id, c.name,
                   COUNT(DISTINCT l.id) AS total,
                   COUNT(DISTINCT CASE WHEN COALESCE(l.is_sold,0)=0 THEN l.id END) AS active,
                   SUM(CASE WHEN lv.action='open' THEN 1 ELSE 0 END) AS views,
                   COUNT(DISTINCT CASE WHEN lv.action='open' THEN lv.user_id END) AS viewers,
                   SUM(CASE WHEN lv.action='contact' THEN 1 ELSE 0 END) AS contacts
            FROM city c
            LEFT JOIN listing l ON l.city_id=c.id
            LEFT JOIN listing_views lv ON lv.listing_id=l.id
            GROUP BY c.id, c.name
            ORDER BY views DESC, total DESC
        """).fetchall()
        type_rows = conn.execute("""
            SELECT l.city_id, l.type, COUNT(*) as cnt
            FROM listing l WHERE l.type IS NOT NULL
            GROUP BY l.city_id, l.type
        """).fetchall()
    by_type = {}
    for r in type_rows:
        by_type.setdefault(r[0], {})[r[1]] = r[2]
    result = []
    for r in rows:
        views = r[4] or 0; contacts = r[6] or 0
        result.append({
            "id": r[0],
            "name": (r[1] or "").rstrip(".").strip(),
            "total": r[2] or 0, "active": r[3] or 0,
            "views": views, "viewers": r[5] or 0, "contacts": contacts,
            "conversion": round(contacts / views * 100, 1) if views else 0,
            "by_type": by_type.get(r[0], {}),
        })
    return result


@app.get("/api/analytics/owners")
def analytics_owners(offset: int = 0, limit: int = 15):
    with db() as conn:
        try:
            rows = conn.execute("""
                SELECT l.owner_id,
                       GROUP_CONCAT(DISTINCT l.contact) AS contacts_raw,
                       COUNT(DISTINCT l.id) AS listings_count,
                       (SELECT COUNT(*) FROM listing ll WHERE ll.owner_id=l.owner_id AND COALESCE(ll.is_sold,0)=0) AS active,
                       COUNT(lv.id) AS opens,
                       COUNT(DISTINCT lv.user_id) AS unique_viewers,
                       MAX(l.created_at) AS last_at
                FROM listing l
                LEFT JOIN listing_views lv ON lv.listing_id=l.id AND lv.action='open'
                GROUP BY l.owner_id
                ORDER BY opens DESC, listings_count DESC
                LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
            total = conn.execute("SELECT COUNT(DISTINCT owner_id) FROM listing").fetchone()[0] or 0
        except Exception:
            rows, total = [], 0
    return {"total": total, "offset": offset, "limit": limit,
            "rows": [{"owner_id": r[0], "contacts_raw": r[1] or "", "listings": r[2] or 0,
                      "active": r[3] or 0, "opens": r[4] or 0, "viewers": r[5] or 0,
                      "last_at": r[6] or ""} for r in rows]}


@app.get("/api/analytics/owner/{owner_id}")
def analytics_owner(owner_id: int, listing_offset: int = 0, listing_limit: int = 10):
    with db() as conn:
        try:
            owner = dict(conn.execute("""
                SELECT l.owner_id,
                       GROUP_CONCAT(DISTINCT l.contact) AS contacts_raw,
                       COUNT(DISTINCT l.id) AS listings_count,
                       SUM(CASE WHEN COALESCE(l.is_sold,0)=0 THEN 1 ELSE 0 END) AS active,
                       SUM(CASE WHEN COALESCE(l.is_sold,0)=1 THEN 1 ELSE 0 END) AS sold,
                       COUNT(lv.id) AS opens,
                       COUNT(DISTINCT lv.user_id) AS unique_viewers,
                       SUM(CASE WHEN lv.source='search' THEN 1 ELSE 0 END) AS search_opens,
                       SUM(CASE WHEN lv.source='catalog' THEN 1 ELSE 0 END) AS catalog_opens,
                       MAX(l.created_at) AS last_at
                FROM listing l
                LEFT JOIN listing_views lv ON lv.listing_id=l.id AND lv.action='open'
                WHERE l.owner_id=?
                GROUP BY l.owner_id
            """, (owner_id,)).fetchone() or {})
            listings = conn.execute("""
                SELECT l.id, l.title, l.price, l.is_sold, l.photo_file_id, l.contact,
                       l.created_at,
                       COUNT(lv.id) AS opens, COUNT(DISTINCT lv.user_id) AS viewers
                FROM listing l
                LEFT JOIN listing_views lv ON lv.listing_id=l.id AND lv.action='open'
                WHERE l.owner_id=?
                GROUP BY l.id ORDER BY opens DESC, l.id DESC
                LIMIT ? OFFSET ?
            """, (owner_id, listing_limit, listing_offset)).fetchall()
            total_listings = conn.execute("SELECT COUNT(*) FROM listing WHERE owner_id=?", (owner_id,)).fetchone()[0] or 0
        except Exception:
            owner, listings, total_listings = {}, [], 0
    if not owner:
        raise HTTPException(404, "Owner not found")
    return {"owner": owner, "total_listings": total_listings,
            "listing_offset": listing_offset,
            "listings": [{"id": r[0], "title": r[1] or "Без названия", "price": r[2] or "",
                          "is_sold": bool(r[3]), "photo_file_id": r[4] or "",
                          "contact": r[5] or "", "created_at": r[6] or "",
                          "opens": r[7] or 0, "viewers": r[8] or 0} for r in listings]}


def _parse_video(flex_str: str) -> dict:
    try:
        flex = json.loads(flex_str or "{}")
    except Exception:
        flex = {}
    raw = flex.get("video", "") or ""
    if not raw:
        return {"video_type": "", "video_id": ""}
    if raw.startswith("http"):
        # Extract YouTube video id
        yt_id = ""
        for part in raw.split("v=")[1:]:
            yt_id = part.split("&")[0].strip()
            break
        if not yt_id:
            for part in raw.split("youtu.be/")[1:]:
                yt_id = part.split("?")[0].strip()
                break
        return {"video_type": "youtube", "video_id": yt_id, "video_url": raw}
    return {"video_type": "telegram", "video_id": raw}


@app.get("/api/listing/{listing_id}")
def get_listing(listing_id: int):
    with db() as conn:
        try:
            row = conn.execute("""
                SELECT l.id, l.title, l.descr, l.price, l.contact, l.photo_file_id,
                       l.is_sold, l.created_at, l.type, l.status, l.owner_id,
                       ci.name AS city_name,
                       (SELECT GROUP_CONCAT(ct.name, ' / ')
                        FROM (
                          WITH RECURSIVE ctr(id,name,parent_id,depth) AS (
                            SELECT c0.id,c0.name,c0.parent_id,0 FROM category c0 WHERE c0.id=l.category_id
                            UNION ALL
                            SELECT cp.id,cp.name,cp.parent_id,ctr.depth+1 FROM category cp JOIN ctr ON ctr.parent_id=cp.id
                          )
                          SELECT name FROM ctr ORDER BY depth DESC
                        ) ct
                       ) AS category_path,
                       COUNT(lv.id) AS opens,
                       COUNT(DISTINCT lv.user_id) AS unique_viewers,
                       SUM(CASE WHEN lv.source='search' THEN 1 ELSE 0 END) AS search_opens,
                       SUM(CASE WHEN lv.source='catalog' THEN 1 ELSE 0 END) AS catalog_opens,
                       l.flex, l.category_id
                FROM listing l
                LEFT JOIN city ci ON ci.id=l.city_id
                LEFT JOIN listing_views lv ON lv.listing_id=l.id AND lv.action='open'
                WHERE l.id=?
                GROUP BY l.id
            """, (listing_id,)).fetchone()
        except Exception as e:
            raise HTTPException(500, str(e))
        if not row:
            raise HTTPException(404, "Listing not found")
        # Fetch flex field definitions — вся цепочка parent_id, дочерние перекрывают родительские
        fields_by_key = {}  # key -> field dict, дочерние пишутся последними
        cur_cat_id = row[18]
        chain = []
        guard = 0
        while cur_cat_id and guard < 10:
            guard += 1
            cat_row = conn.execute("SELECT id, parent_id, fields FROM category WHERE id=?", (cur_cat_id,)).fetchone()
            if not cat_row:
                break
            chain.append(cat_row)
            cur_cat_id = cat_row[1]
        for cat_row in reversed(chain):  # от корня к листу — дочерние перекрывают
            if not cat_row[2]:
                continue
            try:
                for f in json.loads(cat_row[2]):
                    if f.get("type") in ("__meta", "video"):
                        continue
                    k = f.get("key", "").strip()
                    if k:
                        fields_by_key[k] = {
                            "key": k,
                            "label": f.get("label", k),
                            "type": f.get("type", "text"),
                        }
            except Exception:
                pass
        flex_fields = list(fields_by_key.values())
        # Данные афиши: дата/время/площадка события лежат в events_meta
        event = None
        em = conn.execute(
            "SELECT start_date_local, start_time_local, venue_text, city_text, price_text, status "
            "FROM events_meta WHERE listing_id=?", (listing_id,)
        ).fetchone()
        if em:
            event = {
                "date": em[0] or "", "time": em[1] or "",
                "venue": em[2] or "", "city_text": em[3] or "",
                "price_text": em[4] or "", "status": em[5] or "",
            }
    photo_ids = [p.strip() for p in (row[5] or "").split(",") if p.strip()]
    video = _parse_video(row[17] or "")
    try:
        flex_data = json.loads(row[17]) if row[17] else {}
    except Exception:
        flex_data = {}
    return {
        "id": row[0], "title": row[1] or "", "descr": row[2] or "",
        "price": row[3] or "", "contact": row[4] or "",
        "photo_ids": photo_ids, **video,
        "is_sold": bool(row[6]), "created_at": row[7] or "",
        "type": row[8] or "", "status": row[9] or "",
        "owner_id": row[10], "city": row[11] or "",
        "category": row[12] or "",
        "opens": row[13] or 0, "viewers": row[14] or 0,
        "search_opens": row[15] or 0, "catalog_opens": row[16] or 0,
        "flex": flex_data,
        "flex_fields": flex_fields,
        "event": event,
    }


class ListingUpdate(BaseModel):
    title: Optional[str] = None
    descr: Optional[str] = None
    price: Optional[str] = None
    contact: Optional[str] = None
    flex: Optional[dict] = None
    event: Optional[dict] = None  # {date: 'ДД-ММ-ГГГГ', time: 'ЧЧ:ММ', venue, price_text}


# Гибкие парсеры даты/времени — те же правила, что в боте (events_add.py),
# чтобы админ мог вводить в любом привычном формате.
def _parse_event_date(raw: str):
    """07.10.25 / 07-10-25 / 07/10/2025 / 071025 / 07102025 / 2026-02-20 → datetime | None"""
    s = (raw or "").strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    try:
        if len(digits) == 6:  # DDMMYY
            return datetime.datetime(2000 + int(digits[4:6]), int(digits[2:4]), int(digits[0:2]))
        if len(digits) == 8:
            a, b, c = int(digits[0:2]), int(digits[2:4]), int(digits[4:8])
            if 1 <= a <= 31 and 1 <= b <= 12 and 2000 <= c <= 2100:  # DDMMYYYY
                return datetime.datetime(c, b, a)
            year, month, day = int(digits[0:4]), int(digits[4:6]), int(digits[6:8])
            if 2000 <= year <= 2100:  # YYYYMMDD
                return datetime.datetime(year, month, day)
    except ValueError:
        return None
    for fmt in ("%d.%m.%y", "%d-%m-%y", "%d/%m/%y",
                "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y",
                "%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _parse_event_time(raw: str):
    """HH:MM / HH.MM / HH-MM / 'HH MM' / HHMM / HMM / HH → (hh, mm) | None"""
    s = (raw or "").strip().lower()
    if not s:
        return None
    if re.fullmatch(r"\d{1,4}", s):
        if len(s) <= 2:
            hh, mm = int(s), 0
        else:
            hh, mm = int(s[:-2]), int(s[-2:])
        return (hh, mm) if 0 <= hh <= 23 and 0 <= mm <= 59 else None
    s_norm = re.sub(r"\s+", ":", s).replace(".", ":").replace("-", ":")
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s_norm)
    if not m:
        return None
    hh, mm = map(int, m.groups())
    return (hh, mm) if 0 <= hh <= 23 and 0 <= mm <= 59 else None


@app.patch("/api/listing/{listing_id}")
def update_listing(listing_id: int, body: ListingUpdate):
    with db() as conn:
        row = conn.execute("SELECT id, flex FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        fields, vals = [], []
        if body.title is not None:
            fields.append("title=?"); vals.append(body.title.strip() or None)
        if body.descr is not None:
            fields.append("descr=?"); vals.append(body.descr.strip() or None)
        if body.price is not None:
            fields.append("price=?"); vals.append(body.price.strip() or None)
        if body.contact is not None:
            fields.append("contact=?"); vals.append(body.contact.strip() or None)
        if body.flex is not None:
            try:
                existing_flex = json.loads(row[1]) if row[1] else {}
            except Exception:
                existing_flex = {}
            existing_flex.update(body.flex)
            fields.append("flex=?"); vals.append(json.dumps(existing_flex, ensure_ascii=False))

        updated = False
        if fields:
            vals.append(listing_id)
            conn.execute(f"UPDATE listing SET {', '.join(fields)} WHERE id=?", vals)
            updated = True

        # Событие (афиша): при смене даты/времени пересчитываем start_at_utc —
        # по нему бот фильтрует прошедшие события и сортирует выдачу.
        norm_event = None
        if body.event is not None:
            em = conn.execute(
                "SELECT start_date_local, start_time_local, timezone FROM events_meta WHERE listing_id=?",
                (listing_id,)
            ).fetchone()
            if em:
                ev = body.event
                em_fields, em_vals = [], []
                date_s = (ev.get("date") or "").strip() or (em[0] or "")
                time_s = (ev.get("time") or "").strip() or (em[1] or "00:00")
                if "date" in ev or "time" in ev:
                    dt_date = _parse_event_date(date_s)
                    t = _parse_event_time(time_s)
                    if dt_date is None:
                        raise HTTPException(400, "Не понял дату. Примеры: 07.10.25, 07-10-2025, 071025")
                    if t is None:
                        raise HTTPException(400, "Не понял время. Примеры: 19:00, 19.00, 1900, 19")
                    dt_local = dt_date.replace(hour=t[0], minute=t[1])
                    from zoneinfo import ZoneInfo
                    tz = ZoneInfo(em[2] or "Europe/Belgrade")
                    start_utc = int(dt_local.replace(tzinfo=tz).astimezone(datetime.timezone.utc).timestamp())
                    # Сохраняем в нормализованном виде — как это делает бот
                    date_norm = dt_local.strftime("%d-%m-%Y")
                    time_norm = f"{t[0]:02d}:{t[1]:02d}"
                    em_fields += ["start_date_local=?", "start_time_local=?", "start_at_utc=?"]
                    em_vals += [date_norm, time_norm, start_utc]
                    norm_event = {"date": date_norm, "time": time_norm}
                if "venue" in ev:
                    em_fields.append("venue_text=?"); em_vals.append((ev.get("venue") or "").strip())
                if "price_text" in ev:
                    em_fields.append("price_text=?"); em_vals.append((ev.get("price_text") or "").strip())
                if em_fields:
                    em_vals.append(listing_id)
                    conn.execute(f"UPDATE events_meta SET {', '.join(em_fields)} WHERE listing_id=?", em_vals)
                    updated = True

        if not updated:
            raise HTTPException(400, "Nothing to update")
        conn.commit()
    # norm_event — нормализованные дата/время, чтобы фронт показал их сразу
    return {"ok": True, **({"event": norm_event} if norm_event else {})}

@app.post("/api/listing/{listing_id}/toggle_sold")
def toggle_sold(listing_id: int):
    with db() as conn:
        row = conn.execute("SELECT is_sold FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        new_val = 0 if row[0] else 1
        conn.execute("UPDATE listing SET is_sold=? WHERE id=?", (new_val, listing_id))
        conn.commit()
    return {"ok": True, "is_sold": bool(new_val)}

@app.delete("/api/listing/{listing_id}")
def delete_listing(listing_id: int):
    with db() as conn:
        row = conn.execute("SELECT id FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        conn.execute("DELETE FROM listing WHERE id=?", (listing_id,))
        conn.execute("DELETE FROM listing_views WHERE listing_id=?", (listing_id,))
        conn.commit()
    return {"ok": True}

class RemovePhotoBody(BaseModel):
    photo_index: int

@app.post("/api/listing/{listing_id}/remove_photo")
def remove_photo(listing_id: int, body: RemovePhotoBody):
    with db() as conn:
        row = conn.execute("SELECT photo_file_id FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        photos = [p.strip() for p in (row[0] or "").split(",") if p.strip()]
        if body.photo_index < 0 or body.photo_index >= len(photos):
            raise HTTPException(400, "Invalid photo index")
        photos.pop(body.photo_index)
        conn.execute("UPDATE listing SET photo_file_id=? WHERE id=?",
                     (",".join(photos) or None, listing_id))
        conn.commit()
    return {"ok": True, "remaining": len(photos)}


# ── Загрузка фото/видео в объявление через Bot API ──────────────────────────
# Файл отправляется ботом в чат админа (UPLOAD_CHAT_ID) → Telegram возвращает
# file_id → он сохраняется в объявление. Байты хранятся у Telegram, как и все
# медиа бота. Побочный эффект: в вашем чате с ботом появляется сообщение-носитель.
UPLOAD_CHAT_ID = 519335258  # Telegram ID админа (@snd_producer)
MAX_PHOTOS = 3              # как в мастере добавления объявления в боте


async def _tg_upload(kind: str, filename: str, data: bytes) -> str:
    """kind: 'photo' | 'video'. Возвращает file_id."""
    method = "sendPhoto" if kind == "photo" else "sendVideo"
    field = "photo" if kind == "photo" else "video"
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data={"chat_id": UPLOAD_CHAT_ID, "disable_notification": "true"},
            files={field: (filename or f"upload.{'jpg' if kind == 'photo' else 'mp4'}", data)},
        )
        resp = r.json()
        if not resp.get("ok"):
            raise HTTPException(502, f"Telegram: {resp.get('description', 'upload failed')}")
        msg = resp["result"]
        file_id = msg["photo"][-1]["file_id"] if kind == "photo" else msg["video"]["file_id"]
        # Убираем сообщение-носитель из чата: file_id остаётся рабочим
        # и после удаления сообщения (файл живёт на серверах Telegram).
        try:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                data={"chat_id": UPLOAD_CHAT_ID, "message_id": msg["message_id"]},
            )
        except Exception:
            pass  # не удалилось — не критично, файл уже загружен
    return file_id


@app.post("/api/listing/{listing_id}/add_photo")
async def add_photo(listing_id: int, request: Request, filename: str = ""):
    if not BOT_TOKEN:
        raise HTTPException(503, "Bot token not configured")
    with db() as conn:
        row = conn.execute("SELECT photo_file_id FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        photos = [p.strip() for p in (row[0] or "").split(",") if p.strip()]
        if len(photos) >= MAX_PHOTOS:
            raise HTTPException(400, f"Максимум {MAX_PHOTOS} фото")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Пустой файл")
    file_id = await _tg_upload("photo", filename, data)
    with db() as conn:
        row = conn.execute("SELECT photo_file_id FROM listing WHERE id=?", (listing_id,)).fetchone()
        photos = [p.strip() for p in (row[0] or "").split(",") if p.strip()]
        photos.append(file_id)
        conn.execute("UPDATE listing SET photo_file_id=? WHERE id=?", (",".join(photos), listing_id))
        conn.commit()
    return {"ok": True, "file_id": file_id, "photos": photos}


@app.post("/api/listing/{listing_id}/add_video")
async def add_video(listing_id: int, request: Request, filename: str = ""):
    if not BOT_TOKEN:
        raise HTTPException(503, "Bot token not configured")
    with db() as conn:
        row = conn.execute("SELECT flex FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Пустой файл")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Видео больше 20 МБ: Bot API не сможет отдать его в приложении")
    file_id = await _tg_upload("video", filename, data)
    with db() as conn:
        row = conn.execute("SELECT flex FROM listing WHERE id=?", (listing_id,)).fetchone()
        try:
            flex = json.loads(row[0]) if row[0] else {}
        except Exception:
            flex = {}
        flex["video"] = file_id  # заменяет существующее видео
        conn.execute("UPDATE listing SET flex=? WHERE id=?",
                     (json.dumps(flex, ensure_ascii=False), listing_id))
        conn.commit()
    return {"ok": True, "file_id": file_id}

# Кэш скачанных медиа: без него каждая перемотка видео тянет файл из Telegram заново
_media_cache: dict = {}          # file_id -> (bytes, content_type)
_MEDIA_CACHE_MAX = 64 * 1024 * 1024  # ~64 МБ суммарно

_MIME_BY_EXT = {
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp",
}


async def _fetch_tg_file(file_id: str):
    if file_id in _media_cache:
        return _media_cache[file_id]
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile", params={"file_id": file_id})
        data = r.json()
        if not data.get("ok"):
            # Частая причина для видео: Bot API не отдаёт файлы больше 20 МБ
            desc = data.get("description", "File not found")
            raise HTTPException(404, desc)
        file_path = data["result"]["file_path"]
        f_r = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
        content = f_r.content
    content_type = f_r.headers.get("content-type", "")
    # Telegram нередко отдаёт octet-stream — определяем mime по расширению
    ext = os.path.splitext(file_path)[1].lower()
    if not content_type or content_type == "application/octet-stream":
        content_type = _MIME_BY_EXT.get(ext, "application/octet-stream")
    if content_type == "application/octet-stream":
        # Пути часто без расширения (videos/file_221) — судим по папке:
        # без корректного mime <video> в Safari молча не играет
        folder = file_path.split("/", 1)[0]
        content_type = {
            "videos": "video/mp4",
            "video_notes": "video/mp4",
            "animations": "video/mp4",
            "photos": "image/jpeg",
            "voice": "audio/ogg",
            "music": "audio/mpeg",
        }.get(folder, "application/octet-stream")
    # Простейшая защита кэша от разрастания
    if sum(len(v[0]) for v in _media_cache.values()) + len(content) > _MEDIA_CACHE_MAX:
        _media_cache.clear()
    _media_cache[file_id] = (content, content_type)
    return content, content_type


@app.get("/api/tg_photo/{file_id:path}")
async def tg_photo(file_id: str, request: Request):
    if not BOT_TOKEN:
        raise HTTPException(503, "Bot token not configured")
    content, content_type = await _fetch_tg_file(file_id)
    total = len(content)

    # Range-запросы обязательны для видео: Safari не воспроизводит <video>
    # без ответа 206 Partial Content. Нарезаем на своей стороне.
    range_header = request.headers.get("range")
    if range_header and range_header.startswith("bytes="):
        try:
            spec = range_header[6:].split(",")[0].strip()
            start_s, _, end_s = spec.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else total - 1
            end = min(end, total - 1)
            if start > end or start >= total:
                raise ValueError
        except ValueError:
            raise HTTPException(416, "Invalid range")
        chunk = content[start:end + 1]
        return StreamingResponse(
            iter([chunk]), status_code=206, media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{total}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(chunk)),
            },
        )
    return StreamingResponse(
        iter([content]), media_type=content_type,
        headers={"Accept-Ranges": "bytes", "Content-Length": str(total)},
    )


SECTION_ROOTS = {"market": 30, "service": 80, "vacancy": 90, "events": 100}
SECTION_NAMES = {"market": "Барахолка", "service": "Услуги", "vacancy": "Вакансии", "events": "Афиша"}


@app.get("/api/catalog/sections")
def catalog_sections():
    with db() as conn:
        def scl(sql, p=()):
            try: return conn.execute(sql, p).fetchone()[0] or 0
            except: return 0
        result = []
        for stype, root_id in SECTION_ROOTS.items():
            listings = scl("SELECT COUNT(*) FROM listing WHERE type=?", (stype,))
            views = scl("""SELECT COUNT(*) FROM listing_views lv
                           JOIN listing l ON l.id=lv.listing_id
                           WHERE l.type=? AND lv.action='open'""", (stype,))
            viewers = scl("""SELECT COUNT(DISTINCT lv.user_id) FROM listing_views lv
                             JOIN listing l ON l.id=lv.listing_id
                             WHERE l.type=? AND lv.action='open'""", (stype,))
            contacts = scl("""SELECT COUNT(*) FROM listing_views lv
                              JOIN listing l ON l.id=lv.listing_id
                              WHERE l.type=? AND lv.action='contact'""", (stype,))
            result.append({"type": stype, "name": SECTION_NAMES[stype], "root_id": root_id,
                           "listings": listings, "views": views, "viewers": viewers, "contacts": contacts})
    return result


@app.get("/api/catalog/cats/{parent_id}")
def catalog_cats(parent_id: int):
    with db() as conn:
        try:
            rows = conn.execute("""
                WITH RECURSIVE subtree(desc_id, root_id) AS (
                    SELECT id, id FROM category WHERE parent_id=?
                    UNION ALL
                    SELECT c.id, s.root_id FROM category c JOIN subtree s ON c.parent_id=s.desc_id
                )
                SELECT root.id, root.name,
                    (SELECT COUNT(*) FROM category WHERE parent_id=root.id) AS subcat_count,
                    COUNT(DISTINCT l.id) AS listings,
                    SUM(CASE WHEN lv.action='open' THEN 1 ELSE 0 END) AS views,
                    COUNT(DISTINCT CASE WHEN lv.action='open' THEN lv.user_id END) AS viewers,
                    SUM(CASE WHEN lv.action='contact' THEN 1 ELSE 0 END) AS contacts
                FROM category root
                LEFT JOIN subtree s ON s.root_id=root.id
                LEFT JOIN listing l ON l.category_id=s.desc_id
                LEFT JOIN listing_views lv ON lv.listing_id=l.id
                WHERE root.parent_id=?
                GROUP BY root.id, root.name
                ORDER BY root.order_num, root.name
            """, (parent_id, parent_id)).fetchall()
        except Exception as e:
            return {"rows": [], "error": str(e)}
    return {"rows": [{"id": r[0], "name": r[1], "subcats": r[2] or 0,
                      "listings": r[3] or 0, "views": r[4] or 0,
                      "viewers": r[5] or 0, "contacts": r[6] or 0} for r in rows]}


@app.get("/api/catalog/drill/listings")
def catalog_drill_listings(cat_id: int = 0, stype: str = "", offset: int = 0, limit: int = 24):
    with db() as conn:
        try:
            if cat_id:
                cte = ("WITH RECURSIVE subtree(desc_id) AS ("
                       "SELECT ? UNION ALL "
                       "SELECT c.id FROM category c JOIN subtree s ON c.parent_id=s.desc_id) ")
                where = "WHERE l.category_id IN (SELECT desc_id FROM subtree)"
                p = [cat_id]
            elif stype:
                cte = ""
                where = "WHERE l.type=?"
                p = [stype]
            else:
                cte = ""; where = ""; p = []
            total = conn.execute(
                f"{cte}SELECT COUNT(DISTINCT l.id) FROM listing l {where}", p
            ).fetchone()[0] or 0
            rows = conn.execute(f"""
                {cte}
                SELECT l.id, l.title, l.price, l.contact, l.photo_file_id, l.flex,
                       l.is_sold, l.created_at, l.type, l.status,
                       ci.name, cat.name,
                       COUNT(lv.id) AS opens,
                       COUNT(DISTINCT lv.user_id) AS users
                FROM listing l
                LEFT JOIN city ci ON ci.id=l.city_id
                LEFT JOIN category cat ON cat.id=l.category_id
                LEFT JOIN listing_views lv ON lv.listing_id=l.id AND lv.action='open'
                {where}
                GROUP BY l.id ORDER BY l.created_at DESC LIMIT ? OFFSET ?
            """, p + [limit, offset]).fetchall()
        except Exception as e:
            return {"total": 0, "rows": [], "error": str(e)}
    result = []
    for r in rows:
        photo_ids = [x.strip() for x in (r[4] or "").split(",") if x.strip()]
        video = _parse_video(r[5] or "")
        result.append({"id": r[0], "title": r[1] or "", "price": r[2] or "",
                        "contact": r[3] or "", "photo_ids": photo_ids, **video,
                        "is_sold": bool(r[6]), "created_at": (r[7] or "")[:10],
                        "type": r[8] or "", "status": r[9] or "",
                        "city": r[10] or "", "category": r[11] or "",
                        "opens": r[12] or 0, "users": r[13] or 0})
    return {"total": total, "offset": offset, "limit": limit, "rows": result}


@app.get("/api/catalog/drill/users")
def catalog_drill_users(cat_id: int = 0, stype: str = ""):
    with db() as conn:
        try:
            if cat_id:
                cte = ("WITH RECURSIVE subtree(desc_id) AS ("
                       "SELECT ? UNION ALL "
                       "SELECT c.id FROM category c JOIN subtree s ON c.parent_id=s.desc_id) ")
                where = "WHERE l.category_id IN (SELECT desc_id FROM subtree)"
                p = [cat_id]
            elif stype:
                cte = ""; where = "WHERE l.type=?"; p = [stype]
            else:
                cte = ""; where = ""; p = []
            rows = conn.execute(f"""
                {cte}
                SELECT lv.user_id, bu.username, bu.full_name, bu.last_seen,
                       SUM(CASE WHEN lv.action='open' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN lv.action='contact' THEN 1 ELSE 0 END)
                FROM listing_views lv
                JOIN listing l ON l.id=lv.listing_id
                LEFT JOIN BotUser bu ON bu.user_id=lv.user_id
                {where}
                GROUP BY lv.user_id ORDER BY 5 DESC
            """, p).fetchall()
        except Exception as e:
            return {"rows": [], "error": str(e)}
    return {"rows": [{"user_id": r[0], "username": r[1] or "", "full_name": r[2] or str(r[0]),
                      "last_seen": (r[3] or "")[:16], "opens": r[4] or 0, "contacts": r[5] or 0}
                     for r in rows]}


@app.get("/api/catalog/drill/views")
def catalog_drill_views(cat_id: int = 0, stype: str = "", action: str = "open",
                        offset: int = 0, limit: int = 50):
    with db() as conn:
        try:
            if cat_id:
                cte = ("WITH RECURSIVE subtree(desc_id) AS ("
                       "SELECT ? UNION ALL "
                       "SELECT c.id FROM category c JOIN subtree s ON c.parent_id=s.desc_id) ")
                where = "WHERE l.category_id IN (SELECT desc_id FROM subtree) AND lv.action=?"
                p = [cat_id, action]
            else:
                cte = ""
                conds = ["lv.action=?"]; p = [action]
                if stype:
                    conds.append("l.type=?"); p.append(stype)
                where = "WHERE " + " AND ".join(conds)
            total = conn.execute(
                f"{cte}SELECT COUNT(*) FROM listing_views lv JOIN listing l ON l.id=lv.listing_id {where}", p
            ).fetchone()[0] or 0
            rows = conn.execute(f"""
                {cte}
                SELECT datetime(lv.created_at,'unixepoch'), lv.user_id,
                       bu.username, bu.full_name, lv.listing_id, l.title
                FROM listing_views lv
                JOIN listing l ON l.id=lv.listing_id
                LEFT JOIN BotUser bu ON bu.user_id=lv.user_id
                {where}
                ORDER BY lv.created_at DESC LIMIT ? OFFSET ?
            """, p + [limit, offset]).fetchall()
        except Exception as e:
            return {"total": 0, "rows": [], "error": str(e)}
    return {"total": total, "rows": [
        {"ts": r[0] or "", "user_id": r[1], "username": r[2] or "",
         "full_name": r[3] or str(r[1]), "listing_id": r[4], "listing_title": r[5] or ""}
        for r in rows]}


@app.get("/api/listings")
def listings_catalog(offset: int = 0, limit: int = 24,
                     section: str = "", category_id: int = 0,
                     only_photo: bool = False,
                     only_active: bool = False, q: str = ""):
    with db() as conn:
        wheres = []
        params: list = []
        if section:
            wheres.append("l.type=?"); params.append(section)
        if category_id:
            wheres.append("l.category_id=?"); params.append(category_id)
        if only_photo:
            wheres.append("l.photo_file_id IS NOT NULL AND l.photo_file_id!=''")
        if only_active:
            wheres.append("(l.is_sold=0 OR l.is_sold IS NULL) AND (l.status='active' OR l.status IS NULL)")
        if q:
            wheres.append("(l.title LIKE ? OR l.descr LIKE ?)"); params += [f"%{q}%", f"%{q}%"]
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        try:
            total = conn.execute(f"SELECT COUNT(*) FROM listing l {where_sql}", params).fetchone()[0] or 0
            rows = conn.execute(f"""
                SELECT l.id, l.title, l.price, l.contact, l.photo_file_id, l.flex,
                       l.is_sold, l.created_at, l.type, l.status,
                       ci.name AS city_name,
                       cat.name AS category_name,
                       COUNT(lv.id) AS opens
                FROM listing l
                LEFT JOIN city ci ON ci.id=l.city_id
                LEFT JOIN category cat ON cat.id=l.category_id
                LEFT JOIN listing_views lv ON lv.listing_id=l.id AND lv.action='open'
                {where_sql}
                GROUP BY l.id
                ORDER BY l.created_at DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset]).fetchall()
        except Exception as e:
            return {"total": 0, "rows": [], "error": str(e)}
    result = []
    for r in rows:
        photo_ids = [p.strip() for p in (r[4] or "").split(",") if p.strip()]
        video = _parse_video(r[5] or "")
        result.append({
            "id": r[0], "title": r[1] or "", "price": r[2] or "",
            "contact": r[3] or "", "photo_ids": photo_ids, **video,
            "is_sold": bool(r[6]), "created_at": (r[7] or "")[:10],
            "type": r[8] or "", "status": r[9] or "",
            "city": r[10] or "", "category": r[11] or "", "opens": r[12] or 0,
        })
    return {"total": total, "offset": offset, "limit": limit, "rows": result}


# ─────────────────────────── HTML ───────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Category Admin</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
     background:#13131f;color:#dde;min-height:100vh}
.header{display:flex;align-items:center;justify-content:space-between;padding:18px 22px 0}
h1{font-size:18px;color:#fff;letter-spacing:-.3px}

/* tabs */
.tabs{display:flex;gap:5px;padding:14px 22px 0}
.tab{padding:7px 18px;border-radius:8px 8px 0 0;cursor:pointer;
     background:#1e1e35;color:#99a;border:none;font-size:14px;transition:.15s}
.tab.active{background:#161630;color:#7eb8f7;font-weight:600}

/* panel */
.panel{display:none;background:#161630;margin:0 22px 22px;
       border-radius:0 12px 12px 12px;padding:14px}
.panel.active{display:block}

/* toolbar */
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.btn{padding:6px 13px;border-radius:7px;border:none;cursor:pointer;
     font-size:12px;font-weight:500;transition:opacity .15s;white-space:nowrap}
.btn:hover{opacity:.82}
.btn-primary{background:#5a9cf5;color:#0a1a3d;font-weight:700}
.btn-sm{padding:2px 8px;border-radius:5px;font-size:11px}
.btn-add {background:#1b4d33;color:#6ef5aa}
.btn-move{background:#3d3010;color:#f5d06e}
.btn-del {background:#4d1010;color:#f57070}
.btn-fields{background:#1a2e5a;color:#7eb8f7}
.btn-ghost{background:#252540;color:#99a}

/* batch bar */
.batch-bar{display:none;position:sticky;top:0;z-index:50;
           background:#1e1e45;border:1px solid #3a3a70;border-radius:8px;
           padding:8px 12px;margin-bottom:10px;
           align-items:center;gap:10px;flex-wrap:wrap}
.batch-bar.visible{display:flex}
.batch-count{font-size:12px;color:#aac;flex:1}

/* tree */
.tree ul{list-style:none;margin-left:18px;border-left:1px solid #252548;padding-left:0}
.tree>ul{margin-left:0;border-left:none}
.tree li{position:relative}
.node{display:flex;align-items:center;gap:6px;
      padding:4px 6px;border-radius:5px;margin:1px 0;user-select:none}
.node:hover{background:#1e1e45}
.node.selected{background:#1a2e5a}
.chk{width:14px;height:14px;cursor:pointer;accent-color:#5a9cf5;flex-shrink:0}
.toggle{width:13px;color:#556;cursor:pointer;font-size:10px;flex-shrink:0;text-align:center}
.node-name{flex:1;font-size:13px;cursor:default}
.node-name.editing{background:#0d2450;border:1px solid #5a9cf5;border-radius:3px;
                   padding:1px 5px;outline:none;color:#fff;min-width:120px}
.badge{font-size:10px;color:#5a9cf5;background:#12204a;
       padding:1px 6px;border-radius:8px;min-width:20px;text-align:center}
.field-dot{width:6px;height:6px;border-radius:50%;background:#5a9cf5;
           flex-shrink:0;opacity:.7;title:"has fields"}
.drag-handle{cursor:grab;color:#556;font-size:13px;flex-shrink:0;
             padding:0 2px;user-select:none;opacity:.55;line-height:1}
.drag-handle:hover{opacity:1;color:#aac}
.drag-handle:active,.node[draggable="true"]:active .drag-handle{cursor:grabbing}
li.dragging>.node{opacity:.35}
li.drag-over-above>.node{box-shadow:0 -2px 0 0 #5a9cf5}
li.drag-over-below>.node{box-shadow:0 2px 0 0 #5a9cf5}
.node-actions{display:flex;gap:3px;opacity:0;transition:opacity .15s}
.node:hover .node-actions{opacity:1}

/* overlay + modal */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);
         z-index:100;align-items:flex-start;justify-content:center;overflow-y:auto;padding:40px 16px}
.overlay.open{display:flex}
.modal{background:#161630;border-radius:14px;padding:22px;width:460px;
       box-shadow:0 10px 50px rgba(0,0,0,.7);margin:auto;
       resize:both;overflow:auto;min-width:340px;min-height:160px}
.modal.wide{width:640px}
.modal h2{font-size:14px;margin-bottom:16px;color:#fff;font-weight:600}
.field-row{margin-bottom:11px}
.field-row label{display:block;font-size:11px;color:#778;margin-bottom:3px}
.field-row input,.field-row select{
  width:100%;background:#0d1f45;border:1px solid #2a3a6a;
  color:#dde;border-radius:7px;padding:7px 10px;font-size:12px}
.field-row input:focus,.field-row select:focus{outline:none;border-color:#5a9cf5}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
.err{color:#f57070;font-size:11px;margin-top:5px;min-height:16px}

/* fields editor */
.fields-list{display:flex;flex-direction:column;gap:6px;margin:12px 0}
.field-item{display:flex;align-items:center;gap:7px;
            background:#0d1f45;border-radius:7px;padding:7px 9px}
.field-item input,.field-item select{
  background:#0a1730;border:1px solid #1e3060;color:#dde;
  border-radius:5px;padding:4px 7px;font-size:11px}
.field-item input.fi-label{flex:2;min-width:80px}
.field-item input.fi-key{flex:1.5;min-width:60px}
.field-item select.fi-type{flex:1;min-width:70px}
.fi-req{width:14px;height:14px;accent-color:#5a9cf5;cursor:pointer}
.fi-ord{display:flex;flex-direction:column;gap:1px}
.fi-ord button{background:#1a2a50;border:none;color:#778;cursor:pointer;
               border-radius:3px;padding:0 4px;font-size:10px;line-height:1.6}
.fi-ord button:hover{color:#cce}
.fi-del{background:#4d1010;border:none;color:#f57070;cursor:pointer;
        border-radius:4px;padding:2px 6px;font-size:11px}
.meta-row{display:inline-flex;align-items:center;gap:12px;
          background:#0d2040;border-radius:7px;padding:8px 14px;margin-bottom:10px}
.meta-row label{font-size:12px;color:#aac;white-space:nowrap}
.toggle-sw{position:relative;width:36px;height:20px;flex-shrink:0}
.toggle-sw input{opacity:0;width:0;height:0}
.toggle-sw .slider{position:absolute;inset:0;background:#2a2a50;border-radius:20px;cursor:pointer;transition:.2s}
.toggle-sw input:checked+.slider{background:#5a9cf5}
.toggle-sw .slider::before{content:"";position:absolute;width:14px;height:14px;left:3px;top:3px;
  background:#fff;border-radius:50%;transition:.2s}
.toggle-sw input:checked+.slider::before{transform:translateX(16px)}
.fields-section-title{font-size:11px;color:#667;text-transform:uppercase;
                       letter-spacing:.5px;margin:8px 0 4px}
.copy-row{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.copy-row select{flex:1;background:#0d1f45;border:1px solid #2a3a6a;
                  color:#dde;border-radius:7px;padding:6px 9px;font-size:12px}
.empty{color:#445;font-style:italic;padding:10px}
.btn-edit{background:#1e1a40;color:#b89af5}
.emoji-picker{display:flex;flex-wrap:wrap;gap:3px;margin:6px 0 10px;
              max-height:30vh;overflow-y:auto;padding:5px;
              background:#0d1a3a;border-radius:7px;border:1px solid #1e3060}
.emoji-btn{background:none;border:none;cursor:pointer;font-size:19px;
           line-height:1;padding:3px 4px;border-radius:5px;transition:background .1s}
.emoji-btn:hover{background:#2a3a6a}

/* analytics */
.analytics-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}
.an-card{background:#0d1f45;border-radius:9px;padding:14px 16px}
.an-card-label{font-size:11px;color:#667;margin-bottom:6px}
.an-card-value{font-size:26px;font-weight:700;color:#7eb8f7;line-height:1}
.an-card-sub{font-size:11px;color:#556;margin-top:4px}
.an-up{color:#6ef5aa}.an-down{color:#f57070}
.analytics-charts{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px}
.an-chart-box{background:#0d1f45;border-radius:9px;padding:12px}
.an-chart-title{font-size:10px;color:#556;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.chart-wrap{position:relative}
.chart-y-labels{position:absolute;left:0;top:0;bottom:20px;display:flex;flex-direction:column;justify-content:space-between;pointer-events:none}
.chart-y-label{font-size:9px;color:#334;line-height:1}
.bar-chart{display:flex;align-items:flex-end;gap:2px;height:80px;margin-left:28px;border-left:1px solid #1a2a50;border-bottom:1px solid #1a2a50;position:relative}
.bar-wrap{flex:1;display:flex;align-items:flex-end;height:100%;cursor:default;position:relative}
.bar-fill{width:100%;border-radius:2px 2px 0 0;min-height:2px;transition:opacity .1s}
.bar-wrap:hover .bar-fill{opacity:.7}
.bar-wrap:hover::after{content:attr(data-tip);position:absolute;bottom:calc(100% + 3px);left:50%;transform:translateX(-50%);background:#1a2a60;color:#aac;font-size:9px;padding:2px 5px;border-radius:4px;white-space:nowrap;pointer-events:none;z-index:10}
.chart-x-labels{display:flex;justify-content:space-between;margin-left:28px;margin-top:2px;font-size:9px;color:#334}
.analytics-tables{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.an-table-box{background:#0d1f45;border-radius:9px;padding:12px;overflow:auto}
.an-table-title{font-size:10px;color:#556;text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
.an-table{width:100%;border-collapse:collapse;font-size:11px}
.an-table th{color:#778;text-align:left;padding:5px 8px;border-bottom:1px solid #1a2a50;font-weight:500;font-size:12px}
.an-table td{color:#aab;padding:5px 8px;border-bottom:1px solid #0d1628;font-size:13px}
.an-table tr:last-child td{border-bottom:none}
.an-table tr:hover td{background:#1a2a50;color:#cce}
/* analytics sub-nav */
.an-subnav{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:16px}
.an-nav-btn{padding:5px 12px;border-radius:6px;border:none;cursor:pointer;font-size:12px;
            background:#1a2050;color:#778;transition:.12s}
.an-nav-btn:hover{background:#22295a;color:#aab}
.an-nav-btn.active{background:#2a3580;color:#8ab4ff;font-weight:600}
.an-content{min-height:200px}
/* growth table */
.growth-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.growth-box{background:#0d1f45;border-radius:9px;padding:14px}
.growth-box h3{font-size:12px;color:#556;text-transform:uppercase;letter-spacing:.5px;margin-bottom:10px}
.growth-day{display:flex;justify-content:space-between;padding:3px 0;font-size:13px;border-bottom:1px solid #0d1628}
.growth-day:last-child{border:none}
.growth-day .day{color:#778}
.growth-day .val{color:#9bc;font-weight:600}
.growth-total{margin-top:8px;font-size:12px;color:#778;border-top:1px solid #1a2a50;padding-top:6px}
/* section breakdown */
.section-block{background:#0d1f45;border-radius:9px;padding:14px;margin-bottom:10px}
.section-name{font-size:13px;font-weight:700;color:#ccd;margin-bottom:8px}
.section-stats{display:flex;flex-wrap:wrap;gap:6px 20px;font-size:12px;color:#778}
.section-stats span b{color:#9bc}
/* search quality */
.sq-bar-row{display:flex;align-items:center;gap:10px;margin-bottom:7px}
.sq-mode{width:120px;font-size:12px;color:#778;flex-shrink:0}
.sq-bar{flex:1;height:12px;background:#1a2050;border-radius:6px;overflow:hidden}
.sq-bar-fill{height:100%;border-radius:6px;background:#5a9cf5;transition:width .3s}
.sq-count{font-size:12px;color:#9bc;width:80px;text-align:right;flex-shrink:0}
/* top cards */
.card-row{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid #0d1628;overflow:hidden;min-width:0}
.card-row:last-child{border:none}
.card-thumb{width:48px;height:48px;border-radius:6px;object-fit:cover;background:#0d1628;flex-shrink:0}
.card-thumb-empty{width:48px;height:48px;border-radius:6px;background:#111830;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.card-info{flex:1;min-width:0}
.card-title{font-size:13px;color:#ccd;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-meta{font-size:11px;color:#778;margin-top:2px}
.card-stats{text-align:right;font-size:12px;color:#9bc;flex-shrink:0;max-width:100px}
/* card grid column gap so stats don't visually collide with next column's thumbnail */
.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));column-gap:20px}
/* listing modal above drill modal */
#modal-listing{z-index:105}
/* owners */
.owner-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid #0d1628;cursor:pointer;transition:.1s}
.owner-row:hover{background:#0d1628}
.owner-row:last-child{border:none}
.owner-contact{font-size:13px;color:#9bc}
.owner-stats{font-size:12px;color:#778}
.owner-detail{background:#0d1f45;border-radius:9px;padding:14px;margin-bottom:10px}
.owner-detail h3{font-size:14px;color:#ccd;margin-bottom:8px}
.owner-detail-stats{display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:12px;color:#778;margin-bottom:12px}
.owner-detail-stats b{color:#9bc}
.back-btn{display:inline-flex;align-items:center;gap:5px;padding:5px 10px;border-radius:6px;
          border:none;cursor:pointer;background:#1a2050;color:#778;font-size:12px;margin-bottom:12px}
.back-btn:hover{background:#22295a;color:#aab}
.an-pagination{display:flex;gap:8px;align-items:center;margin-top:12px;font-size:12px;color:#778}
.an-pagination button{padding:4px 10px;border-radius:5px;border:none;cursor:pointer;background:#1a2050;color:#778}
.an-pagination button:hover{background:#22295a;color:#aab}
.an-pagination button:disabled{opacity:.35;cursor:default}
/* catalog tree */
.catalog-breadcrumb{display:flex;align-items:center;gap:6px;margin-bottom:14px;flex-wrap:wrap;min-height:28px}
.bc-item{font-size:13px;color:#778;cursor:pointer}
.bc-item:hover{color:#aab}
.bc-item.current{color:#ccd;cursor:default}
.bc-sep{color:#334;font-size:12px}
.bc-back{display:flex;align-items:center;gap:6px;padding:5px 10px;background:#1a2050;
  border:none;border-radius:6px;color:#778;font-size:13px;cursor:pointer;margin-right:8px}
.bc-back:hover{background:#22295a;color:#aab}
/* section & category rows — fixed grid so columns align */
.cat-tree-list{display:table;width:100%;border-spacing:0 5px}
.cat-tree-row{display:table-row;cursor:pointer;border-radius:9px}
.cat-tree-row > *{display:table-cell;vertical-align:middle;padding:10px 8px;
  background:#0d1f45;transition:background .12s}
.cat-tree-row > *:first-child{padding-left:14px;border-radius:9px 0 0 9px}
.cat-tree-row > *:last-child{padding-right:14px;border-radius:0 9px 9px 0}
.cat-tree-row:hover > *{background:#111f55}
.cat-tree-icon{width:34px;font-size:18px;text-align:center}
.cat-tree-name{font-size:14px;font-weight:600;color:#dde;min-width:160px}
.cat-tree-col{width:110px;text-align:right;font-size:12px;color:#556;white-space:nowrap}
.cat-tree-col b{color:#9bc;font-size:13px;margin-right:2px}
.cat-tree-subcount{width:80px;text-align:center}
.cat-tree-subcount span{font-size:11px;color:#556;background:#0d1628;border-radius:4px;padding:2px 7px}
.cat-tree-arrow{width:22px;text-align:right;color:#334;font-size:18px}
.lm-admin-bar{display:flex;gap:8px;margin-top:16px;padding-top:12px;border-top:1px solid #1a2540;flex-wrap:wrap}
.lm-field-group{display:flex;flex-direction:column;gap:4px;margin-bottom:2px}
.lm-field-label{font-size:11px;color:#6a8aaa;text-transform:uppercase;letter-spacing:.05em;font-weight:600}
.lm-edit-input{width:100%;background:#0d1628;color:#c8d8f0;border:1px solid #2a3a5a;border-radius:6px;padding:8px 10px;font-size:14px;box-sizing:border-box;font-family:inherit}
textarea.lm-edit-input{resize:vertical;min-height:80px}
.lm-photo-del{position:absolute;top:4px;right:4px;width:24px;height:24px;border-radius:50%;background:rgba(180,0,0,.85);color:#fff;border:none;cursor:pointer;font-size:16px;line-height:1;display:flex;align-items:center;justify-content:center;z-index:10}
.lm-photo-del:hover{background:#c00}
.cat-drill{cursor:pointer}
.cat-drill:hover{background:#162050 !important}
.cat-drill:hover b{color:#7eb8f7;text-decoration:underline dotted}
/* listings in catalog */
.catalog-listings-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px;margin-top:12px}
.cat-card{background:#0d1f45;border-radius:10px;overflow:hidden;cursor:pointer;transition:.12s}
.cat-card:hover{background:#111f50;transform:translateY(-1px)}
.cat-card-media{position:relative;height:180px;background:#0d1628;overflow:hidden}
.cat-card-media img{width:100%;height:100%;object-fit:cover;display:block}
.cat-card-media-empty{display:flex;align-items:center;justify-content:center;height:100%;font-size:36px;color:#2a3060}
.cat-card-video-badge{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.7);
  border-radius:5px;padding:2px 7px;font-size:11px;color:#fff}
.cat-card-sold{position:absolute;top:8px;right:8px;background:#8b1a1a;
  color:#faa;border-radius:5px;padding:2px 7px;font-size:11px;font-weight:600}
.cat-card-photos-count{position:absolute;bottom:6px;right:8px;background:rgba(0,0,0,.6);
  color:#ccd;border-radius:4px;padding:1px 6px;font-size:10px}
.cat-card-body{padding:10px 12px}
.cat-card-title{font-size:14px;font-weight:700;color:#eef;margin-bottom:3px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cat-card-price{font-size:15px;color:#6ef5aa;font-weight:700;margin-bottom:4px}
.cat-card-meta{font-size:11px;color:#778;line-height:1.5}
.cat-card-stats{font-size:11px;color:#445;margin-top:4px}
.card-row:hover{background:#0d1f35}
/* listing modal */
.modal.listing-modal{width:700px;max-width:96vw;max-height:90vh;overflow-y:auto;padding:24px}
.listing-photos{display:flex;gap:10px;margin-bottom:18px;flex-wrap:wrap}
.listing-photo{flex:1;min-width:0;max-width:33%;border-radius:10px;overflow:hidden;background:#0d1628;cursor:zoom-in}
.listing-photo img{width:100%;height:220px;object-fit:cover;display:block;transition:.15s}
.listing-photo img:hover{opacity:.85}
.listing-photo-empty{height:160px;display:flex;align-items:center;justify-content:center;font-size:40px;color:#334}
.listing-title{font-size:18px;font-weight:700;color:#eef;margin-bottom:6px}
.listing-price{font-size:22px;font-weight:800;color:#6ef5aa;margin-bottom:12px}
.listing-descr{font-size:13px;color:#aab;line-height:1.6;white-space:pre-wrap;margin-bottom:14px;padding:10px 12px;background:#0d1628;border-radius:8px}
.listing-meta{display:grid;grid-template-columns:auto 1fr;gap:5px 12px;font-size:13px;margin-bottom:14px}
.listing-meta-key{color:#556;white-space:nowrap}
.listing-meta-val{color:#aab}
.listing-meta-val a{color:#7eb8f7}
.listing-stats{display:flex;gap:12px;flex-wrap:wrap;padding:10px 12px;background:#0d1f45;border-radius:8px;font-size:12px;color:#778}
.listing-stats span b{color:#9bc;font-size:14px}
.listing-sold-badge{display:inline-block;background:#8b1a1a;color:#faa;padding:3px 10px;border-radius:5px;font-size:12px;font-weight:600;margin-bottom:10px}
/* photo lightbox */
.lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:9999;align-items:center;justify-content:center}
.lightbox.open{display:flex}
.lightbox-img-wrap{position:relative;display:flex;align-items:center;justify-content:center}
.lightbox img{max-width:88vw;max-height:90vh;border-radius:10px;object-fit:contain;cursor:zoom-out}
.lb-arrow{position:fixed;top:50%;transform:translateY(-50%);background:rgba(255,255,255,.12);
  border:none;color:#fff;font-size:28px;padding:16px 14px;cursor:pointer;z-index:10000;
  border-radius:8px;transition:.15s;user-select:none}
.lb-arrow:hover{background:rgba(255,255,255,.25)}
.lb-prev{left:16px}
.lb-next{right:16px}
.lb-counter{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);
  background:rgba(0,0,0,.6);color:#ccd;font-size:13px;padding:4px 14px;border-radius:20px;z-index:10000}
.lb-close{position:fixed;top:16px;right:20px;background:none;border:none;color:#aab;
  font-size:26px;cursor:pointer;z-index:10000;line-height:1}
</style>
</head>
<body>
<div class="header">
  <h1 data-i18n="title">Категории</h1>
  <button class="btn btn-ghost btn-sm" onclick="toggleLang()" id="lang-btn">EN</button>
</div>

<div class="tabs">
  <button class="tab active" onclick="switchTab('market')" data-i18n="tab_market">Барахолка</button>
  <button class="tab" onclick="switchTab('services')" data-i18n="tab_services">Услуги</button>
  <button class="tab" onclick="switchTab('vacancy')" data-i18n="tab_vacancy">Вакансии</button>
  <button class="tab" onclick="switchTab('analytics')" data-i18n="tab_analytics">📊 Аналитика</button>
  <button class="tab" onclick="switchTab('catalog')" data-i18n="tab_catalog">📦 Объявления</button>
</div>

<div id="panel-market"  class="panel active"><div class="toolbar">
  <button class="btn btn-primary" onclick="openAdd(null,'market')" data-i18n="add_top">+ Добавить корневую</button>
  <button class="btn btn-ghost"   onclick="clearSelection()" data-i18n="clear_sel">Снять выделение</button>
</div><div class="batch-bar" id="batch-market">
  <span class="batch-count" id="batch-count-market">0 выбрано</span>
  <button class="btn btn-fields" onclick="openBatchField('market')" data-i18n="add_field_sel">Добавить поле к выбранным</button>
  <button class="btn btn-ghost"  onclick="clearSelection()">✕</button>
</div><div class="tree" id="tree-market"></div></div>

<div id="panel-services" class="panel"><div class="toolbar">
  <button class="btn btn-primary" onclick="openAdd(null,'services')" data-i18n="add_top">+ Добавить корневую</button>
  <button class="btn btn-ghost"   onclick="clearSelection()" data-i18n="clear_sel">Снять выделение</button>
</div><div class="batch-bar" id="batch-services">
  <span class="batch-count" id="batch-count-services">0 выбрано</span>
  <button class="btn btn-fields" onclick="openBatchField('services')" data-i18n="add_field_sel">Добавить поле к выбранным</button>
  <button class="btn btn-ghost"  onclick="clearSelection()">✕</button>
</div><div class="tree" id="tree-services"></div></div>

<div id="panel-vacancy" class="panel"><div class="toolbar">
  <button class="btn btn-primary" onclick="openAdd(null,'vacancy')" data-i18n="add_top">+ Добавить корневую</button>
  <button class="btn btn-ghost"   onclick="clearSelection()" data-i18n="clear_sel">Снять выделение</button>
</div><div class="batch-bar" id="batch-vacancy">
  <span class="batch-count" id="batch-count-vacancy">0 выбрано</span>
  <button class="btn btn-fields" onclick="openBatchField('vacancy')" data-i18n="add_field_sel">Добавить поле к выбранным</button>
  <button class="btn btn-ghost"  onclick="clearSelection()">✕</button>
</div><div class="tree" id="tree-vacancy"></div></div>

<div id="panel-analytics" class="panel">
  <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">
    <button class="btn btn-ghost btn-sm" onclick="anReload()" data-i18n="an_refresh">↻ Обновить</button>
    <span style="font-size:12px;color:#445" id="an-updated"></span>
  </div>
  <div class="an-subnav">
    <button class="an-nav-btn active" data-an="overview" onclick="anSwitch('overview',this)">📊 Обзор</button>
    <button class="an-nav-btn" data-an="growth" onclick="anSwitch('growth',this)">📅 Рост</button>
    <button class="an-nav-btn" data-an="sections" onclick="anSwitch('sections',this)">📂 По разделам</button>
    <button class="an-nav-btn" data-an="top_searches" onclick="anSwitch('top_searches',this)">🔎 Запросы</button>
    <button class="an-nav-btn" data-an="no_results" onclick="anSwitch('no_results',this)">❌ Пустые</button>
    <button class="an-nav-btn" data-an="search_quality" onclick="anSwitch('search_quality',this)">🧠 Типы поиска</button>
    <button class="an-nav-btn" data-an="top_cards" onclick="anSwitch('top_cards',this)">🔥 Топ карточек</button>
    <button class="an-nav-btn" data-an="sources" onclick="anSwitch('sources',this)">📈 Источники</button>
    <button class="an-nav-btn" data-an="search_conversion" onclick="anSwitch('search_conversion',this)">🔁 Search→Open</button>
    <button class="an-nav-btn" data-an="owners" onclick="anSwitch('owners',this)">👤 Авторы</button>
    <button class="an-nav-btn" data-an="cities" onclick="anSwitch('cities',this)">🏙 Города</button>
  </div>
  <div class="an-content" id="an-content"></div>
</div>

<!-- Add/Edit modal -->
<div class="overlay" id="modal-add">
  <div class="modal">
    <h2 id="add-title">Новая категория</h2>
    <div class="field-row"><label data-i18n="lbl_name">Название</label><input id="a-name" oninput="autoSlug('a-name','a-slug')"></div>
    <div class="emoji-picker" id="emoji-picker"></div>
    <div class="field-row"><label data-i18n="lbl_slug">Slug</label><input id="a-slug"></div>
    <div class="field-row" id="a-parent-wrap">
      <label data-i18n="lbl_parent">Родительская категория</label><select id="a-parent"></select>
    </div>
    <div class="err" id="add-err"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeAll()" data-i18n="cancel">Отмена</button>
      <button class="btn btn-primary" onclick="submitAdd()" data-i18n="save">Сохранить</button>
    </div>
  </div>
</div>

<!-- Move modal -->
<div class="overlay" id="modal-move">
  <div class="modal">
    <h2 data-i18n="move_to">Переместить в…</h2>
    <div class="field-row"><label data-i18n="lbl_new_parent">Новый родитель</label><select id="m-parent"></select></div>
    <div class="err" id="move-err"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeAll()" data-i18n="cancel">Отмена</button>
      <button class="btn btn-primary" onclick="submitMove()" data-i18n="move">Переместить</button>
    </div>
  </div>
</div>

<!-- Fields editor modal -->
<div class="overlay" id="modal-fields">
  <div class="modal wide">
    <h2 id="fields-title">Поля</h2>
    <!-- Extra categories toggle -->
    <div class="meta-row">
      <label data-i18n="allow_extra">Разрешить доп. категории</label>
      <label class="toggle-sw"><input type="checkbox" id="f-extra"><span class="slider"></span></label>
    </div>
    <!-- Copy from -->
    <div class="copy-row">
      <select id="f-copy-src"><option value="" data-i18n="copy_from">— Скопировать поля из категории…</option></select>
      <button class="btn btn-ghost btn-sm" onclick="copyFields()" data-i18n="copy">Скопировать</button>
    </div>
    <div class="fields-section-title" data-i18n="fields_title">Поля при создании объявления</div>
    <div class="fields-list" id="fields-list"></div>
    <button class="btn btn-add btn-sm" onclick="addFieldRow()" data-i18n="add_field">+ Добавить поле</button>
    <div class="err" id="fields-err"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeAll()" data-i18n="cancel">Отмена</button>
      <button class="btn btn-primary" onclick="saveFields()" data-i18n="save">Сохранить</button>
    </div>
  </div>
</div>

<!-- Batch field modal -->
<div class="overlay" id="modal-batch">
  <div class="modal">
    <h2 data-i18n="batch_title">Добавить поле к выбранным категориям</h2>
    <div class="field-row"><label data-i18n="lbl_label">Название (для пользователя)</label><input id="bf-label" oninput="autoSlug('bf-label','bf-key')"></div>
    <div class="field-row"><label data-i18n="lbl_key">Ключ (латиница)</label><input id="bf-key"></div>
    <div class="field-row"><label data-i18n="lbl_type">Тип</label>
      <select id="bf-type">
        <option value="text">text</option>
        <option value="number">number</option>
        <option value="checkbox">checkbox</option>
        <option value="video">video</option>
      </select>
    </div>
    <div class="field-row" style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="bf-req" style="width:auto">
      <label for="bf-req" style="font-size:12px;color:#aac;display:inline" data-i18n="lbl_required">Обязательное</label>
    </div>
    <div class="err" id="batch-err"></div>
    <div class="modal-actions">
      <button class="btn btn-ghost" onclick="closeAll()" data-i18n="cancel">Отмена</button>
      <button class="btn btn-primary" onclick="submitBatch()" data-i18n="add_to_all">Добавить ко всем выбранным</button>
    </div>
  </div>
</div>

<!-- Catalog panel -->
<div id="panel-catalog" class="panel">
  <div id="catalog-breadcrumb" class="catalog-breadcrumb"></div>
  <div id="catalog-content"></div>
</div>

<!-- Listing detail modal -->
<div class="overlay" id="modal-listing" onclick="if(event.target===this)closeListingModal()">
  <div class="modal listing-modal" id="modal-listing-body">
    <div style="display:flex;justify-content:flex-end;margin-bottom:12px">
      <button class="btn btn-ghost btn-sm" onclick="closeListingModal()">✕ Закрыть</button>
    </div>
    <div id="listing-content"><div class="empty" style="padding:40px;text-align:center">…</div></div>
  </div>
</div>

<!-- Drill-down detail modal -->
<div class="overlay" id="modal-drill" onclick="if(event.target===this)closeDrill()">
  <div class="modal" style="width:740px;max-width:96vw;max-height:90vh;overflow-y:auto;padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
      <h2 id="drill-title" style="font-size:15px;margin:0;color:#eef"></h2>
      <button class="btn btn-ghost btn-sm" onclick="closeDrill()">✕ Закрыть</button>
    </div>
    <div id="drill-content"></div>
  </div>
</div>

<!-- Lightbox for full-size photos -->
<div class="lightbox" id="lightbox">
  <button class="lb-close" onclick="closeLightbox()">✕</button>
  <button class="lb-arrow lb-prev" id="lb-prev" onclick="lbGo(-1)">‹</button>
  <div class="lightbox-img-wrap" onclick="closeLightbox()">
    <img id="lightbox-img" src="" alt="">
  </div>
  <button class="lb-arrow lb-next" id="lb-next" onclick="lbGo(1)">›</button>
  <div class="lb-counter" id="lb-counter"></div>
</div>

<script>
// ── i18n ──
const I18N = {
  ru: {
    title:'Категории', tab_market:'Барахолка', tab_services:'Услуги', tab_vacancy:'Вакансии', tab_analytics:'📊 Аналитика', tab_catalog:'📦 Объявления',
    an_refresh:'↻ Обновить', an_chart_users:'Новые пользователи · 30 дней', an_chart_listings:'Новые объявления · 30 дней',
    an_top_cats:'Топ категорий', an_top_searches:'Поисковые запросы',
    an_users:'Пользователи', an_listings:'Активных объявлений', an_search:'Поисковых запросов', an_views:'Просмотров карточек',
    an_total:'Всего', an_new_week:'За неделю', an_no_result:'Без результата', an_contacts:'Написали',
    an_col_cat:'Категория', an_col_listings:'Объявл.', an_col_opens:'Просм.', an_col_contacts:'Контакт',
    an_col_query:'Запрос', an_col_count:'Кол-во', an_col_hit:'Успешных',
    add_top:'+ Добавить корневую', clear_sel:'Снять выделение', add_field_sel:'Добавить поле к выбранным',
    empty:'Нет категорий', new_cat:'Новая категория', new_subcat:'Новая подкатегория',
    lbl_name:'Название', lbl_slug:'Slug', lbl_parent:'Родительская категория',
    cancel:'Отмена', save:'Сохранить', move_to:'Переместить в…', lbl_new_parent:'Новый родитель', move:'Переместить',
    allow_extra:'Разрешить доп. категории', copy_from:'— Скопировать поля из категории…',
    copy:'Скопировать', fields_title:'Поля при создании объявления', add_field:'+ Добавить поле',
    batch_title:'Добавить поле к выбранным категориям', lbl_label:'Название (для пользователя)',
    lbl_key:'Ключ (латиница)', lbl_type:'Тип', lbl_required:'Обязательное', add_to_all:'Добавить ко всем выбранным',
    del_confirm:'Удалить «{name}»?\n\nВозможно только если нет подкатегорий и активных объявлений.',
    err_name:'Введите название', err_slug:'Введите slug', err_label:'Введите название поля',
    err_key:'Введите ключ', err_fields:'У всех полей должен быть ключ и тип',
    tt_edit:'Переименовать', tt_add:'Добавить подкатегорию', tt_fields:'Редактировать поля', tt_move:'Переместить в…', tt_delete:'Удалить',
    tt_up:'Выше', tt_down:'Ниже', tt_has_fields:'Есть поля',
    n_selected:'{n} выбрано', top_level:'верхний уровень', no_fields:'Нет полей', fields_hdr:'Поля — ',
    edit_cat:'Редактировать категорию',
  },
  en: {
    title:'Category Admin', tab_market:'Market', tab_services:'Services', tab_vacancy:'Vacancies', tab_analytics:'📊 Analytics', tab_catalog:'📦 Listings',
    an_refresh:'↻ Refresh', an_chart_users:'New users · 30 days', an_chart_listings:'New listings · 30 days',
    an_top_cats:'Top categories', an_top_searches:'Search queries',
    an_users:'Users', an_listings:'Active listings', an_search:'Search queries', an_views:'Card views',
    an_total:'Total', an_new_week:'This week', an_no_result:'No results', an_contacts:'Contacted',
    an_col_cat:'Category', an_col_listings:'Listings', an_col_opens:'Views', an_col_contacts:'Contacts',
    an_col_query:'Query', an_col_count:'Count', an_col_hit:'Hit rate',
    add_top:'+ Add top-level', clear_sel:'Clear selection', add_field_sel:'Add field to selected',
    empty:'No categories', new_cat:'New category', new_subcat:'New subcategory',
    lbl_name:'Name', lbl_slug:'Slug', lbl_parent:'Parent category',
    cancel:'Cancel', save:'Save', move_to:'Move to…', lbl_new_parent:'New parent', move:'Move',
    allow_extra:'Allow extra categories', copy_from:'— Copy fields from category…',
    copy:'Copy', fields_title:'Fields shown when creating a listing', add_field:'+ Add field',
    batch_title:'Add field to selected categories', lbl_label:'Label (shown to user)',
    lbl_key:'Key (latin identifier)', lbl_type:'Type', lbl_required:'Required', add_to_all:'Add to all selected',
    del_confirm:'Delete «{name}»?\n\nOnly possible if no subcategories and no active listings.',
    err_name:'Enter name', err_slug:'Enter slug', err_label:'Enter label',
    err_key:'Enter key', err_fields:'All fields must have a key and type',
    tt_edit:'Rename', tt_add:'Add subcategory', tt_fields:'Edit fields', tt_move:'Move to…', tt_delete:'Delete',
    tt_up:'Move up', tt_down:'Move down', tt_has_fields:'Has fields',
    n_selected:'{n} selected', top_level:'top level', no_fields:'No fields yet', fields_hdr:'Fields — ',
    edit_cat:'Edit category',
  }
};
let lang = localStorage.getItem('admin_lang') || 'ru';
function T(key){ return (I18N[lang]||I18N.ru)[key] || I18N.en[key] || key; }
function toggleLang(){
  lang = lang==='ru' ? 'en' : 'ru';
  localStorage.setItem('admin_lang', lang);
  applyLang();
  loadAll().then(()=>loadTree(currentTab));
}
function applyLang(){
  document.getElementById('lang-btn').textContent = lang==='ru' ? 'EN' : 'RU';
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    el.textContent = T(el.dataset.i18n);
  });
}

// ── state ──
let currentTab = 'market';
let allCats = [];
let addData = {};
let moveTargetId = null, moveSection = null;
let fieldsTargetId = null, fieldsSection = null;
let batchSection = null;
let selected = new Set();

const ROOT_IDS = {market:30, services:80, vacancy:90};

// ── data ──
async function loadAll() {
  const r = await fetch('/api/all_categories');
  allCats = await r.json();
}
async function loadTree(section) {
  const r = await fetch(`/api/tree/${section}`);
  const tree = await r.json();
  const el = document.getElementById(`tree-${section}`);
  el.innerHTML = tree.length ? renderList(tree, section, true, ROOT_IDS[section]) : `<div class="empty">${T('empty')}</div>`;
}

// ── render ──
function renderList(nodes, section, isRoot, parentId) {
  return '<ul>' + nodes.map(n => renderNode(n, section, isRoot, parentId)).join('') + '</ul>';
}
function renderNode(n, section, isRoot, parentId) {
  const hasKids = n.children && n.children.length > 0;
  const tog = hasKids
    ? `<span class="toggle" onclick="toggleNode(this)">▶</span>`
    : `<span class="toggle"></span>`;
  const kids = hasKids
    ? `<div class="kids" style="display:none">${renderList(n.children, section, false, n.id)}</div>` : '';
  const dot = n.has_fields ? `<span class="field-dot" title="${T('tt_has_fields')}"></span>` : '';
  const sel = selected.has(n.id) ? ' selected' : '';
  return `<li data-id="${n.id}" data-parent="${parentId}">
    <div class="node${sel}" data-id="${n.id}" data-section="${section}" draggable="true">
      <span class="drag-handle" title="Drag to reorder">⠿</span>
      <input type="checkbox" class="chk" ${selected.has(n.id)?'checked':''} onchange="toggleSelect(${n.id},'${section}',this)">
      ${tog}
      <span class="node-name" ondblclick="startEdit(this,${n.id})">${esc(n.name)}</span>
      ${dot}
      <span class="badge">${n.count||0}</span>
      <div class="node-actions">
        <button class="btn btn-sm btn-edit"   title="${T('tt_edit')}"   onclick="openEdit(${n.id},'${section}')">✏</button>
        <button class="btn btn-sm btn-add"    title="${T('tt_add')}"    onclick="openAdd(${n.id},'${section}')">+</button>
        <button class="btn btn-sm btn-fields" title="${T('tt_fields')}" onclick="openFields(${n.id},'${section}')">⚙</button>
        <button class="btn btn-sm btn-move"   title="${T('tt_move')}"   onclick="openMove(${n.id},'${section}')">↕</button>
        <button class="btn btn-sm btn-del"    title="${T('tt_delete')}" onclick="deleteCat(${n.id},'${section}','${esc(n.name)}')">✕</button>
      </div>
    </div>
    ${kids}
  </li>`;
}

function toggleNode(el) {
  const kids = el.closest('li').querySelector('.kids');
  if (!kids) return;
  const open = kids.style.display !== 'none';
  kids.style.display = open ? 'none' : 'block';
  el.textContent = open ? '▶' : '▼';
}
function esc(s){ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') }

// ── selection ──
function toggleSelect(id, section, chk) {
  if (chk.checked) selected.add(id); else selected.delete(id);
  const node = chk.closest('.node');
  node.classList.toggle('selected', chk.checked);
  updateBatchBar(section);
}
function updateBatchBar(section) {
  const count = selected.size;
  const bar = document.getElementById(`batch-${section}`);
  const cnt = document.getElementById(`batch-count-${section}`);
  bar.classList.toggle('visible', count > 0);
  cnt.textContent = T('n_selected').replace('{n}', count);
}
function clearSelection() {
  selected.clear();
  document.querySelectorAll('.chk').forEach(c => c.checked = false);
  document.querySelectorAll('.node.selected').forEach(n => n.classList.remove('selected'));
  ['market','services','vacancy'].forEach(s => {
    document.getElementById(`batch-${s}`).classList.remove('visible');
  });
}

// ── inline edit ──
function startEdit(el, id) {
  const old = el.textContent;
  el.contentEditable = true;
  el.classList.add('editing');
  el.focus();
  window.getSelection().selectAllChildren(el);
  el.onblur = () => finishEdit(el, id, old);
  el.onkeydown = e => {
    if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    if (e.key === 'Escape') { el.textContent = old; el.blur(); }
  };
}
async function finishEdit(el, id, old) {
  el.contentEditable = false; el.classList.remove('editing');
  el.onblur = null; el.onkeydown = null;
  const name = el.textContent.trim();
  if (!name || name === old) { el.textContent = old; return; }
  const r = await fetch(`/api/categories/${id}`, {
    method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})
  });
  if (!r.ok) { alert((await r.json()).detail||'Error'); el.textContent = old; }
}

// ── drag & drop reorder ──
let _dragId = null, _dragParent = null, _dragSection = null;

function setupDnD() {
  document.addEventListener('dragstart', e => {
    const node = e.target.closest('.node[draggable]');
    if (!node) return;
    _dragId = parseInt(node.dataset.id);
    _dragParent = parseInt(node.closest('li').dataset.parent);
    _dragSection = node.dataset.section;
    e.dataTransfer.effectAllowed = 'move';
    setTimeout(() => node.closest('li').classList.add('dragging'), 0);
  });

  document.addEventListener('dragend', () => {
    document.querySelectorAll('.dragging,.drag-over-above,.drag-over-below')
      .forEach(el => el.classList.remove('dragging','drag-over-above','drag-over-below'));
    _dragId = null;
  });

  document.addEventListener('dragover', e => {
    if (_dragId === null) return;
    const li = e.target.closest('li[data-parent]');
    if (!li) return;
    if (parseInt(li.dataset.parent) !== _dragParent) { e.dataTransfer.dropEffect='none'; return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    document.querySelectorAll('.drag-over-above,.drag-over-below')
      .forEach(el => el.classList.remove('drag-over-above','drag-over-below'));
    const nodeEl = li.querySelector(':scope>.node');
    const mid = nodeEl.getBoundingClientRect().top + nodeEl.offsetHeight / 2;
    li.classList.add(e.clientY < mid ? 'drag-over-above' : 'drag-over-below');
  });

  document.addEventListener('drop', async e => {
    e.preventDefault();
    const li = e.target.closest('li[data-parent]');
    if (!li || _dragId === null) return;
    const targetId = parseInt(li.dataset.id);
    if (targetId === _dragId) return;
    if (parseInt(li.dataset.parent) !== _dragParent) return;

    const insertAbove = li.classList.contains('drag-over-above');
    const section = _dragSection, parentId = _dragParent, dragId = _dragId;

    document.querySelectorAll('.dragging,.drag-over-above,.drag-over-below')
      .forEach(el => el.classList.remove('dragging','drag-over-above','drag-over-below'));
    _dragId = null;

    const ul = li.parentElement;
    const ids = [...ul.querySelectorAll(':scope>li')].map(s => parseInt(s.dataset.id)).filter(id => id !== dragId);
    const idx = ids.indexOf(targetId);
    ids.splice(insertAbove ? idx : idx + 1, 0, dragId);

    await fetch('/api/categories/reorder_siblings', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({parent_id: parentId, ids})
    });
    await loadTree(section);
  });
}

// ── emoji picker ──
const EMOJIS = [
  '🎵','🎶','🎼','♩','♪','♫','♬','♭','♮','♯',
  '🎸','🎹','🎺','🎻','🥁','🎷','🪗','🪘','🪕','🪈',
  '🎤','🎧','🎙','🎛','🎚','📻','💿','📀',
  '🎭','🎬','🎪','📣','🔔',
  '⭐','🔥','💎','👑','🌟','✨','💫',
  '💰','🛠','💼','📋','🎁','👥','🤝','✅','🆕','📍',
];
function insertEmoji(idx) {
  const emoji = EMOJIS[idx];
  const inp = document.getElementById('a-name');
  const start = inp.selectionStart ?? inp.value.length;
  const end   = inp.selectionEnd   ?? inp.value.length;
  inp.value = inp.value.slice(0, start) + emoji + inp.value.slice(end);
  const pos = start + emoji.length;
  inp.setSelectionRange(pos, pos);
  inp.focus();
  autoSlug('a-name', 'a-slug');
}
function initEmojiPicker() {
  document.getElementById('emoji-picker').innerHTML =
    EMOJIS.map((e, i) =>
      `<button type="button" class="emoji-btn" onclick="insertEmoji(${i})" title="${e}">${e}</button>`
    ).join('');
}

// ── add modal ──
function openAdd(parentId, section) {
  addData = {parentId, section, mode:'add'};
  document.getElementById('add-title').textContent = parentId ? T('new_subcat') : T('new_cat');
  document.getElementById('a-name').value = '';
  document.getElementById('a-slug').value = '';
  document.getElementById('add-err').textContent = '';
  fillParentSel('a-parent', section, parentId || ROOT_IDS[section]);
  initEmojiPicker();
  openOverlay('modal-add');
  setTimeout(()=>document.getElementById('a-name').focus(),60);
}
function openEdit(id, section) {
  const cat = allCats.find(c => c.id === id);
  if (!cat) return;
  addData = {id, section, mode:'edit'};
  document.getElementById('add-title').textContent = T('edit_cat');
  document.getElementById('a-name').value = cat.name || '';
  document.getElementById('a-slug').value = cat.slug || '';
  document.getElementById('add-err').textContent = '';
  fillParentSel('a-parent', section, cat.parent_id);
  initEmojiPicker();
  openOverlay('modal-add');
  setTimeout(()=>document.getElementById('a-name').focus(),60);
}
async function submitAdd() {
  const name = document.getElementById('a-name').value.trim();
  const slug = document.getElementById('a-slug').value.trim();
  const parentId = parseInt(document.getElementById('a-parent').value);
  const errEl = document.getElementById('add-err');
  errEl.textContent='';
  if (!name){errEl.textContent=T('err_name');return}
  if (!slug){errEl.textContent=T('err_slug');return}
  let r;
  if (addData.mode === 'edit') {
    r = await fetch(`/api/categories/${addData.id}`,{
      method:'PATCH',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,slug,parent_id:parentId})
    });
  } else {
    r = await fetch('/api/categories',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name,slug,parent_id:parentId})
    });
  }
  if (!r.ok){errEl.textContent=(await r.json()).detail||'Error';return}
  closeAll(); await loadAll(); await loadTree(addData.section);
}

// ── move modal ──
function openMove(id, section) {
  moveTargetId = id; moveSection = section;
  document.getElementById('move-err').textContent='';
  fillParentSel('m-parent', section, null);
  openOverlay('modal-move');
}
async function submitMove() {
  const parentId = parseInt(document.getElementById('m-parent').value);
  const errEl = document.getElementById('move-err');
  errEl.textContent='';
  const r = await fetch(`/api/categories/${moveTargetId}`,{
    method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({parent_id:parentId})
  });
  if (!r.ok){errEl.textContent=(await r.json()).detail||'Error';return}
  closeAll(); await loadAll(); await loadTree(moveSection);
}

// ── fields editor ──
let _editFields = [];

async function openFields(id, section) {
  fieldsTargetId = id; fieldsSection = section;
  const cat = allCats.find(c=>c.id===id);
  document.getElementById('fields-title').textContent = T('fields_hdr') + (cat?cat.name:'');
  document.getElementById('fields-err').textContent='';
  const raw = await (await fetch(`/api/categories/${id}/fields`)).json();
  const meta = raw.find(f=>f.type==='__meta'&&f.key==='allow_extra_categories');
  document.getElementById('f-extra').checked = !!(meta && meta.value);
  _editFields = raw.filter(f=>f.type!=='__meta');
  renderFieldsList(_editFields);
  const srcSel = document.getElementById('f-copy-src');
  const rootId = ROOT_IDS[section];
  const eligible = allCats.filter(c=>c.id!==id && isUnder(c.id,rootId));
  srcSel.innerHTML=`<option value="">${T('copy_from')}</option>`+
    eligible.map(c=>`<option value="${c.id}">${esc(c.name)}</option>`).join('');
  openOverlay('modal-fields');
}

function isUnder(id, rootId) {
  let cur = allCats.find(c=>c.id===id);
  let guard=0;
  while(cur && guard<15){
    if(cur.id===rootId) return true;
    cur = allCats.find(c=>c.id===cur.parent_id);
    guard++;
  }
  return false;
}

function renderFieldsList(fields) {
  const el = document.getElementById('fields-list');
  if (!fields.length) { el.innerHTML=`<div class="empty">${T('no_fields')}</div>`; return; }
  el.innerHTML = fields.map((f,i)=>`
    <div class="field-item" data-idx="${i}">
      <div class="fi-ord">
        <button onclick="moveField(${i},-1)">▲</button>
        <button onclick="moveField(${i},1)">▼</button>
      </div>
      <input class="fi-label" placeholder="Label" value="${esc(f.label||'')}" onchange="updateField(${i},'label',this.value)">
      <input class="fi-key"   placeholder="key"   value="${esc(f.key||'')}"   onchange="updateField(${i},'key',this.value)">
      <select class="fi-type" onchange="updateField(${i},'type',this.value)">
        ${['text','number','checkbox','video'].map(t=>`<option value="${t}" ${f.type===t?'selected':''}>${t}</option>`).join('')}
      </select>
      <input type="checkbox" class="fi-req" title="Required" ${f.required?'checked':''} onchange="updateField(${i},'required',this.checked)">
      <button class="fi-del" onclick="removeField(${i})">✕</button>
    </div>`).join('');
}

function updateField(idx, key, val) { if(_editFields[idx]) _editFields[idx][key]=val; }
function moveField(idx, dir) {
  const newIdx = idx+dir;
  if(newIdx<0||newIdx>=_editFields.length) return;
  [_editFields[idx],_editFields[newIdx]]=[_editFields[newIdx],_editFields[idx]];
  renderFieldsList(_editFields);
}
function removeField(idx) {
  _editFields.splice(idx,1);
  renderFieldsList(_editFields);
}
function addFieldRow() {
  _editFields.push({type:'text',label:'',key:'',required:false});
  renderFieldsList(_editFields);
  const list = document.getElementById('fields-list');
  list.lastElementChild && list.lastElementChild.scrollIntoView({behavior:'smooth'});
}

async function copyFields() {
  const srcId = parseInt(document.getElementById('f-copy-src').value);
  if(!srcId) return;
  const raw = await (await fetch(`/api/categories/${srcId}/fields`)).json();
  const imported = raw.filter(f=>f.type!=='__meta');
  const existKeys = new Set(_editFields.map(f=>f.key));
  imported.forEach(f=>{ if(!existKeys.has(f.key)){ _editFields.push({...f}); existKeys.add(f.key); }});
  renderFieldsList(_editFields);
}

async function saveFields() {
  const errEl = document.getElementById('fields-err');
  errEl.textContent='';
  for(const f of _editFields){
    if(!f.key||!f.type){errEl.textContent=T('err_fields');return}
  }
  const result = [];
  if(document.getElementById('f-extra').checked){
    result.push({type:'__meta',key:'allow_extra_categories',value:true});
  }
  result.push(..._editFields);
  const r = await fetch(`/api/categories/${fieldsTargetId}/fields`,{
    method:'PUT',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({fields:result})
  });
  if(!r.ok){errEl.textContent=(await r.json()).detail||'Error';return}
  closeAll(); await loadTree(fieldsSection);
}

// ── batch field ──
function openBatchField(section) {
  batchSection = section;
  document.getElementById('bf-label').value='';
  document.getElementById('bf-key').value='';
  document.getElementById('bf-type').value='text';
  document.getElementById('bf-req').checked=false;
  document.getElementById('batch-err').textContent='';
  openOverlay('modal-batch');
}
async function submitBatch() {
  const label=document.getElementById('bf-label').value.trim();
  const key=document.getElementById('bf-key').value.trim();
  const type=document.getElementById('bf-type').value;
  const required=document.getElementById('bf-req').checked;
  const errEl=document.getElementById('batch-err');
  errEl.textContent='';
  if(!label){errEl.textContent=T('err_label');return}
  if(!key){errEl.textContent=T('err_key');return}
  const r=await fetch('/api/categories/batch/add_field',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ids:[...selected],field:{type,label,key,required}})
  });
  if(!r.ok){errEl.textContent=(await r.json()).detail||'Error';return}
  closeAll(); clearSelection(); await loadTree(batchSection);
}

// ── delete ──
async function deleteCat(id, section, name) {
  if(!confirm(T('del_confirm').replace('{name}',name))) return;
  const r=await fetch(`/api/categories/${id}`,{method:'DELETE'});
  if(!r.ok){alert((await r.json()).detail||'Error');return}
  await loadAll(); await loadTree(section);
}

// ── slug auto ──
function autoSlug(srcId, dstId) {
  const name=document.getElementById(srcId).value;
  const slug=name.toLowerCase()
    .replace(/[а-яё]/g,c=>({а:'a',б:'b',в:'v',г:'g',д:'d',е:'e',ё:'yo',ж:'zh',з:'z',и:'i',й:'y',к:'k',л:'l',м:'m',н:'n',о:'o',п:'p',р:'r',с:'s',т:'t',у:'u',ф:'f',х:'h',ц:'ts',ч:'ch',ш:'sh',щ:'sch',ъ:'',ы:'y',ь:'',э:'e',ю:'yu',я:'ya'}[c]||c))
    .replace(/[^a-z0-9]/g,'_').replace(/_+/g,'_').replace(/^_|_$/g,'');
  document.getElementById(dstId).value=slug;
}

// ── parent select ──
function fillParentSel(selId, section, selected) {
  const sel=document.getElementById(selId);
  const rootId=ROOT_IDS[section];
  const name={market:T('tab_market'),services:T('tab_services'),vacancy:T('tab_vacancy')}[section];
  const opts=allCats.filter(c=>isUnder(c.id,rootId)||c.id===rootId);
  sel.innerHTML=`<option value="${rootId}">[${name}] ${T('top_level')}</option>`+
    opts.filter(c=>c.id!==rootId).map(c=>{
      const d=getDepth(c.id,rootId);
      return `<option value="${c.id}" ${c.id==selected?'selected':''}>${'  '.repeat(d)}${esc(c.name)}</option>`;
    }).join('');
}
function getDepth(id, rootId) {
  let d=0,cur=allCats.find(c=>c.id===id);
  while(cur&&cur.id!==rootId&&cur.parent_id){cur=allCats.find(c=>c.id===cur.parent_id);d++;if(d>12)break}
  return d;
}

// ── overlay helpers ──
function openOverlay(id){ document.getElementById(id).classList.add('open') }
function closeAll(){
  document.querySelectorAll('.overlay').forEach(o=>o.classList.remove('open'));
}
document.querySelectorAll('.overlay').forEach(o=>{
  o.addEventListener('click',e=>{ if(e.target===o) closeAll(); });
});

// ── tabs ──
const ALL_TABS = ['market','services','vacancy','analytics','catalog'];
function switchTab(section) {
  currentTab=section;
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active', ALL_TABS[i]===section));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById(`panel-${section}`).classList.add('active');
  if (section==='analytics') loadAnalytics();
  else if (section==='catalog') catalogLoad(0);
  else loadTree(section);
}

// ── Catalog tree browser ──
const SECTION_ICONS = {market:'🛒', service:'🎵', vacancy:'💼', events:'🎭'};
const SECTION_NAMES_JS = {market:'Барахолка', service:'Услуги', vacancy:'Вакансии', events:'Афиша'};
// breadcrumb stack: [{label, action}]
let _catStack = [];

async function catalogLoad() {
  _catStack = [];
  await catalogShowSections();
}

function catalogSetBreadcrumb() {
  const el = document.getElementById('catalog-breadcrumb');
  if (!_catStack.length) { el.innerHTML = ''; return; }
  const parts = _catStack.map((item, i) => {
    if (i === _catStack.length - 1)
      return `<span class="bc-item current">${esc(item.label)}</span>`;
    return `<span class="bc-item" onclick="catalogNavTo(${i})">${esc(item.label)}</span>`;
  });
  el.innerHTML = `<button class="bc-back" onclick="catalogNavTo(${_catStack.length-2})">← Назад</button>
    ${parts.join('<span class="bc-sep"> › </span>')}`;
}

async function catalogNavTo(idx) {
  if (idx < 0) { _catStack = []; await catalogShowSections(); return; }
  _catStack = _catStack.slice(0, idx+1);
  await _catStack[idx].reload();
}

async function catalogShowSections() {
  const el = document.getElementById('catalog-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  catalogSetBreadcrumb();
  const rows = await fetch('/api/catalog/sections').then(r=>r.json());
  el.innerHTML = `<div class="cat-tree-list">${rows.map(s => `
    <div class="cat-tree-row" onclick="catalogOpenSection('${s.type}', '${esc(s.name)}', ${s.root_id})">
      <span class="cat-tree-icon">${SECTION_ICONS[s.type]||'📦'}</span>
      <span class="cat-tree-name">${esc(s.name)}</span>
      <span class="cat-tree-subcount" style="opacity:.3">—</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillListings(0,'${s.type}','${esc(s.name)}')"><b>${s.listings}</b> объявл.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(0,'${s.type}','${esc(s.name)}','open')"><b>${s.views}</b> просм.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillUsers(0,'${s.type}','${esc(s.name)}')"><b>${s.viewers}</b> польз.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(0,'${s.type}','${esc(s.name)}','contact')"><b>${s.contacts}</b> контакт.</span>
      <span class="cat-tree-arrow">›</span>
    </div>`).join('')}</div>`;
}

async function catalogOpenSection(stype, sname, rootId) {
  _catStack.push({label: sname, reload: () => catalogShowCats(rootId, stype)});
  await catalogShowCats(rootId, stype);
}

async function catalogShowCats(parentId, stype) {
  const el = document.getElementById('catalog-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  catalogSetBreadcrumb();
  const data = await fetch(`/api/catalog/cats/${parentId}`).then(r=>r.json());
  if (!data.rows || !data.rows.length) {
    // leaf: show listings directly
    await catalogShowListings(parentId, stype);
    return;
  }
  const hasSubcats = data.rows.some(r => r.subcats > 0);
  el.innerHTML = `<div class="cat-tree-list">${data.rows.map(r => `
    <div class="cat-tree-row" onclick="catalogOpenCat(${r.id}, '${esc(r.name)}', '${stype||''}')">
      <span class="cat-tree-icon">📁</span>
      <span class="cat-tree-name">${esc(r.name)}</span>
      <span class="cat-tree-subcount">${r.subcats ? `<span>${r.subcats} подкат.</span>` : '<span style="opacity:.2">—</span>'}</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillListings(${r.id},0,'${esc(r.name)}')"><b>${r.listings}</b> объявл.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(${r.id},0,'${esc(r.name)}','open')"><b>${r.views}</b> просм.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillUsers(${r.id},0,'${esc(r.name)}')"><b>${r.viewers}</b> польз.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(${r.id},0,'${esc(r.name)}','contact')"><b>${r.contacts||0}</b> контакт.</span>
      <span class="cat-tree-arrow">›</span>
    </div>`).join('')}</div>`;
}

async function catalogOpenCat(catId, catName, stype) {
  async function showCatContent() {
    const data = await fetch(`/api/catalog/cats/${catId}`).then(r=>r.json());
    if (data.rows && data.rows.length) {
      await catalogShowCats(catId, stype);
    } else {
      await catalogShowListings(catId, stype);
    }
  }
  _catStack.push({label: catName, reload: showCatContent});
  await showCatContent();
}

const CAT_LISTING_LIMIT = 24;
let _catListingOffset = 0;

async function catalogShowListings(catId, stype, offset=0) {
  _catListingOffset = offset;
  const el = document.getElementById('catalog-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  catalogSetBreadcrumb();
  const params = new URLSearchParams({category_id: catId, offset, limit: CAT_LISTING_LIMIT});
  const data = await fetch(`/api/listings?${params}`).then(r=>r.json());
  if (!data.rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Нет объявлений в этой категории</div>';
    return;
  }
  const totalPages = Math.ceil(data.total / CAT_LISTING_LIMIT);
  const curPage = Math.floor(offset / CAT_LISTING_LIMIT) + 1;
  const pag = totalPages > 1 ? `<div class="an-pagination" style="margin-top:16px">
    <button onclick="catalogShowListings(${catId},'${stype}',${Math.max(0,offset-CAT_LISTING_LIMIT)})" ${offset===0?'disabled':''}>◀</button>
    <span>${curPage} / ${totalPages}</span>
    <button onclick="catalogShowListings(${catId},'${stype}',${offset+CAT_LISTING_LIMIT})" ${offset+CAT_LISTING_LIMIT>=data.total?'disabled':''}>▶</button>
  </div>` : '';
  el.innerHTML = `<div style="font-size:12px;color:#556;margin-bottom:10px">${data.total} объявлений</div>
    <div class="catalog-listings-grid">${data.rows.map(r=>catCardHtml(r)).join('')}</div>${pag}`;
}

function catCardHtml(r) {
  const firstPhoto = (r.photo_ids||[])[0];
  const photoCount = (r.photo_ids||[]).length;
  const hasVideo = !!r.video_type;
  let mediaHtml;
  if (firstPhoto) {
    mediaHtml = `<img src="/api/tg_photo/${encodeURIComponent(firstPhoto)}"
      onerror="this.parentElement.innerHTML='<div class=\'cat-card-media-empty\'>📋</div>'" alt="">`;
  } else if (r.video_type === 'youtube' && r.video_id) {
    mediaHtml = `<img src="https://img.youtube.com/vi/${r.video_id}/mqdefault.jpg"
      onerror="this.parentElement.innerHTML='<div class=\'cat-card-media-empty\'>▶️</div>'" alt="">`;
  } else {
    mediaHtml = `<div class="cat-card-media-empty">${hasVideo?'▶️':'📋'}</div>`;
  }
  const fmtDate = s => s ? s.slice(5).replace('-','.') : '';
  return `<div class="cat-card" onclick="openListing(${r.id})">
    <div class="cat-card-media">
      ${mediaHtml}
      ${hasVideo ? `<span class="cat-card-video-badge">${r.video_type==='youtube'?'▶ YouTube':'▶ Видео'}</span>` : ''}
      ${r.is_sold ? '<span class="cat-card-sold">продано</span>' : ''}
      ${photoCount > 1 ? `<span class="cat-card-photos-count">📷 ${photoCount}</span>` : ''}
    </div>
    <div class="cat-card-body">
      <div class="cat-card-title">${esc(r.title||'Без названия')}</div>
      ${r.price ? `<div class="cat-card-price">${esc(r.price)}</div>` : ''}
      <div class="cat-card-meta">${r.city?esc(r.city):''}${r.category?' · '+esc(r.category):''}</div>
      <div class="cat-card-stats">👁 ${r.opens} · ${fmtDate(r.created_at)}</div>
    </div>
  </div>`;
}

// ── analytics ──
// ── Analytics helpers ──
function pct(a,b){ return b ? Math.round(a/b*100)+'%' : '—'; }
function pctF(a,b){ return b ? (a/b*100).toFixed(1)+'%' : '—'; }
function trend(cur, prev) {
  const diff = cur - prev;
  if (!prev) return cur>0 ? `<span class="an-up">▲${cur}</span>` : `<span>${cur}</span>`;
  if (diff>0) return `<span class="an-up">▲${diff}</span>`;
  if (diff<0) return `<span class="an-down">▼${Math.abs(diff)}</span>`;
  return `<span style="color:#556">→0</span>`;
}
const SECTION_LABELS = {market:'Барахолка',services:'Услуги',service:'Услуги',
  vacancy:'Вакансии',vacancies:'Вакансии',events:'Афиша',afisha:'Афиша','':(v)=>v||'—'};
function sectionName(s){ return SECTION_LABELS[s] || s || '—'; }
const SOURCE_LABELS = {search:'поиск',catalog:'каталог',my:'мои объявления',
  calendar:'календарь',calendar_city:'календарь/город',direct:'прямой переход','':(v)=>v||'—'};
function sourceName(s){ return SOURCE_LABELS[s] || s || '—'; }

// current analytics sub-section
let _anSection = 'overview';
let _ownersOffset = 0;
const OWNERS_LIMIT = 15;

function anSwitch(section, btn) {
  _anSection = section;
  document.querySelectorAll('.an-nav-btn').forEach(b=>b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  anLoad(section);
}

function anReload() {
  anLoad(_anSection);
  document.getElementById('an-updated').textContent = new Date().toLocaleTimeString();
}

async function anLoad(section) {
  const el = document.getElementById('an-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  try {
    if (section === 'overview')         await anOverview(el);
    else if (section === 'growth')      await anGrowth(el);
    else if (section === 'sections')    await anSections(el);
    else if (section === 'top_searches') await anTopSearches(el);
    else if (section === 'no_results')  await anNoResults(el);
    else if (section === 'search_quality') await anSearchQuality(el);
    else if (section === 'top_cards')   await anTopCards(el);
    else if (section === 'sources')     await anSources(el);
    else if (section === 'search_conversion') await anSearchConversion(el);
    else if (section === 'owners')      await anOwners(el, _ownersOffset);
    else if (section === 'cities')      await anCities(el);
  } catch(e) {
    el.innerHTML = `<div class="empty" style="padding:20px;color:#f66">Ошибка: ${esc(e.message)}</div>`;
  }
}

// ── Overview ──
async function anOverview(el) {
  const [ov, daily, cats] = await Promise.all([
    fetch('/api/analytics/overview').then(r=>r.json()),
    fetch('/api/analytics/daily').then(r=>r.json()),
    fetch('/api/analytics/top_categories').then(r=>r.json()),
  ]);
  const u=ov.users, l=ov.listings, s=ov.search, v=ov.views;
  el.innerHTML = `
    <div class="analytics-cards">
      <div class="an-card">
        <div class="an-card-label">👥 Пользователи</div>
        <div class="an-card-value">${u.total.toLocaleString()}</div>
        <div class="an-card-sub">DAU ${u.dau} · WAU ${u.wau} · MAU ${u.mau}</div>
        <div class="an-card-sub">За неделю: ${trend(u.new_week, u.new_prev)}</div>
      </div>
      <div class="an-card">
        <div class="an-card-label">📋 Активных объявлений</div>
        <div class="an-card-value">${l.active.toLocaleString()}</div>
        <div class="an-card-sub">Всего: ${l.total}</div>
        <div class="an-card-sub">За неделю: ${trend(l.new_week, l.new_prev)}</div>
      </div>
      <div class="an-card">
        <div class="an-card-label">🔎 Поисковых запросов</div>
        <div class="an-card-value">${s.total.toLocaleString()}</div>
        <div class="an-card-sub">Без результата: ${s.no_result} (${pct(s.no_result, s.total)})</div>
      </div>
      <div class="an-card">
        <div class="an-card-label">👁 Просмотров карточек</div>
        <div class="an-card-value">${v.opens.toLocaleString()}</div>
        <div class="an-card-sub">Написали: ${v.contacts} (${pct(v.contacts, v.opens)})</div>
      </div>
    </div>
    <div class="analytics-charts">
      <div class="an-chart-box">
        <div class="an-chart-title">Новые пользователи · 30 дней</div>
        <div class="chart-wrap">
          <div class="chart-y-labels" id="chart-users-y"></div>
          <div class="bar-chart" id="chart-users"></div>
          <div class="chart-x-labels" id="chart-users-x"></div>
        </div>
      </div>
      <div class="an-chart-box">
        <div class="an-chart-title">Новые объявления · 30 дней</div>
        <div class="chart-wrap">
          <div class="chart-y-labels" id="chart-listings-y"></div>
          <div class="bar-chart" id="chart-listings"></div>
          <div class="chart-x-labels" id="chart-listings-x"></div>
        </div>
      </div>
    </div>
    <div class="analytics-tables">
      <div class="an-table-box">
        <div class="an-table-title">Топ категорий</div>
        <div id="tbl-categories"></div>
      </div>
    </div>`;
  renderBarChart('chart-users', daily.users, '#5a9cf5');
  renderBarChart('chart-listings', daily.listings, '#6ef5aa');
  const tcEl = document.getElementById('tbl-categories');
  if (!cats.length) { tcEl.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  tcEl.innerHTML = `<table class="an-table"><thead><tr>
    <th>Категория</th><th>Объявл.</th><th>Просм.</th><th>Контакт</th>
  </tr></thead><tbody>${cats.map(c=>`<tr>
    <td>${esc(c.name)}</td><td>${c.listings}</td><td>${c.opens}</td>
    <td>${c.contacts}${c.opens?' ('+pct(c.contacts,c.opens)+')':''}</td>
  </tr>`).join('')}</tbody></table>`;
}

function renderBarChart(id, data, color) {
  const el = document.getElementById(id);
  const yEl = document.getElementById(id+'-y');
  const xEl = document.getElementById(id+'-x');
  if (!el) return;
  if (!data||!data.length){el.innerHTML=`<div class="empty">Нет данных</div>`;return;}
  const max = Math.max(...data.map(d=>d.count), 1);
  el.innerHTML = data.map(d => {
    const h = Math.max(2, Math.round(d.count/max*100));
    return `<div class="bar-wrap" data-tip="${d.date}: ${d.count}"><div class="bar-fill" style="height:${h}%;background:${color}"></div></div>`;
  }).join('');
  if (yEl) {
    const mid = Math.round(max/2);
    yEl.innerHTML = `<span class="chart-y-label">${max}</span><span class="chart-y-label">${mid}</span><span class="chart-y-label">0</span>`;
  }
  if (xEl && data.length) {
    xEl.innerHTML = `<span>${data[0].date}</span><span>${data[Math.floor(data.length/2)].date}</span><span>${data[data.length-1].date}</span>`;
  }
}

// ── Growth ──
async function anGrowth(el) {
  const d = await fetch('/api/analytics/growth').then(r=>r.json());
  const fmtDay = iso => iso.slice(5).replace('-','.');
  const rowsU = d.days.map((day,i)=>`<div class="growth-day"><span class="day">${fmtDay(day)}</span><span class="val">${d.users_by_day[i]}</span></div>`).join('');
  const rowsL = d.days.map((day,i)=>`<div class="growth-day"><span class="day">${fmtDay(day)}</span><span class="val">${d.listings_by_day[i]}</span></div>`).join('');
  el.innerHTML = `<div class="growth-grid">
    <div class="growth-box">
      <h3>👥 Новые пользователи · 7 дней</h3>
      ${rowsU}
      <div class="growth-total">За неделю: +${d.new_users_week} · предыдущая: +${d.new_users_prev}</div>
    </div>
    <div class="growth-box">
      <h3>📋 Новые объявления · 7 дней</h3>
      ${rowsL}
      <div class="growth-total">За неделю: +${d.new_listings_week} · предыдущая: +${d.new_listings_prev}</div>
    </div>
  </div>`;
}

// ── By sections ──
async function anSections(el) {
  const rows = await fetch('/api/analytics/sections').then(r=>r.json());
  if (!rows.length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  el.innerHTML = rows.map(r=>`
    <div class="section-block">
      <div class="section-name">${esc(sectionName(r.section))} <span style="font-size:11px;color:#556;font-weight:400">${esc(r.section||'')}</span></div>
      <div class="section-stats">
        <span>🔎 Поисков: <b>${r.searches}</b></span>
        <span>❌ Пустых: <b>${r.no_results}</b> (${pct(r.no_results,r.searches)})</span>
        <span>👤 Искали: <b>${r.search_users}</b></span>
        <span>👁 Открытий: <b>${r.opens}</b></span>
        <span>🙋 Зрителей: <b>${r.open_users}</b></span>
        <span>🔍 Из поиска: <b>${r.search_opens}</b></span>
        <span>📂 Из каталога: <b>${r.catalog_opens}</b></span>
      </div>
    </div>`).join('');
}

// ── Top searches ──
async function anTopSearches(el) {
  const rows = await fetch('/api/analytics/top_searches').then(r=>r.json());
  if (!rows.length) { el.innerHTML = '<div class="empty">Нет запросов</div>'; return; }
  el.innerHTML = `<div class="an-table-box"><table class="an-table"><thead><tr>
    <th>#</th><th>Запрос</th><th>Раздел</th><th>Сколько раз</th><th>Успешных</th>
  </tr></thead><tbody>${rows.map((r,i)=>`<tr>
    <td style="color:#556">${i+1}</td>
    <td><b>${esc(r.query||'—')}</b></td>
    <td style="color:#778">${esc(r.section||'—')}</td>
    <td>${r.count}</td>
    <td>${pct(r.with_results,r.count)}</td>
  </tr>`).join('')}</tbody></table></div>`;
}

// ── Empty searches ──
async function anNoResults(el) {
  const rows = await fetch('/api/analytics/no_results').then(r=>r.json());
  if (!rows.length) { el.innerHTML = '<div class="empty" style="padding:20px">Пустых поисков нет — отличный результат!</div>'; return; }
  el.innerHTML = `<div style="font-size:12px;color:#778;margin-bottom:10px">Запросы, по которым бот не нашёл ни одного объявления (results_count = 0).</div>
    <div class="an-table-box"><table class="an-table"><thead><tr>
    <th>#</th><th>Запрос</th><th>Раздел</th><th>Раз</th>
  </tr></thead><tbody>${rows.map((r,i)=>`<tr>
    <td style="color:#556">${i+1}</td>
    <td><b>${esc(r.query||'—')}</b></td>
    <td style="color:#778">${esc(r.section||'—')}</td>
    <td>${r.count}</td>
  </tr>`).join('')}</tbody></table></div>`;
}

// ── Search quality / match_mode ──
async function anSearchQuality(el) {
  const d = await fetch('/api/analytics/search_quality').then(r=>r.json());
  const modesHtml = d.modes.map(m=>{
    const w = d.total ? Math.round(m.count/d.total*100) : 0;
    return `<div class="sq-bar-row">
      <span class="sq-mode">${esc(m.mode)}</span>
      <div class="sq-bar"><div class="sq-bar-fill" style="width:${w}%"></div></div>
      <span class="sq-count">${m.count} (${pct(m.count,d.total)})</span>
    </div>`;
  }).join('');
  el.innerHTML = `
    <div style="display:flex;gap:20px;margin-bottom:16px">
      <div class="an-card" style="flex:1"><div class="an-card-label">Всего поисков</div><div class="an-card-value">${d.total}</div></div>
      <div class="an-card" style="flex:1"><div class="an-card-label">Без результатов</div><div class="an-card-value">${d.no_results}</div><div class="an-card-sub">${pct(d.no_results,d.total)}</div></div>
    </div>
    <div class="an-table-box" style="padding:14px">
      <div class="an-table-title">Распределение по типу match_mode</div>
      ${modesHtml || '<div class="empty">Нет данных</div>'}
    </div>`;
}

// ── Top cards (listings) ──
function cardRowHtml(r, idx) {
  const firstPhoto = (r.photo_file_id||r.photo||'').split(',')[0].trim() || (r.photo_ids||[])[0] || '';
  const thumbHtml = firstPhoto
    ? `<img class="card-thumb" style="cursor:pointer" src="/api/tg_photo/${encodeURIComponent(firstPhoto)}"
         onclick="openListing(${r.id})" title="Открыть"
         onerror="this.outerHTML='<div class=\'card-thumb-empty\' style=\'cursor:pointer\' onclick=\'openListing(${r.id})\'>📋</div>'">`
    : `<div class="card-thumb-empty" style="cursor:pointer" onclick="openListing(${r.id})">📋</div>`;
  const opens = r.opens||0, viewers = r.users||r.viewers||0;
  return `<div class="card-row" onclick="openListing(${r.id})" style="cursor:pointer">
    ${thumbHtml}
    <div class="card-info">
      <div class="card-title">${idx!=null?idx+'. ':''}${esc(r.title)}</div>
      <div class="card-meta">${esc(sectionName(r.section||r.type||''))}${r.price?' · '+esc(r.price):''}${r.contact?' · '+esc(r.contact):''} ${r.is_sold?'· <span style="color:#f88">продано</span>':''}</div>
      <div class="card-meta" style="margin-top:2px">🔍 ${r.search_opens||0} · 📂 ${r.catalog_opens||0}</div>
    </div>
    <div class="card-stats" style="text-align:right;white-space:nowrap">
      <b style="font-size:16px">${opens}</b> откр. · ${viewers} чел.
    </div>
  </div>`;
}

async function anTopCards(el) {
  const rows = await fetch('/api/analytics/top_cards').then(r=>r.json());
  if (!rows.length) { el.innerHTML = '<div class="empty">Нет открытий карточек</div>'; return; }
  el.innerHTML = `<div class="card-grid">${rows.map((r,i)=>cardRowHtml(r, i+1)).join('')}</div>`;
}

// ── Sources ──
async function anSources(el) {
  const rows = await fetch('/api/analytics/sources').then(r=>r.json());
  if (!rows.length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const sections = [...new Set(rows.map(r=>r.section))];
  el.innerHTML = sections.map(sec=>{
    const secRows = rows.filter(r=>r.section===sec);
    const totalOpens = secRows.reduce((s,r)=>s+r.opens,0);
    return `<div class="section-block">
      <div class="section-name">${esc(sectionName(sec))}</div>
      <table class="an-table"><thead><tr><th>Источник</th><th>Открытий</th><th>Доля</th><th>Пользователей</th></tr></thead>
      <tbody>${secRows.map(r=>`<tr>
        <td>${esc(sourceName(r.source))}</td><td>${r.opens}</td><td>${pct(r.opens,totalOpens)}</td><td>${r.users}</td>
      </tr>`).join('')}</tbody></table>
    </div>`;
  }).join('');
}

// ── Search → open ──
async function anSearchConversion(el) {
  const rows = await fetch('/api/analytics/search_conversion').then(r=>r.json());
  if (!rows.length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  const totalS = rows.reduce((s,r)=>s+r.searches,0);
  const totalO = rows.reduce((s,r)=>s+r.opens,0);
  el.innerHTML = `
    <div style="font-size:13px;color:#778;margin-bottom:12px">
      Условная конверсия поиска в открытие карточки (не точная воронка, но показывает эффективность поиска).
    </div>
    <div style="display:flex;gap:16px;margin-bottom:16px">
      <div class="an-card" style="flex:1"><div class="an-card-label">Всего поисков</div><div class="an-card-value">${totalS}</div></div>
      <div class="an-card" style="flex:1"><div class="an-card-label">Открытий из поиска</div><div class="an-card-value">${totalO}</div></div>
      <div class="an-card" style="flex:1"><div class="an-card-label">Общая конверсия</div><div class="an-card-value">${pctF(totalO,totalS)}</div></div>
    </div>
    <div class="an-table-box"><table class="an-table"><thead><tr>
      <th>Раздел</th><th>Поисков</th><th>Открытий из поиска</th><th>Конверсия</th>
    </tr></thead><tbody>${rows.map(r=>`<tr>
      <td>${esc(sectionName(r.section))}</td><td>${r.searches}</td><td>${r.opens}</td><td>${pctF(r.opens,r.searches)}</td>
    </tr>`).join('')}</tbody></table></div>`;
}

// ── Owners ──
let _ownerNavStack = [];  // {section:'owners'|'owner', ownerId, offset}

async function anOwners(el, offset=0) {
  _ownersOffset = offset;
  const d = await fetch(`/api/analytics/owners?offset=${offset}&limit=${OWNERS_LIMIT}`).then(r=>r.json());
  const totalPages = Math.max(1, Math.ceil(d.total/OWNERS_LIMIT));
  const curPage = Math.floor(offset/OWNERS_LIMIT)+1;
  el.innerHTML = `
    <div style="margin-bottom:10px;font-size:13px;color:#778">Всего авторов: <b style="color:#9bc">${d.total}</b></div>
    ${d.rows.map(r=>{
      const contact = (r.contacts_raw||'').split(',').map(s=>s.trim()).filter(Boolean)[0] || '—';
      return `<div class="owner-row" onclick="anOwnerDetail(${r.owner_id})">
        <div>
          <div class="owner-contact">${esc(contact)}</div>
          <div class="owner-stats">${r.listings} объявл. · ${r.active} актив. · ${r.opens} открытий</div>
        </div>
        <div style="font-size:13px;color:#9bc">›</div>
      </div>`;
    }).join('')}
    <div class="an-pagination">
      <button onclick="anOwners(document.getElementById('an-content'),${Math.max(0,offset-OWNERS_LIMIT)})" ${offset===0?'disabled':''}>◀</button>
      <span>${curPage} / ${totalPages}</span>
      <button onclick="anOwners(document.getElementById('an-content'),${offset+OWNERS_LIMIT})" ${offset+OWNERS_LIMIT>=d.total?'disabled':''}>▶</button>
    </div>`;
}

async function anOwnerDetail(ownerId) {
  const el = document.getElementById('an-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const d = await fetch(`/api/analytics/owner/${ownerId}`).then(r=>r.json());
  const o = d.owner;
  const contact = (o.contacts_raw||'').split(',').map(s=>s.trim()).filter(Boolean)[0] || '—';
  const listingsHtml = `<div class="card-grid">${d.listings.map((l,i)=>cardRowHtml({...l, search_opens:0, catalog_opens:0}, i+1+d.listing_offset)).join('')}</div>`;
  el.innerHTML = `
    <button class="back-btn" onclick="anOwners(document.getElementById('an-content'),${_ownersOffset})">← Все авторы</button>
    <div class="owner-detail">
      <h3>👤 ${esc(contact)}</h3>
      <div class="owner-detail-stats">
        <div>Всего объявлений: <b>${o.listings_count||0}</b></div>
        <div>Активных: <b>${o.active||0}</b></div>
        <div>Закрыто/продано: <b>${o.sold||0}</b></div>
        <div>Открытий всего: <b>${o.opens||0}</b></div>
        <div>Уникальных зрителей: <b>${o.unique_viewers||0}</b></div>
        <div>Из поиска: <b>${o.search_opens||0}</b></div>
        <div>Из каталога: <b>${o.catalog_opens||0}</b></div>
        <div>Ср. открытий/карточку: <b>${o.opens&&o.listings_count?(o.opens/o.listings_count).toFixed(1):'0'}</b></div>
      </div>
    </div>
    <div class="an-table-title" style="margin:12px 0 8px">Объявления (${d.total_listings})</div>
    ${listingsHtml || '<div class="empty">Нет объявлений</div>'}
    ${d.total_listings > d.listing_offset + 10 ? `<button class="back-btn" style="margin-top:10px" onclick="anOwnerPage(${ownerId},${d.listing_offset+10})">Ещё ▼</button>` : ''}`;
}

async function anOwnerPage(ownerId, offset) {
  const el = document.getElementById('an-content');
  const more = await fetch(`/api/analytics/owner/${ownerId}?listing_offset=${offset}`).then(r=>r.json());
  const btn = el.querySelector('button[onclick*="anOwnerPage"]');
  if (btn) btn.remove();
  const listingsHtml = more.listings.map((l,i)=>cardRowHtml({...l, photo_file_id:l.photo_file_id, search_opens:0, catalog_opens:0}, i+1+offset)).join('');
  el.insertAdjacentHTML('beforeend', listingsHtml);
  if (more.total_listings > offset + 10) {
    el.insertAdjacentHTML('beforeend', `<button class="back-btn" style="margin-top:10px" onclick="anOwnerPage(${ownerId},${offset+10})">Ещё ▼</button>`);
  }
}

// ── Listing detail modal ──
function closeListingModal() {
  const lc = document.getElementById('listing-content');
  lc.querySelectorAll('iframe').forEach(f => { f.src = ''; });
  lc.querySelectorAll('video').forEach(v => { v.pause(); v.src = ''; });
  document.getElementById('modal-listing').classList.remove('open');
}

async function openListing(listingId) {
  document.getElementById('listing-content').innerHTML = '<div class="empty" style="padding:40px;text-align:center">…</div>';
  openOverlay('modal-listing');
  try {
    const d = await fetch(`/api/listing/${listingId}`).then(r=>r.json());
    renderListingModal(d);
  } catch(e) {
    document.getElementById('listing-content').innerHTML = `<div class="empty" style="color:#f66">Ошибка: ${esc(e.message)}</div>`;
  }
}

function renderListingModal(d) {
  const photoIds = d.photo_ids || [];
  const photoUrls = photoIds.map(fid => `/api/tg_photo/${encodeURIComponent(fid.trim())}`);
  window._lbCurrentPhotos = photoUrls;
  const photosHtml = photoUrls.length
    ? `<div class="listing-photos">${photoUrls.map((url, i)=>`
        <div class="listing-photo">
          <img src="${url}" alt="фото"
               onclick="event.stopPropagation(); openLightbox(window._lbCurrentPhotos, ${i})"
               style="cursor:zoom-in"
               onerror="this.parentElement.innerHTML='<div class=\'listing-photo-empty\'>🖼</div>'">
        </div>`).join('')}</div>`
    : `<div class="listing-photos"><div class="listing-photo-empty" style="height:80px;font-size:32px;display:flex;align-items:center;justify-content:center;color:#334;background:#0d1628;border-radius:10px;width:100%">📋 Без фото</div></div>`;
  const videoHtml = d.video_type === 'youtube'
    ? `<div style="margin-bottom:14px;border-radius:10px;overflow:hidden;aspect-ratio:16/9">
        <iframe width="100%" height="100%" style="border:none;display:block"
          src="https://www.youtube.com/embed/${esc(d.video_id)}"
          allow="autoplay;encrypted-media;picture-in-picture" allowfullscreen></iframe>
      </div>`
    : d.video_type === 'telegram'
    ? `<div style="margin-bottom:14px">
        <video controls style="width:100%;border-radius:10px;background:#000;max-height:400px">
          <source src="/api/tg_photo/${encodeURIComponent(d.video_id)}">
          Ваш браузер не поддерживает видео.
        </video>
      </div>`
    : '';

  const fmtDate = s => {
    if (!s) return '—';
    const clean = s.replace('T',' ').split('+')[0].split('.')[0];
    const [date, time=''] = clean.split(' ');
    const [y,m,day] = (date||'').split('-');
    return y && m && day ? `${day}.${m}.${y} ${time.slice(0,5)}` : s;
  };

  const contact = d.contact
    ? (d.contact.startsWith('@')
        ? `<a href="https://t.me/${d.contact.slice(1)}" target="_blank">${esc(d.contact)}</a>`
        : esc(d.contact))
    : '—';

  window._currentListing = d;

  const flexFields = d.flex_fields || [];
  const flexData = d.flex || {};
  // Build flex display rows (skip video key which is shown as player)
  const flexRows = flexFields
    .map(f => {
      const val = flexData[f.key];
      if (val === undefined || val === null || val === '') return '';
      return `<span class="listing-meta-key">${esc(f.label)}</span><span class="listing-meta-val">${esc(String(val))}</span>`;
    })
    .filter(Boolean)
    .join('');

  // Блок афиши: дата/время/площадка из events_meta
  const ev = d.event;
  const eventRows = ev ? [
    ev.date  ? `<span class="listing-meta-key">📅 Дата</span><span class="listing-meta-val">${esc(ev.date)}</span>` : '',
    ev.time  ? `<span class="listing-meta-key">⏰ Время</span><span class="listing-meta-val">${esc(ev.time)}</span>` : '',
    ev.venue ? `<span class="listing-meta-key">📍 Площадка</span><span class="listing-meta-val">${esc(ev.venue)}</span>` : '',
    ev.city_text && !d.city ? `<span class="listing-meta-key">Город (текст)</span><span class="listing-meta-val">${esc(ev.city_text)}</span>` : '',
    ev.price_text ? `<span class="listing-meta-key">💲 Цена входа</span><span class="listing-meta-val">${esc(ev.price_text)}</span>` : '',
    ev.status && ev.status !== 'published' ? `<span class="listing-meta-key">Статус события</span><span class="listing-meta-val">${esc(ev.status)}</span>` : '',
  ].filter(Boolean).join('') : '';

  document.getElementById('listing-content').innerHTML = `
    ${d.is_sold ? '<div class="listing-sold-badge">✓ Закрыто / продано</div>' : ''}
    <div id="lm-photos">${photosHtml}</div>
    ${videoHtml}
    <div class="listing-title" id="lm-title">${esc(d.title||'Без названия')}</div>
    ${d.price ? `<div class="listing-price" id="lm-price">${esc(d.price)}</div>` : `<div class="listing-price" id="lm-price" style="display:none"></div>`}
    ${d.descr ? `<div class="listing-descr" id="lm-descr">${esc(d.descr)}</div>` : `<div class="listing-descr" id="lm-descr" style="display:none"></div>`}
    ${ev ? `<div class="listing-meta" id="lm-event" style="border-left:3px solid #7af;padding-left:10px">${eventRows}</div>` : ''}
    <div class="listing-meta">
      ${d.category ? `<span class="listing-meta-key">Категория</span><span class="listing-meta-val">${esc(d.category)}</span>` : ''}
      ${d.city ? `<span class="listing-meta-key">Город</span><span class="listing-meta-val">${esc(d.city)}</span>` : ''}
      <span class="listing-meta-key">Контакт</span><span class="listing-meta-val" id="lm-contact">${contact}</span>
      <span class="listing-meta-key">Раздел</span><span class="listing-meta-val">${esc(sectionName(d.type))}</span>
      <span class="listing-meta-key">Опубликовано</span><span class="listing-meta-val">${fmtDate(d.created_at)}</span>
      ${d.status && d.status!=='active' ? `<span class="listing-meta-key">Статус</span><span class="listing-meta-val">${esc(d.status)}</span>` : ''}
    </div>
    ${flexRows ? `<div class="listing-meta" id="lm-flex">${flexRows}</div>` : `<div id="lm-flex"></div>`}
    <div class="listing-stats">
      <span>👁 Открытий: <b>${d.opens}</b></span>
      <span>🙋 Уникальных: <b>${d.viewers}</b></span>
      <span>🔍 Из поиска: <b>${d.search_opens}</b></span>
      <span>📂 Из каталога: <b>${d.catalog_opens}</b></span>
    </div>
    <div class="lm-admin-bar" id="lm-admin-bar">
      <button class="btn btn-sm" style="background:#1a3a6a;color:#7af" onclick="lmStartEdit()">✏️ Редактировать</button>
      <button class="btn btn-sm" id="lm-toggle-btn" style="background:#1a4a2a;color:#7f7" onclick="lmToggleSold()">${d.is_sold ? '🔓 Открыть' : '🔒 Скрыть'}</button>
      <button class="btn btn-sm" style="background:#4a1a1a;color:#f77" onclick="lmConfirmDelete()">🗑 Удалить</button>
    </div>
    <div class="lm-admin-bar" id="lm-edit-bar" style="display:none">
      <button class="btn btn-sm" style="background:#1a4a2a;color:#7f7" onclick="lmSaveEdit()">💾 Сохранить</button>
      <button class="btn btn-sm" style="background:#333;color:#aaa" onclick="lmCancelEdit()">✕ Отмена</button>
    </div>
    <div id="lm-delete-confirm" style="display:none;background:#2a0a0a;border:1px solid #f44;border-radius:8px;padding:14px;margin-top:10px;text-align:center">
      <div style="color:#f77;margin-bottom:10px">Удалить объявление безвозвратно?</div>
      <button class="btn btn-sm" style="background:#8b0000;color:#fff;margin-right:8px" onclick="lmDoDelete()">✅ Да, удалить</button>
      <button class="btn btn-sm" style="background:#333;color:#aaa" onclick="document.getElementById('lm-delete-confirm').style.display='none'">Отмена</button>
    </div>`;
}

function lmStartEdit() {
  const d = window._currentListing;
  document.getElementById('lm-admin-bar').style.display = 'none';
  document.getElementById('lm-edit-bar').style.display = 'flex';
  // Поля → инпуты с подписями
  document.getElementById('lm-title').innerHTML =
    `<div class="lm-field-group"><label class="lm-field-label">Название</label>
     <input id="lm-inp-title" class="lm-edit-input" value="${esc(d.title||'')}"></div>`;
  const priceEl = document.getElementById('lm-price');
  priceEl.style.display = '';
  priceEl.innerHTML =
    `<div class="lm-field-group"><label class="lm-field-label">Цена</label>
     <input id="lm-inp-price" class="lm-edit-input" value="${esc(d.price||'')}"></div>`;
  const descrEl = document.getElementById('lm-descr');
  descrEl.style.display = '';
  descrEl.innerHTML =
    `<div class="lm-field-group"><label class="lm-field-label">Описание</label>
     <textarea id="lm-inp-descr" class="lm-edit-input" rows="5">${esc(d.descr||'')}</textarea></div>`;
  document.getElementById('lm-contact').innerHTML =
    `<div class="lm-field-group"><label class="lm-field-label">Контакт</label>
     <input id="lm-inp-contact" class="lm-edit-input" value="${esc(d.contact||'')}"></div>`;
  // Событие (афиша): дата/время/площадка/цена входа
  const evEl = document.getElementById('lm-event');
  if (evEl && d.event) {
    const ev = d.event;
    evEl.innerHTML = `
      <span class="listing-meta-key"><label class="lm-field-label">📅 Дата</label></span>
      <span class="listing-meta-val"><input id="lm-inp-ev-date" class="lm-edit-input" value="${esc(ev.date||'')}" placeholder="21.09.26 / 210926 / 21-09-2026" title="Любой привычный формат: 21.09.26, 21-09-2026, 210926, 2026-09-21"></span>
      <span class="listing-meta-key"><label class="lm-field-label">⏰ Время</label></span>
      <span class="listing-meta-val"><input id="lm-inp-ev-time" class="lm-edit-input" value="${esc(ev.time||'')}" placeholder="19:00 / 1900 / 19" title="Любой привычный формат: 19:00, 19.00, 1900, 19"></span>
      <span class="listing-meta-key"><label class="lm-field-label">📍 Площадка</label></span>
      <span class="listing-meta-val"><input id="lm-inp-ev-venue" class="lm-edit-input" value="${esc(ev.venue||'')}"></span>
      <span class="listing-meta-key"><label class="lm-field-label">💲 Цена входа</label></span>
      <span class="listing-meta-val"><input id="lm-inp-ev-price" class="lm-edit-input" value="${esc(ev.price_text||'')}"></span>`;
  }
  // Flex fields
  const flexEl = document.getElementById('lm-flex');
  const flexFields = d.flex_fields || [];
  if (flexEl && flexFields.length > 0) {
    flexEl.className = 'listing-meta';
    flexEl.innerHTML = flexFields.map(f => {
      const val = (d.flex || {})[f.key] ?? '';
      return `<span class="listing-meta-key"><label class="lm-field-label">${esc(f.label)}</label></span>
              <span class="listing-meta-val"><input id="lm-inp-flex-${esc(f.key)}" class="lm-edit-input" data-flex-key="${esc(f.key)}" value="${esc(String(val))}"></span>`;
    }).join('');
  }
  // Кнопки × на фото
  const photos = document.querySelectorAll('#lm-photos .listing-photo');
  photos.forEach((ph, i) => {
    const btn = document.createElement('button');
    btn.className = 'lm-photo-del';
    btn.innerHTML = '×';
    btn.onclick = (e) => { e.stopPropagation(); lmRemovePhoto(i); };
    ph.style.position = 'relative';
    ph.appendChild(btn);
  });
  // Кнопки добавления фото/видео
  const photosWrap = document.getElementById('lm-photos');
  if (photosWrap && !document.getElementById('lm-media-add')) {
    const bar = document.createElement('div');
    bar.id = 'lm-media-add';
    bar.style.cssText = 'display:flex;gap:8px;margin:6px 0 12px';
    const nPhotos = (d.photo_ids || []).length;
    bar.innerHTML = `
      ${nPhotos < 3 ? `<button class="btn btn-sm" style="background:#1a3a6a;color:#7af" onclick="document.getElementById('lm-file-photo').click()">➕ Фото (${nPhotos}/3)</button>` : ''}
      <button class="btn btn-sm" style="background:#1a3a6a;color:#7af" onclick="document.getElementById('lm-file-video').click()">🎥 ${d.video_type ? 'Заменить видео' : 'Добавить видео'}</button>
      <span id="lm-upload-status" style="color:#9bc;font-size:12px;align-self:center"></span>
      <input type="file" id="lm-file-photo" accept="image/*" style="display:none" onchange="lmUploadMedia(this, 'photo')">
      <input type="file" id="lm-file-video" accept="video/*" style="display:none" onchange="lmUploadMedia(this, 'video')">`;
    photosWrap.after(bar);
  }
}

async function lmUploadMedia(input, kind) {
  const d = window._currentListing;
  const file = input.files[0];
  if (!file) return;
  if (kind === 'video' && file.size > 20 * 1024 * 1024) {
    alert('Видео больше 20 МБ — Bot API не сможет показывать его в приложении.');
    input.value = '';
    return;
  }
  const status = document.getElementById('lm-upload-status');
  if (status) status.textContent = '⏳ Загрузка в Telegram…';
  try {
    const ep = kind === 'photo' ? 'add_photo' : 'add_video';
    const r = await fetch(`/api/listing/${d.id}/${ep}?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/octet-stream'},
      body: file,
    });
    const res = await r.json();
    if (!r.ok) throw new Error(res.detail || r.status);
    // Обновляем локальное состояние и перерисовываем в режиме редактирования
    if (kind === 'photo') {
      d.photo_ids = res.photos;
    } else {
      d.video_type = 'telegram';
      d.video_id = res.file_id;
      d.flex = {...(d.flex || {}), video: res.file_id};
    }
    window._currentListing = d;
    renderListingModal(d);
    lmStartEdit();
  } catch(e) {
    if (status) status.textContent = '';
    alert('Ошибка загрузки: ' + e.message);
    input.value = '';
  }
}

function lmCancelEdit() {
  const d = window._currentListing;
  renderListingModal(d);
}

async function lmSaveEdit() {
  const d = window._currentListing;
  // Collect flex inputs
  const flexInputs = document.querySelectorAll('[data-flex-key]');
  let flexUpdate = null;
  if (flexInputs.length > 0) {
    flexUpdate = {};
    flexInputs.forEach(inp => { flexUpdate[inp.dataset.flexKey] = inp.value; });
  }
  // Событие (афиша)
  let eventUpdate = null;
  const evDate = document.getElementById('lm-inp-ev-date');
  if (evDate) {
    eventUpdate = {
      date:       evDate.value.trim(),
      time:       document.getElementById('lm-inp-ev-time')?.value.trim() ?? '',
      venue:      document.getElementById('lm-inp-ev-venue')?.value.trim() ?? '',
      price_text: document.getElementById('lm-inp-ev-price')?.value.trim() ?? '',
    };
  }
  const body = {
    title:   document.getElementById('lm-inp-title')?.value ?? d.title,
    descr:   document.getElementById('lm-inp-descr')?.value ?? d.descr,
    price:   document.getElementById('lm-inp-price')?.value ?? d.price,
    contact: document.getElementById('lm-inp-contact')?.value ?? d.contact,
    ...(flexUpdate ? {flex: flexUpdate} : {}),
    ...(eventUpdate ? {event: eventUpdate} : {}),
  };
  try {
    const r = await fetch(`/api/listing/${d.id}`, {
      method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    const saved = await r.json();
    if (!r.ok) throw new Error(saved.detail || r.status);
    // Обновляем локальный объект и перерисовываем
    Object.assign(window._currentListing, {
      title: body.title, descr: body.descr, price: body.price, contact: body.contact,
    });
    if (flexUpdate) {
      window._currentListing.flex = {...(d.flex || {}), ...flexUpdate};
    }
    if (eventUpdate) {
      // сервер возвращает нормализованные дату/время (21.09.26 → 21-09-2026)
      window._currentListing.event = {...(d.event || {}), ...eventUpdate, ...(saved.event || {})};
    }
    renderListingModal(window._currentListing);
  } catch(e) { alert('Ошибка сохранения: ' + e.message); }
}

async function lmToggleSold() {
  const d = window._currentListing;
  try {
    const r = await fetch(`/api/listing/${d.id}/toggle_sold`, {method:'POST'});
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const res = await r.json();
    window._currentListing.is_sold = res.is_sold;
    renderListingModal(window._currentListing);
  } catch(e) { alert('Ошибка: ' + e.message); }
}

function lmConfirmDelete() {
  document.getElementById('lm-delete-confirm').style.display = '';
}

async function lmDoDelete() {
  const d = window._currentListing;
  try {
    const r = await fetch(`/api/listing/${d.id}`, {method:'DELETE'});
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    closeListingModal();
    // Убираем карточку из текущего списка если видна
    document.querySelectorAll(`[data-listing-id="${d.id}"]`).forEach(el => el.remove());
  } catch(e) { alert('Ошибка удаления: ' + e.message); }
}

async function lmRemovePhoto(idx) {
  const d = window._currentListing;
  if (!confirm(`Удалить фото ${idx+1}?`)) return;
  try {
    const r = await fetch(`/api/listing/${d.id}/remove_photo`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({photo_index: idx})
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    d.photo_ids.splice(idx, 1);
    window._currentListing = d;
    renderListingModal(d);
    lmStartEdit();
  } catch(e) { alert('Ошибка: ' + e.message); }
}

let _lbPhotos = [], _lbIdx = 0;
function openLightbox(srcs, idx=0) {
  _lbPhotos = Array.isArray(srcs) ? srcs : [srcs];
  _lbIdx = idx;
  _lbUpdate();
  document.getElementById('lightbox').classList.add('open');
}
function _lbUpdate() {
  document.getElementById('lightbox-img').src = _lbPhotos[_lbIdx];
  const counter = document.getElementById('lb-counter');
  counter.textContent = _lbPhotos.length > 1 ? `${_lbIdx+1} / ${_lbPhotos.length}` : '';
  counter.style.display = _lbPhotos.length > 1 ? 'block' : 'none';
  document.getElementById('lb-prev').style.display = _lbPhotos.length > 1 ? 'block' : 'none';
  document.getElementById('lb-next').style.display = _lbPhotos.length > 1 ? 'block' : 'none';
}
function lbGo(dir) {
  _lbIdx = (_lbIdx + dir + _lbPhotos.length) % _lbPhotos.length;
  _lbUpdate();
}
function closeLightbox() {
  document.getElementById('lightbox').classList.remove('open');
}
document.addEventListener('keydown', e => {
  const lb = document.getElementById('lightbox');
  if (lb.classList.contains('open')) {
    if (e.key==='ArrowRight') lbGo(1);
    else if (e.key==='ArrowLeft') lbGo(-1);
    else if (e.key==='Escape') closeLightbox();
    return;
  }
  const ml = document.getElementById('modal-listing');
  if (ml.classList.contains('open')) {
    if (e.key === 'Escape') closeListingModal();
    return;
  }
  if (e.key==='Escape') closeAll();
});

// ── Catalog drill-down ──
let _drillState = {type:'', catId:0, stype:'', title:'', action:''};

function closeDrill() {
  document.getElementById('modal-drill').classList.remove('open');
}

async function openDrillListings(catId, stype, title, offset=0) {
  _drillState = {type:'listings', catId, stype, title, action:''};
  const el = document.getElementById('drill-content');
  document.getElementById('drill-title').textContent = '…';
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  document.getElementById('modal-drill').classList.add('open');
  const params = new URLSearchParams({cat_id: catId||0, stype: stype||'', offset, limit: 24});
  const data = await fetch(`/api/catalog/drill/listings?${params}`).then(r=>r.json());
  document.getElementById('drill-title').textContent = `${data.total} объявлений — ${title}`;
  if (!data.rows || !data.rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Нет объявлений</div>'; return;
  }
  const totalPages = Math.ceil(data.total / 24);
  const curPage = Math.floor(offset / 24) + 1;
  const pag = totalPages > 1 ? `<div class="an-pagination" style="margin-top:16px">
    <button onclick="openDrillListings(_drillState.catId,_drillState.stype,_drillState.title,${Math.max(0,offset-24)})" ${offset===0?'disabled':''}>◀</button>
    <span>${curPage} / ${totalPages}</span>
    <button onclick="openDrillListings(_drillState.catId,_drillState.stype,_drillState.title,${offset+24})" ${offset+24>=data.total?'disabled':''}>▶</button>
  </div>` : '';
  el.innerHTML = `<div class="card-grid">${data.rows.map((r,i)=>cardRowHtml({...r, search_opens:0, catalog_opens:0}, offset+i+1)).join('')}</div>${pag}`;
}

async function openDrillUsers(catId, stype, title) {
  _drillState = {type:'users', catId, stype, title, action:''};
  const el = document.getElementById('drill-content');
  document.getElementById('drill-title').textContent = '…';
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  document.getElementById('modal-drill').classList.add('open');
  const params = new URLSearchParams({cat_id: catId||0, stype: stype||''});
  const data = await fetch(`/api/catalog/drill/users?${params}`).then(r=>r.json());
  const rows = data.rows || [];
  document.getElementById('drill-title').textContent = `${rows.length} пользователей — ${title}`;
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Нет данных</div>'; return;
  }
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:6px 8px">Пользователь</th>
      <th style="padding:6px 8px">Telegram</th>
      <th style="padding:6px 8px;white-space:nowrap">Последний визит</th>
      <th style="padding:6px 8px;text-align:right">Просм.</th>
      <th style="padding:6px 8px;text-align:right">Контакт.</th>
    </tr></thead>
    <tbody>${rows.map(u => `
      <tr style="border-top:1px solid #0d1628">
        <td style="padding:7px 8px;color:#ccd">${esc(u.full_name||String(u.user_id))}</td>
        <td style="padding:7px 8px;color:#778">${u.username ? '@'+esc(u.username) : '—'}</td>
        <td style="padding:7px 8px;color:#556;white-space:nowrap;font-size:12px">${esc(u.last_seen||'—')}</td>
        <td style="padding:7px 8px;text-align:right"><b style="color:#9bc">${u.opens}</b></td>
        <td style="padding:7px 8px;text-align:right;color:#778">${u.contacts||0}</td>
      </tr>`).join('')}
    </tbody></table>`;
}

async function openDrillViews(catId, stype, title, action='open', offset=0) {
  _drillState = {type:'views', catId, stype, title, action};
  const el = document.getElementById('drill-content');
  const label = action==='contact' ? 'контактов' : 'просмотров';
  document.getElementById('drill-title').textContent = '…';
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  document.getElementById('modal-drill').classList.add('open');
  const params = new URLSearchParams({cat_id: catId||0, stype: stype||'', action, offset, limit: 50});
  const data = await fetch(`/api/catalog/drill/views?${params}`).then(r=>r.json());
  document.getElementById('drill-title').textContent = `${data.total} ${label} — ${title}`;
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Нет данных</div>'; return;
  }
  const totalPages = Math.ceil(data.total / 50);
  const curPage = Math.floor(offset / 50) + 1;
  const pag = totalPages > 1 ? `<div class="an-pagination" style="margin-top:12px">
    <button onclick="openDrillViews(_drillState.catId,_drillState.stype,_drillState.title,_drillState.action,${Math.max(0,offset-50)})" ${offset===0?'disabled':''}>◀</button>
    <span>${curPage} / ${totalPages}</span>
    <button onclick="openDrillViews(_drillState.catId,_drillState.stype,_drillState.title,_drillState.action,${offset+50})" ${offset+50>=data.total?'disabled':''}>▶</button>
  </div>` : '';
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:6px 8px;white-space:nowrap">Дата/время</th>
      <th style="padding:6px 8px">Пользователь</th>
      <th style="padding:6px 8px">Объявление</th>
    </tr></thead>
    <tbody>${rows.map(v => `
      <tr style="border-top:1px solid #0d1628;cursor:pointer" onclick="openListing(${v.listing_id})">
        <td style="padding:7px 8px;color:#556;white-space:nowrap;font-size:12px">${esc(v.ts)}</td>
        <td style="padding:7px 8px;color:#778">${v.username ? '@'+esc(v.username) : esc(v.full_name||String(v.user_id))}</td>
        <td style="padding:7px 8px;color:#ccd">${esc(v.listing_title)}</td>
      </tr>`).join('')}
    </tbody></table>${pag}`;
}

// ── Cities ──
async function anCities(el) {
  const rows = await fetch('/api/analytics/cities').then(r=>r.json());
  if (!rows.length) { el.innerHTML = '<div class="empty">Нет данных по городам</div>'; return; }
  const TYPE_NAMES = {market:'Барахолка', service:'Услуги', vacancy:'Вакансии', events:'Афиша'};
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;border-bottom:2px solid #1a2050">
      <th style="padding:9px 10px;text-align:left">Город</th>
      <th style="padding:9px 10px;text-align:right">Всего</th>
      <th style="padding:9px 10px;text-align:right">Акт.</th>
      <th style="padding:9px 10px;text-align:right">Просм.</th>
      <th style="padding:9px 10px;text-align:right">Польз.</th>
      <th style="padding:9px 10px;text-align:right">Контакт.</th>
      <th style="padding:9px 10px;text-align:right">Конверс.</th>
      <th style="padding:9px 10px;text-align:left">По разделам</th>
    </tr></thead>
    <tbody>${rows.map(r => {
      const conv = r.conversion;
      const convColor = conv >= 10 ? '#6ef5aa' : conv >= 3 ? '#f5d06e' : '#778';
      const byType = Object.entries(r.by_type||{})
        .sort((a,b)=>b[1]-a[1])
        .map(([t,c])=>`<span style="color:#556">${TYPE_NAMES[t]||t}:</span>&nbsp;<b style="color:#9bc">${c}</b>`)
        .join('&ensp;');
      return `<tr style="border-bottom:1px solid #0d1628">
        <td style="padding:9px 10px;color:#eef;font-weight:600">📍 ${esc(r.name)}</td>
        <td style="padding:9px 10px;text-align:right;color:#556">${r.total}</td>
        <td style="padding:9px 10px;text-align:right"><b style="color:#9bc">${r.active}</b></td>
        <td style="padding:9px 10px;text-align:right;color:#aab">${r.views}</td>
        <td style="padding:9px 10px;text-align:right;color:#778">${r.viewers}</td>
        <td style="padding:9px 10px;text-align:right;color:#aab">${r.contacts}</td>
        <td style="padding:9px 10px;text-align:right;font-weight:600;color:${convColor}">${conv}%</td>
        <td style="padding:9px 10px;font-size:11px">${byType||'—'}</td>
      </tr>`;
    }).join('')}
    </tbody></table>`;
}

// ── Initial analytics load ──
async function loadAnalytics() {
  await anLoad('overview');
  document.getElementById('an-updated').textContent = new Date().toLocaleTimeString();
}

// ── init ──
(async()=>{ applyLang(); setupDnD(); await loadAll(); await loadTree('market'); })();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

if __name__ == "__main__":
    print("Open: http://localhost:8001")
    print("Удалённо (Tailscale): http://<tailscale-ip>:8001")
    # 0.0.0.0 + IP-фильтр выше: пускаем только localhost и Tailscale-сеть
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level="warning")
