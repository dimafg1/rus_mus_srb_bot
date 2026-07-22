"""
Локальный админ для управления категориями.
Запуск: python category_admin.py   (или двойной клик на category_admin.command)
Открыть: http://localhost:8001
"""
import asyncio
import base64
import html
import secrets
import sqlite3, json, datetime, os, re
from contextlib import contextmanager
from pathlib import Path
from collections import defaultdict
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse, PlainTextResponse
from pydantic import BaseModel, Field
from typing import Optional, List, Any
import httpx, uvicorn

from app.db_path import config_value, resolve_sqlite_path
from app.admin_ids import ADMIN_IDS

# Load bot token for Telegram media proxy
_ROOT = Path(__file__).resolve().parent
BOT_TOKEN = (config_value(_ROOT, "BOT_TOKEN", "") or "").strip()

DB_PATH = resolve_sqlite_path(_ROOT)
ROOT_IDS = {"market": 30, "services": 80, "vacancy": 90}
ROOT_NAMES = {"market": "Барахолка", "services": "Услуги", "vacancy": "Вакансии"}

# Логи в файл с ротацией: logs/admin.log, 5 МБ x 5 файлов
import logging
from logging.handlers import RotatingFileHandler
_log_dir = Path(config_value(_ROOT, "LOG_DIR", "logs") or "logs").expanduser()
if not _log_dir.is_absolute():
    _log_dir = (_ROOT / _log_dir).resolve()
_log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
_log_dir.chmod(0o700)
_fh = RotatingFileHandler(_log_dir / "admin.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")


class _SecretRedactingFormatter(logging.Formatter):
    def format(self, record):
        rendered = super().format(record)
        return rendered.replace(BOT_TOKEN, "[REDACTED_BOT_TOKEN]") if BOT_TOKEN else rendered


_formatter = _SecretRedactingFormatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
_fh.setFormatter(_formatter)
_sh = logging.StreamHandler()
_sh.setFormatter(_formatter)
_log_level = (config_value(_ROOT, "LOG_LEVEL", "INFO") or "INFO").upper()
logging.basicConfig(level=_log_level, handlers=[_fh, _sh])
# HTTPX пишет полный Telegram API URL, в который входит токен бота. Не даём
# библиотечным INFO-сообщениям попадать ни в admin.log, ни в systemd journal.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = FastAPI()

_ADMIN_USER = (config_value(_ROOT, "CATEGORY_ADMIN_USER", "") or "").strip()
_ADMIN_PASSWORD = config_value(_ROOT, "CATEGORY_ADMIN_PASSWORD", "") or ""


def _basic_auth_ok(request: Request) -> bool:
    if not (_ADMIN_USER and _ADMIN_PASSWORD):
        return False
    raw = request.headers.get("authorization", "")
    if not raw.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(raw[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(username, _ADMIN_USER) and secrets.compare_digest(
        password, _ADMIN_PASSWORD
    )

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
_ALLOWED_HOSTNAMES = {
    value.strip().lower().rstrip(".")
    for value in (
        config_value(_ROOT, "CATEGORY_ADMIN_ALLOWED_HOSTS", "") or ""
    ).split(",")
    if value.strip()
}


def _admin_host_allowed(request: Request) -> bool:
    host = (request.url.hostname or "").lower().rstrip(".")
    if host == "localhost" or host in _ALLOWED_HOSTNAMES:
        return True
    try:
        host_ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(host_ip in net for net in _ALLOWED_NETS)


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
        origin_host = (parsed.hostname or "").lower().rstrip(".")
        request_host = (request.url.hostname or "").lower().rstrip(".")
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
        request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == request.url.scheme
        and origin_host == request_host
        and origin_port == request_port
    )


@app.middleware("http")
async def _ip_allowlist(request: Request, call_next):
    if not _admin_host_allowed(request):
        return PlainTextResponse("Forbidden host", status_code=403)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin(request):
        return PlainTextResponse("Forbidden origin", status_code=403)

    client_ip = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(client_ip)
        if not any(ip in net for net in _ALLOWED_NETS):
            logging.warning("Отклонён запрос с недоверенного IP: %s %s", client_ip, request.url.path)
            return PlainTextResponse("Forbidden", status_code=403)
    except ValueError:
        return PlainTextResponse("Forbidden", status_code=403)

    # Если credentials заданы, защищаем ими и прямой доступ, и запрос через
    # локальный reverse proxy. Без credentials разрешён только localhost.
    if _ADMIN_USER and _ADMIN_PASSWORD:
        if not _basic_auth_ok(request):
            return PlainTextResponse(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="category-admin"'},
            )
    elif not ip.is_loopback:
        return PlainTextResponse(
            "Remote admin access is disabled until credentials are configured",
            status_code=503,
        )
    return await call_next(request)


# ─────────────────────────── DB helpers ───────────────────────────

@contextmanager
def db():
    # timeout + busy_timeout: бот и админка работают с базой параллельно,
    # без них конкурентная запись даёт "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


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

        # Казахский — третий целевой язык проекта (RU/EN/KK), колонка была
        # запланирована заранее, но не заведена до появления редактора текстов.
        bottext_cols = [r[1] for r in conn.execute('PRAGMA table_info("BotText")').fetchall()]
        if "text_kk" not in bottext_cols:
            conn.execute('ALTER TABLE "BotText" ADD COLUMN text_kk TEXT NOT NULL DEFAULT \'\'')
            conn.commit()

        menu_cols = [r[1] for r in conn.execute("PRAGMA table_info(menu)").fetchall()]
        if "text_kk" not in menu_cols:
            conn.execute("ALTER TABLE menu ADD COLUMN text_kk TEXT NOT NULL DEFAULT ''")
            conn.commit()

        # Ограничение на запись (mute) — бан только на публикацию нового
        # контента, просмотр разделов бота остаётся доступен.
        botuser_cols = [r[1] for r in conn.execute('PRAGMA table_info("BotUser")').fetchall()]
        if "is_muted" not in botuser_cols:
            conn.execute('ALTER TABLE "BotUser" ADD COLUMN is_muted BOOLEAN NOT NULL DEFAULT 0')
            conn.commit()

        # Журнал рассылок — только веб-админка пишет и читает эту таблицу,
        # боту она не нужна (нет соответствующей SQLModel-модели).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcast_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text_ru TEXT NOT NULL,
                sent_at DATETIME NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 0,
                failed INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.commit()


@app.on_event("startup")
def _startup_migrate() -> None:
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
            """SELECT category_id, COUNT(*) FROM listing
               WHERE status='active' AND COALESCE(is_sold,0)=0
               GROUP BY category_id"""
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


def _normalized_category_name(value: str) -> str:
    name = (value or "").strip()
    if not name or any(ord(ch) < 32 for ch in name):
        raise HTTPException(400, "Название категории не может быть пустым")
    if len(name) > 200:
        raise HTTPException(400, "Название категории длиннее 200 символов")
    return name


def _normalized_category_slug(value: str) -> str:
    slug = (value or "").strip().lower()
    if not slug or not re.fullmatch(r"[a-z0-9_-]+", slug):
        raise HTTPException(
            400,
            "Slug должен содержать только a-z, 0-9, дефис или подчёркивание",
        )
    if len(slug) > 100:
        raise HTTPException(400, "Slug длиннее 100 символов")
    return slug


@app.post("/api/categories")
def create_category(body: CatCreate):
    name = _normalized_category_name(body.name)
    slug = _normalized_category_slug(body.slug)
    with db() as conn:
        if not conn.execute(
            "SELECT 1 FROM category WHERE id=?", (body.parent_id,)
        ).fetchone():
            raise HTTPException(400, "Родительская категория не найдена")
        if conn.execute(
            "SELECT id FROM category WHERE lower(trim(slug))=?", (slug,)
        ).fetchone():
            raise HTTPException(400, f"Slug «{slug}» уже занят")
        max_order = conn.execute(
            "SELECT COALESCE(MAX(order_num),0) FROM category WHERE parent_id=?", (body.parent_id,)
        ).fetchone()[0]
        cur = conn.execute(
            "INSERT INTO category (name, slug, parent_id, order_num) VALUES (?,?,?,?)",
            (name, slug, body.parent_id, max_order + 10),
        )
        conn.commit()
        return {"id": cur.lastrowid}


@app.patch("/api/categories/{cat_id}")
def update_category(cat_id: int, body: CatUpdate):
    with db() as conn:
        current = conn.execute(
            "SELECT id, parent_id FROM category WHERE id=?", (cat_id,)
        ).fetchone()
        if not current:
            raise HTTPException(404)
        normalized_slug = None
        if body.slug is not None:
            normalized_slug = _normalized_category_slug(body.slug)
            if conn.execute(
                "SELECT id FROM category "
                "WHERE lower(trim(slug))=? AND id!=?",
                (normalized_slug, cat_id),
            ).fetchone():
                raise HTTPException(400, f"Slug «{normalized_slug}» уже занят")
        fields, vals = [], []
        if body.name is not None:
            fields.append("name=?"); vals.append(_normalized_category_name(body.name))
        if normalized_slug is not None:
            fields.append("slug=?"); vals.append(normalized_slug)
        if body.parent_id is not None:
            if current["parent_id"] is None:
                raise HTTPException(400, "Корневую категорию нельзя перемещать")
            if body.parent_id == cat_id:
                raise HTTPException(400, "Категория не может быть родителем самой себя")
            if not conn.execute(
                "SELECT 1 FROM category WHERE id=?", (body.parent_id,)
            ).fetchone():
                raise HTTPException(400, "Родительская категория не найдена")
            parent_is_descendant = conn.execute(
                """
                WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM category WHERE parent_id=?
                    UNION
                    SELECT c.id
                    FROM category c
                    JOIN descendants d ON c.parent_id=d.id
                )
                SELECT 1 FROM descendants WHERE id=?
                """,
                (cat_id, body.parent_id),
            ).fetchone()
            if parent_is_descendant:
                raise HTTPException(
                    400,
                    "Категорию нельзя переместить внутрь её подкатегории",
                )
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
        if not conn.execute("SELECT 1 FROM category WHERE id=?", (cat_id,)).fetchone():
            raise HTTPException(404, "Категория не найдена")
        if conn.execute("SELECT COUNT(*) FROM category WHERE parent_id=?", (cat_id,)).fetchone()[0]:
            raise HTTPException(400, "Сначала удалите подкатегории")
        listings = conn.execute(
            """SELECT COUNT(*) FROM listing
               WHERE category_id=? OR extra_category_id1=? OR extra_category_id2=?""",
            (cat_id, cat_id, cat_id),
        ).fetchone()[0]
        if listings:
            raise HTTPException(
                400,
                f"Категория используется в {listings} объявлениях (включая архивные)",
            )
        items = conn.execute(
            "SELECT COUNT(*) FROM item WHERE category_id=?", (cat_id,)
        ).fetchone()[0]
        profiles = conn.execute(
            "SELECT COUNT(*) FROM profile WHERE category_id=?", (cat_id,)
        ).fetchone()[0]
        if items or profiles:
            refs = []
            if items:
                refs.append(f"анкеты: {items}")
            if profiles:
                refs.append(f"профили: {profiles}")
            raise HTTPException(
                400,
                "Категория используется: " + ", ".join(refs),
            )
        try:
            conn.execute("DELETE FROM category WHERE id=?", (cat_id,))
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "Категория всё ещё используется") from exc
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
                "active":   q(
                    "SELECT COUNT(*) FROM listing "
                    "WHERE status='active' AND COALESCE(is_sold,0)=0"
                ),
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
                LEFT JOIN listing l        ON l.category_id=c.id
                    AND l.status='active' AND COALESCE(l.is_sold,0)=0
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


TOP_CARDS_SORT_COLUMNS = {
    "title": "title", "section": "lv.section", "price": "l.price",
    "contact": "l.contact", "opens": "opens", "users": "users",
    "search_opens": "search_opens", "catalog_opens": "catalog_opens",
    "status": "l.status",
}
TOP_CARDS_FILTER_FIELDS = ["title", "section", "contact", "status"]


def _casefold_filter_paginate(rows: list, q: str, q_fields: list,
                               f_dict: dict, filter_fields: list,
                               offset: int, limit: int):
    """Регистронезависимый поиск/фильтр (в т.ч. кириллица) на стороне Python —
    SQLite LIKE и LOWER() сворачивают регистр только для ASCII, кириллица
    (например «Микрофон» при запросе «микрофон») через них не находится."""
    if q:
        qf = q.casefold()
        rows = [r for r in rows if any(qf in (r.get(f) or "").casefold() for f in q_fields)]
    for key in filter_fields:
        val = (f_dict.get(key) or "").strip()
        if val:
            vf = val.casefold()
            rows = [r for r in rows if vf in (r.get(key) or "").casefold()]
    total = len(rows)
    return total, rows[offset:offset + limit]


@app.get("/api/analytics/top_cards")
def analytics_top_cards(offset: int = 0, limit: int = 24, q: str = "",
                         sort: str = "opens", order: str = "desc", filters: str = ""):
    sort_col = TOP_CARDS_SORT_COLUMNS.get(sort, "opens")
    sort_dir = "ASC" if order == "asc" else "DESC"
    try:
        f_dict = json.loads(filters) if filters else {}
    except Exception:
        f_dict = {}
    with db() as conn:
        try:
            rows = conn.execute(f"""
                SELECT lv.listing_id, lv.section,
                       COUNT(*) AS opens, COUNT(DISTINCT lv.user_id) AS users,
                       SUM(CASE WHEN lv.source='search' THEN 1 ELSE 0 END) AS search_opens,
                       SUM(CASE WHEN lv.source='catalog' THEN 1 ELSE 0 END) AS catalog_opens,
                       COALESCE(l.title,'Без названия') AS title,
                       l.owner_id, l.contact, l.price, l.photo_file_id,
                       l.is_sold, l.status
                FROM listing_views lv
                LEFT JOIN listing l ON l.id=lv.listing_id
                WHERE lv.action='open'
                GROUP BY lv.listing_id ORDER BY {sort_col} {sort_dir}
            """).fetchall()
        except Exception as e:
            return {"total": 0, "offset": offset, "limit": limit, "rows": [], "error": str(e)}
    result = [{
        "id": r[0], "section": r[1], "opens": r[2], "users": r[3],
        "search_opens": r[4] or 0, "catalog_opens": r[5] or 0,
        "title": r[6], "owner_id": r[7], "contact": r[8] or "",
        "price": r[9] or "", "photo_file_id": r[10] or "",
        "is_sold": bool(r[11]), "status": r[12] or ""} for r in rows]
    total, page = _casefold_filter_paginate(
        result, q, ["title", "contact"], f_dict, TOP_CARDS_FILTER_FIELDS, offset, limit)
    return {"total": total, "offset": offset, "limit": limit, "rows": page}


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
                   COUNT(DISTINCT CASE
                       WHEN l.status='active' AND COALESCE(l.is_sold,0)=0 THEN l.id
                   END) AS active,
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
                       (SELECT COUNT(*) FROM listing ll
                        WHERE ll.owner_id=l.owner_id
                          AND ll.status='active'
                          AND COALESCE(ll.is_sold,0)=0) AS active,
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
                       COUNT(DISTINCT CASE WHEN l.status='active' AND COALESCE(l.is_sold,0)=0 THEN l.id END) AS active,
                       COUNT(DISTINCT CASE WHEN l.status!='active' OR COALESCE(l.is_sold,0)=1 THEN l.id END) AS sold,
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
    if not isinstance(flex, dict):
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


def _video_from_url(raw: str) -> dict:
    """URL → структура видео (та же логика, что в _parse_video)."""
    if not raw:
        return {}
    yt_id = ""
    for part in raw.split("v=")[1:]:
        yt_id = part.split("&")[0].strip()
        break
    if not yt_id:
        for part in raw.split("youtu.be/")[1:]:
            yt_id = part.split("?")[0].strip()
            break
    if yt_id:
        return {"video_type": "youtube", "video_id": yt_id, "video_url": raw}
    return {}


def _video_from_release(conn, listing_id: int) -> dict:
    """Видео релиза живёт в release_meta (клип file_id или YouTube-ссылка),
    а не во flex — админка иначе его не видит."""
    try:
        row = conn.execute(
            "SELECT video_file_id, links FROM release_meta WHERE listing_id=?",
            (listing_id,)).fetchone()
        if not row:
            return {}
        if row[0]:
            return {"video_type": "telegram", "video_id": row[0]}
        links = json.loads(row[1] or "[]")
        if not isinstance(links, list):
            return {}
        for l in links:
            if not isinstance(l, dict):
                continue
            url = (l.get("url") or "")
            if "youtube.com" in url or "youtu.be" in url:
                v = _video_from_url(url)
                if v:
                    return v
    except Exception as e:
        print(f"[_video_from_release] {listing_id}: {e}")
    return {}


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
                       l.flex, l.category_id, l.expires_at, l.archive_reason
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
        # Данные релиза: тип/статус/исполнитель/треки лежат в release_meta
        release = None
        rrow = conn.execute(
            "SELECT rm.release_type, rm.status, rm.links, rm.release_date, rm.recorded_at, "
            "a.name, (SELECT COUNT(*) FROM release_track rt WHERE rt.listing_id=rm.listing_id), "
            "rm.artist_id "
            "FROM release_meta rm LEFT JOIN artist a ON a.id=rm.artist_id "
            "WHERE rm.listing_id=?", (listing_id,)
        ).fetchone()
        if rrow:
            try:
                links_data = json.loads(rrow[2]) if rrow[2] else []
                if not isinstance(links_data, list):
                    links_data = []
            except Exception:
                links_data = []
            release = {"rtype": rrow[0] or "", "status": rrow[1] or "",
                       "links": len(links_data), "date": rrow[3] or "",
                       "recorded": rrow[4] or "", "artist": rrow[5] or "",
                       "tracks": rrow[6] or 0, "artist_id": rrow[7],
                       # Полные ссылки (label+url) — для кликабельного списка в
                       # режиме просмотра модалки; links_urls (только url,
                       # см. ниже) — для textarea в режиме редактирования.
                       "links_list": links_data}
            trows = conn.execute(
                "SELECT id, position, title, duration FROM release_track "
                "WHERE listing_id=? ORDER BY position", (listing_id,)).fetchall()
            release["tracks_list"] = [
                {"id": t[0], "position": t[1], "title": t[2] or "", "duration": t[3] or 0}
                for t in trows]
            release["links_urls"] = [l.get("url") for l in links_data]
    photo_ids = [p.strip() for p in (row[5] or "").split(",") if p.strip()]
    video = _parse_video(row[17] or "")
    if not video.get("video_type") and (row[8] or "") == "release":
        with db() as _c:
            video = {**video, **_video_from_release(_c, listing_id)}
    try:
        flex_data = json.loads(row[17]) if row[17] else {}
    except Exception:
        flex_data = {}
    if not isinstance(flex_data, dict):
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
        "release": release,
        "expires_at": row[19] or "", "archive_reason": row[20] or "",
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
            title = body.title.strip()
            if not title:
                raise HTTPException(400, "Название не может быть пустым")
            fields.append("title=?"); vals.append(title)
        if body.descr is not None:
            fields.append("descr=?"); vals.append(body.descr.strip() or None)
        if body.price is not None:
            fields.append("price=?"); vals.append(body.price.strip() or None)
        if body.contact is not None:
            contact = body.contact.strip()
            if not contact:
                raise HTTPException(400, "Контакт не может быть пустым")
            fields.append("contact=?"); vals.append(contact)
        if body.flex is not None:
            try:
                existing_flex = json.loads(row[1]) if row[1] else {}
            except Exception:
                existing_flex = {}
            if not isinstance(existing_flex, dict):
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
        # Релизы: подчищаем мету и треки, чтобы не оставлять сирот
        conn.execute("DELETE FROM release_meta WHERE listing_id=?", (listing_id,))
        conn.execute("DELETE FROM release_track WHERE listing_id=?", (listing_id,))
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
try:
    UPLOAD_CHAT_ID = int(
        config_value(_ROOT, "CATEGORY_ADMIN_UPLOAD_CHAT_ID", "519335258")
        or "519335258"
    )
except ValueError as exc:
    raise RuntimeError("CATEGORY_ADMIN_UPLOAD_CHAT_ID must be an integer") from exc
PHOTO_LIMIT_BY_TYPE = {
    "market": 3,
    "service": 3,
    # Карточки Афиши и релизов отправляют одну обложку через answer_photo.
    "events": 1,
    "release": 1,
}


def _listing_photo_limit(listing_type: str | None) -> int:
    return PHOTO_LIMIT_BY_TYPE.get((listing_type or "").strip().lower(), 0)


async def _tg_upload(kind: str, filename: str, data: bytes) -> str:
    """kind: 'photo' | 'video'. Возвращает file_id."""
    if not BOT_TOKEN:
        raise HTTPException(503, "Bot token not configured")
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
        row = conn.execute(
            "SELECT type, photo_file_id FROM listing WHERE id=?", (listing_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        limit = _listing_photo_limit(row[0])
        if not limit:
            raise HTTPException(400, "Для этого типа объявления фото не поддерживаются")
        photos = [p.strip() for p in (row[1] or "").split(",") if p.strip()]
        if len(photos) >= limit:
            raise HTTPException(400, f"Максимум {limit} фото")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Пустой файл")
    file_id = await _tg_upload("photo", filename, data)
    with db() as conn:
        row = conn.execute(
            "SELECT type, photo_file_id FROM listing WHERE id=?", (listing_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        limit = _listing_photo_limit(row[0])
        if not limit:
            raise HTTPException(409, "Для этого типа объявления фото не поддерживаются")
        photos = [p.strip() for p in (row[1] or "").split(",") if p.strip()]
        if len(photos) >= limit:
            raise HTTPException(409, f"Максимум {limit} фото")
        photos.append(file_id)
        conn.execute("UPDATE listing SET photo_file_id=? WHERE id=?", (",".join(photos), listing_id))
        conn.commit()
    return {"ok": True, "file_id": file_id, "photos": photos}


@app.post("/api/listing/{listing_id}/extend")
def extend_listing_admin(listing_id: int, days: int = 30):
    """Продление объявления админом. Семантика — как extend_listing в
    app/lifecycle.py: реактивация (снятие архива) + сдвиг expires_at;
    из глубокого карантина продлеваем от «сейчас»."""
    EXPIRABLE = {"market", "service", "vacancy"}
    SECTION_BY_TYPE = {"market": "market", "service": "services", "vacancy": "vacancy"}

    def _parse_dt(s):
        if not s:
            return None
        try:
            return datetime.datetime.fromisoformat(str(s).replace("T", " ").split("+")[0])
        except ValueError:
            return None

    with db() as conn:
        row = conn.execute(
            "SELECT type, expires_at, created_at, status FROM listing WHERE id=?",
            (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        ltype = (row[0] or "").strip()
        if ltype not in EXPIRABLE:
            raise HTTPException(400, "Этот тип объявления не продлевается")

        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        if (row[3] or "").strip() == "archived":
            # Реактивация архивного: срок строго от «сейчас» — как в
            # app/lifecycle.py, иначе закрытое с остатком дней накрутит срок
            base = now
        else:
            base = _parse_dt(row[1]) or ((_parse_dt(row[2]) or now) + datetime.timedelta(days=30))
            if base < now:
                base = now
        new_expires = base + datetime.timedelta(days=days)

        conn.execute("""
            UPDATE listing SET status='active', archive_reason=NULL, archived_at=NULL,
                   archived_by=NULL, archived_by_user_id=NULL, reminded_at=NULL,
                   expires_at=? WHERE id=?
        """, (new_expires.isoformat(sep=" "), listing_id))
        try:  # единый поток событий (словарь: app/analytics)
            conn.execute("""
                INSERT INTO analytics_events
                    (event_type, user_id, section, entity_type, entity_id, source, meta, created_at)
                VALUES ('listing_extended', NULL, ?, 'listing', ?, NULL, '{"by": "admin"}', ?)
            """, (SECTION_BY_TYPE.get(ltype, ltype), listing_id, now.isoformat(sep=" ")))
        except Exception as e:
            print(f"[extend] analytics_events: {e}")
    return {"ok": True, "expires_at": new_expires.isoformat(sep=" ")}


@app.post("/api/listing/{listing_id}/add_video")
async def add_video(listing_id: int, request: Request, filename: str = ""):
    if not BOT_TOKEN:
        raise HTTPException(503, "Bot token not configured")
    with db() as conn:
        row = conn.execute("SELECT type, flex FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Пустой файл")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Видео больше 20 МБ: Bot API не сможет отдать его в приложении")
    file_id = await _tg_upload("video", filename, data)
    with db() as conn:
        row = conn.execute("SELECT type, flex FROM listing WHERE id=?", (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Listing not found")
        if row[0] == "release":
            updated = conn.execute(
                "UPDATE release_meta SET video_file_id=?, video_file_unique_id=NULL "
                "WHERE listing_id=?",
                (file_id, listing_id),
            ).rowcount
            if not updated:
                raise HTTPException(409, "У релиза отсутствует release_meta")
        else:
            try:
                flex = json.loads(row[1]) if row[1] else {}
            except Exception:
                flex = {}
            if not isinstance(flex, dict):
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

_BYTE_RANGE_RE = re.compile(r"bytes=([0-9]*)-([0-9]*)", re.IGNORECASE)


def _parse_byte_range(raw: str, total: int) -> tuple[int, int]:
    """Parse one byte range, including ``bytes=-N`` suffix ranges."""
    match = _BYTE_RANGE_RE.fullmatch((raw or "").strip())
    if not match or total <= 0:
        raise HTTPException(
            416,
            "Invalid range",
            headers={"Content-Range": f"bytes */{max(total, 0)}"},
        )
    start_s, end_s = match.groups()
    if not start_s and not end_s:
        raise HTTPException(
            416,
            "Invalid range",
            headers={"Content-Range": f"bytes */{total}"},
        )
    if len(start_s) > 20 or len(end_s) > 20:
        raise HTTPException(
            416,
            "Invalid range",
            headers={"Content-Range": f"bytes */{total}"},
        )
    if not start_s:
        suffix = int(end_s)
        if suffix <= 0:
            raise HTTPException(
                416,
                "Invalid range",
                headers={"Content-Range": f"bytes */{total}"},
            )
        return max(0, total - suffix), total - 1

    start = int(start_s)
    end = int(end_s) if end_s else total - 1
    if start >= total or end < start:
        raise HTTPException(
            416,
            "Invalid range",
            headers={"Content-Range": f"bytes */{total}"},
        )
    return start, min(end, total - 1)


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
    if range_header:
        start, end = _parse_byte_range(range_header, total)
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


SECTION_ROOTS = {"market": 30, "service": 80, "vacancy": 90, "events": 100, "release": 393}
SECTION_NAMES = {"market": "Барахолка", "service": "Услуги", "vacancy": "Вакансии", "events": "Афиша", "release": "Релизы"}


ARTISTS_SORT_COLUMNS = {
    "name": "a.name", "type": "a.artist_type", "genres": "a.genres",
    "city": "a.city_text", "username": "bu.username",
    "releases": "releases", "opens": "opens",
    "created_at": "a.created_at", "status": "a.status",
}
ARTISTS_FILTER_FIELDS = ["name", "type", "genres", "city", "username", "status"]


@app.get("/api/artists")
def artists_list(offset: int = 0, limit: int = 24, q: str = "",
                  sort: str = "created_at", order: str = "desc", filters: str = ""):
    """Исполнители: список для вкладки админки (пагинация/сортировка/поиск)."""
    sort_col = ARTISTS_SORT_COLUMNS.get(sort, "a.created_at")
    sort_dir = "ASC" if order == "asc" else "DESC"
    try:
        f_dict = json.loads(filters) if filters else {}
    except Exception:
        f_dict = {}
    with db() as conn:
        try:
            rows = conn.execute(f"""
                SELECT a.id, a.name, a.artist_type, a.status, a.owner_user_id,
                       a.created_at, a.genres, a.city_text, bu.username,
                       (SELECT COUNT(*) FROM release_meta rm
                        WHERE rm.artist_id=a.id AND rm.status='published') AS releases,
                       (SELECT COUNT(*) FROM analytics_events ae
                        WHERE ae.event_type='artist_opened' AND ae.entity_id=a.id) AS opens
                FROM artist a
                LEFT JOIN BotUser bu ON bu.user_id=a.owner_user_id
                ORDER BY {sort_col} {sort_dir}
            """).fetchall()
        except Exception as e:
            return {"total": 0, "rows": [], "error": str(e)}
    result = [{
        "id": r[0], "name": r[1] or "", "type": r[2] or "", "status": r[3] or "",
        "owner_id": r[4], "created_at": r[5] or "",
        "genres": r[6] or "", "city": r[7] or "", "username": r[8] or "",
        "releases": r[9] or 0, "opens": r[10] or 0,
    } for r in rows]
    total, page = _casefold_filter_paginate(
        result, q, ["name", "username", "genres", "city"], f_dict, ARTISTS_FILTER_FIELDS, offset, limit)
    return {"total": total, "offset": offset, "limit": limit, "rows": page}


@app.get("/api/artist/{artist_id}/openers")
def artist_openers(artist_id: int):
    """Кто и сколько раз открывал карточку исполнителя — для клика по счётчику «Открытий»."""
    with db() as conn:
        rows = conn.execute("""
            SELECT ae.user_id, bu.username, bu.full_name, COUNT(*) AS cnt,
                   MAX(ae.created_at) AS last_open
            FROM analytics_events ae
            LEFT JOIN BotUser bu ON bu.user_id=ae.user_id
            WHERE ae.event_type='artist_opened' AND ae.entity_id=?
            GROUP BY ae.user_id
            ORDER BY cnt DESC
        """, (artist_id,)).fetchall()
    return {"rows": [{
        "user_id": r[0], "username": r[1] or "", "full_name": r[2] or "",
        "count": r[3] or 0, "last_open": r[4] or "",
    } for r in rows]}


RELEASES_SORT_COLUMNS = {
    "title": "l.title", "artist": "a.name", "rtype": "rm.release_type",
    "tracks": "tracks", "opens": "opens", "username": "bu.username",
    "created_at": "rm.created_at", "status": "rm.status",
}
RELEASES_FILTER_FIELDS = ["title", "artist", "rtype", "status", "username"]


@app.get("/api/releases")
def releases_list(offset: int = 0, limit: int = 24, q: str = "",
                   sort: str = "created_at", order: str = "desc", filters: str = ""):
    """Релизы: список для отдельной вкладки админки (пагинация/сортировка/поиск)."""
    sort_col = RELEASES_SORT_COLUMNS.get(sort, "rm.created_at")
    sort_dir = "ASC" if order == "asc" else "DESC"
    try:
        f_dict = json.loads(filters) if filters else {}
    except Exception:
        f_dict = {}
    with db() as conn:
        try:
            rows = conn.execute(f"""
                SELECT l.id, l.title, rm.release_type, rm.status, rm.release_date,
                       l.photo_file_id, l.created_at, l.owner_id, bu.username,
                       a.id, a.name,
                       (SELECT COUNT(*) FROM release_track rt WHERE rt.listing_id=l.id) AS tracks,
                       (SELECT COUNT(*) FROM listing_views lv
                        WHERE lv.listing_id=l.id AND lv.action='open') AS opens
                FROM release_meta rm
                JOIN listing l ON l.id=rm.listing_id
                LEFT JOIN artist a ON a.id=rm.artist_id
                LEFT JOIN BotUser bu ON bu.user_id=l.owner_id
                WHERE rm.status != 'deleted'
                ORDER BY {sort_col} {sort_dir}
            """).fetchall()
        except Exception as e:
            return {"total": 0, "rows": [], "error": str(e)}
    result = [{
        "id": r[0], "title": r[1] or "", "rtype": r[2] or "", "status": r[3] or "",
        "date": r[4] or "", "photo": (r[5] or "").split(",")[0].strip(),
        "created_at": r[6] or "", "owner_id": r[7], "username": r[8] or "",
        "artist_id": r[9], "artist": r[10] or "—", "tracks": r[11] or 0,
        "opens": r[12] or 0,
    } for r in rows]
    total, page = _casefold_filter_paginate(
        result, q, ["title", "artist", "username"], f_dict, RELEASES_FILTER_FIELDS, offset, limit)
    return {"total": total, "offset": offset, "limit": limit, "rows": page}


@app.get("/api/listing/{listing_id}/openers")
def listing_openers(listing_id: int):
    """Кто и сколько раз открывал объявление/релиз — для клика по счётчику «Просмотры»."""
    with db() as conn:
        rows = conn.execute("""
            SELECT lv.user_id, bu.username, bu.full_name, COUNT(*) AS cnt,
                   datetime(MAX(lv.created_at), 'unixepoch', 'localtime') AS last_open
            FROM listing_views lv
            LEFT JOIN BotUser bu ON bu.user_id=lv.user_id
            WHERE lv.listing_id=? AND lv.action='open'
            GROUP BY lv.user_id
            ORDER BY cnt DESC
        """, (listing_id,)).fetchall()
    return {"rows": [{
        "user_id": r[0], "username": r[1] or "", "full_name": r[2] or "",
        "count": r[3] or 0, "last_open": r[4] or "",
    } for r in rows]}


@app.post("/api/release/{listing_id}/toggle_status")
def release_toggle_status(listing_id: int):
    """Скрыть/показать релиз (release_meta.status, как «Скрыть» в боте)."""
    with db() as conn:
        row = conn.execute("SELECT status FROM release_meta WHERE listing_id=?",
                           (listing_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Release not found")
        new_status = "hidden" if (row[0] or "published") == "published" else "published"
        conn.execute("UPDATE release_meta SET status=? WHERE listing_id=?",
                     (new_status, listing_id))
    return {"ok": True, "status": new_status}


@app.get("/api/artist/{artist_id}")
def artist_detail(artist_id: int):
    """Карточка исполнителя + его релизы (для модалки вкладки)."""
    with db() as conn:
        a = conn.execute("""
            SELECT a.id, a.name, a.artist_type, a.status, a.owner_user_id,
                   a.created_at, a.genres, a.city_text, a.descr, a.contact,
                   a.links, a.photo_file_id, bu.username
            FROM artist a LEFT JOIN BotUser bu ON bu.user_id=a.owner_user_id
            WHERE a.id=?""", (artist_id,)).fetchone()
        if not a:
            raise HTTPException(404, "Artist not found")
        rel_rows = conn.execute("""
            SELECT l.id, l.title, rm.release_type, rm.status, l.photo_file_id,
                   (SELECT COUNT(*) FROM listing_views lv
                    WHERE lv.listing_id=l.id AND lv.action='open') AS opens,
                   l.created_at
            FROM release_meta rm JOIN listing l ON l.id=rm.listing_id
            WHERE rm.artist_id=? AND rm.status != 'deleted'
            ORDER BY rm.created_at DESC""", (artist_id,)).fetchall()
        try:
            links = json.loads(a[10]) if a[10] else []
        except Exception:
            links = []
    return {
        "id": a[0], "name": a[1] or "", "type": a[2] or "", "status": a[3] or "",
        "owner_id": a[4], "created_at": (a[5] or "")[:10],
        "genres": a[6] or "", "city": a[7] or "", "descr": a[8] or "",
        "contact": a[9] or "", "links": links, "photo_file_id": a[11] or "",
        "username": a[12] or "",
        "releases": [{"id": r[0], "title": r[1] or "", "rtype": r[2] or "",
                      "status": r[3] or "", "photo": (r[4] or "").split(",")[0].strip(),
                      "opens": r[5] or 0, "created_at": (r[6] or "")[:10]}
                     for r in rel_rows],
    }


@app.post("/api/artist/{artist_id}/toggle_status")
def artist_toggle_status(artist_id: int):
    """Скрыть/показать карточку исполнителя."""
    with db() as conn:
        row = conn.execute("SELECT status FROM artist WHERE id=?", (artist_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Artist not found")
        new_status = "hidden" if (row[0] or "active") == "active" else "active"
        conn.execute("UPDATE artist SET status=? WHERE id=?", (new_status, artist_id))
    return {"ok": True, "status": new_status}


# ── Полноценное редактирование музыкального слоя из админки ──

_ADMIN_LINK_LABELS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube", "spotify.com": "Spotify",
    "music.yandex": "Яндекс Музыка", "music.apple": "Apple Music",
    "bandcamp.com": "Bandcamp", "soundcloud.com": "SoundCloud",
    "vk.com": "VK Музыка", "vk.ru": "VK Музыка", "instagram.com": "Instagram",
    "facebook.com": "Facebook", "t.me": "Telegram",
}


def _validated_http_url(raw: str) -> str:
    """Return a safe absolute HTTP(S) URL or reject the whole update."""
    value = (raw or "").strip()
    if (
        not value
        or any(ord(ch) < 32 for ch in value)
        or any(ch in value for ch in {'"', "'", "<", ">", "\\"})
    ):
        raise HTTPException(400, f"Некорректная ссылка: {raw!r}")
    try:
        parsed = urlparse(value)
        port = parsed.port  # validates malformed ports as well
    except ValueError:
        raise HTTPException(400, f"Некорректная ссылка: {raw!r}")
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None and not (1 <= port <= 65535)
    ):
        raise HTTPException(400, f"Разрешены только полные ссылки http:// или https://: {raw!r}")
    return value


def _links_json_from_text(text_val: str | None) -> str | None:
    """Строки/пробелы с URL → JSON [{label,url}] (метка по домену)."""
    if not text_val or not text_val.strip():
        return None
    raw_urls = text_val.replace(",", " ").split()
    if len(raw_urls) > 10:
        raise HTTPException(400, "Можно сохранить не больше 10 ссылок")
    links = []
    for raw_url in raw_urls:
        u = _validated_http_url(raw_url)
        label = "Ссылка"
        for dom, lb in _ADMIN_LINK_LABELS.items():
            if dom in u.lower():
                label = lb
                break
        links.append({"label": label, "url": u})
    return json.dumps(links, ensure_ascii=False) if links else None


class ArtistPatch(BaseModel):
    name: str | None = None
    type: str | None = None
    genres: str | None = None
    city: str | None = None
    descr: str | None = None
    contact: str | None = None
    links_text: str | None = None


@app.patch("/api/artist/{artist_id}")
def artist_patch(artist_id: int, body: ArtistPatch):
    sets, vals = [], []
    if body.name is not None and body.name.strip():
        sets.append("name=?"); vals.append(body.name.strip()[:128])
    if body.type is not None and body.type.strip():
        sets.append("artist_type=?"); vals.append(body.type.strip()[:32])
    for col, v, lim in (("genres", body.genres, 128), ("city_text", body.city, 64),
                        ("descr", body.descr, 600), ("contact", body.contact, 128)):
        if v is not None:
            sets.append(f"{col}=?"); vals.append(v.strip()[:lim] or None)
    if body.links_text is not None:
        sets.append("links=?"); vals.append(_links_json_from_text(body.links_text))
    if not sets:
        return {"ok": True}
    with db() as conn:
        if not conn.execute("SELECT 1 FROM artist WHERE id=?", (artist_id,)).fetchone():
            raise HTTPException(404, "Artist not found")
        conn.execute(f"UPDATE artist SET {', '.join(sets)} WHERE id=?", vals + [artist_id])
    return {"ok": True}


@app.post("/api/artist/{artist_id}/photo")
async def artist_photo_upload(artist_id: int, request: Request, filename: str = ""):
    with db() as conn:
        if not conn.execute("SELECT 1 FROM artist WHERE id=?", (artist_id,)).fetchone():
            raise HTTPException(404, "Artist not found")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty file")
    file_id = await _tg_upload("photo", filename, data)
    with db() as conn:
        if not conn.execute(
            "UPDATE artist SET photo_file_id=? WHERE id=?", (file_id, artist_id)
        ).rowcount:
            raise HTTPException(404, "Artist not found")
    return {"ok": True, "file_id": file_id}


class ReleasePatch(BaseModel):
    rtype: str | None = None
    date: str | None = None
    recorded: str | None = None
    links_text: str | None = None
    artist_id: int | None = None


class ReleaseTrackRename(BaseModel):
    id: int
    title: str


class ReleaseFullPatch(BaseModel):
    """One transaction for the listing, release metadata and track names."""
    title: str
    descr: str | None = None
    price: str | None = None
    contact: str
    flex: dict[str, Any] | None = None
    release: ReleasePatch
    tracks: list[ReleaseTrackRename] = Field(default_factory=list)


@app.patch("/api/release/{listing_id}/full")
def release_full_patch(listing_id: int, body: ReleaseFullPatch):
    title = body.title.strip()
    contact = body.contact.strip()
    if not title:
        raise HTTPException(400, "Название не может быть пустым")
    if not contact:
        raise HTTPException(400, "Контакт не может быть пустым")

    release = body.release
    allowed_types = {"single", "ep", "album", "clip", "live"}
    if release.rtype is not None and release.rtype not in allowed_types:
        raise HTTPException(400, "Неизвестный тип релиза")
    links_json = (
        _links_json_from_text(release.links_text)
        if release.links_text is not None else None
    )

    normalized_tracks: list[tuple[int, str]] = []
    seen_track_ids: set[int] = set()
    for track in body.tracks:
        if track.id in seen_track_ids:
            raise HTTPException(400, "Один трек передан дважды")
        seen_track_ids.add(track.id)
        track_title = track.title.strip()[:255]
        if not track_title:
            raise HTTPException(400, "Название трека не может быть пустым")
        normalized_tracks.append((track.id, track_title))

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        listing = conn.execute(
            "SELECT type, flex FROM listing WHERE id=?", (listing_id,)
        ).fetchone()
        if not listing:
            raise HTTPException(404, "Listing not found")
        if listing["type"] != "release":
            raise HTTPException(400, "Это объявление не является релизом")
        if not conn.execute(
            "SELECT 1 FROM release_meta WHERE listing_id=?", (listing_id,)
        ).fetchone():
            raise HTTPException(409, "У релиза отсутствует release_meta")
        if release.artist_id is not None and not conn.execute(
            "SELECT 1 FROM artist WHERE id=?", (release.artist_id,)
        ).fetchone():
            raise HTTPException(404, "Artist not found")

        for track_id, _ in normalized_tracks:
            track_row = conn.execute(
                "SELECT listing_id FROM release_track WHERE id=?", (track_id,)
            ).fetchone()
            if not track_row:
                raise HTTPException(404, f"Track {track_id} not found")
            if track_row["listing_id"] != listing_id:
                raise HTTPException(400, "Трек не принадлежит этому релизу")

        listing_sets = ["title=?", "contact=?"]
        listing_vals: list[Any] = [title, contact]
        if body.descr is not None:
            listing_sets.append("descr=?")
            listing_vals.append(body.descr.strip() or None)
        if body.price is not None:
            listing_sets.append("price=?")
            listing_vals.append(body.price.strip() or None)
        if body.flex is not None:
            try:
                existing_flex = json.loads(listing["flex"]) if listing["flex"] else {}
            except (TypeError, ValueError):
                existing_flex = {}
            if not isinstance(existing_flex, dict):
                existing_flex = {}
            existing_flex.update(body.flex)
            listing_sets.append("flex=?")
            listing_vals.append(json.dumps(existing_flex, ensure_ascii=False))
        conn.execute(
            f"UPDATE listing SET {', '.join(listing_sets)} WHERE id=?",
            listing_vals + [listing_id],
        )

        release_sets: list[str] = []
        release_vals: list[Any] = []
        for column, value in (
            ("release_type", release.rtype),
            ("release_date", release.date.strip()[:32] or None if release.date is not None else None),
            ("recorded_at", release.recorded.strip()[:128] or None if release.recorded is not None else None),
            ("links", links_json),
            ("artist_id", release.artist_id),
        ):
            supplied = {
                "release_type": release.rtype is not None,
                "release_date": release.date is not None,
                "recorded_at": release.recorded is not None,
                "links": release.links_text is not None,
                "artist_id": release.artist_id is not None,
            }[column]
            if supplied:
                release_sets.append(f"{column}=?")
                release_vals.append(value)
        if release_sets:
            conn.execute(
                f"UPDATE release_meta SET {', '.join(release_sets)} WHERE listing_id=?",
                release_vals + [listing_id],
            )

        for track_id, track_title in normalized_tracks:
            conn.execute(
                "UPDATE release_track SET title=? WHERE id=?",
                (track_title, track_id),
            )
        conn.commit()
    return {"ok": True}


@app.patch("/api/release/{listing_id}")
def release_patch(listing_id: int, body: ReleasePatch):
    sets, vals = [], []
    if body.rtype is not None and body.rtype in ("single", "ep", "album", "clip", "live"):
        sets.append("release_type=?"); vals.append(body.rtype)
    if body.date is not None:
        sets.append("release_date=?"); vals.append(body.date.strip()[:32] or None)
    if body.recorded is not None:
        sets.append("recorded_at=?"); vals.append(body.recorded.strip()[:128] or None)
    if body.links_text is not None:
        sets.append("links=?"); vals.append(_links_json_from_text(body.links_text))
    with db() as conn:
        if body.artist_id is not None:
            if not conn.execute("SELECT 1 FROM artist WHERE id=?", (body.artist_id,)).fetchone():
                raise HTTPException(404, "Artist not found")
            sets.append("artist_id=?"); vals.append(body.artist_id)
        if not sets:
            return {"ok": True}
        if not conn.execute("SELECT 1 FROM release_meta WHERE listing_id=?", (listing_id,)).fetchone():
            raise HTTPException(404, "Release not found")
        conn.execute(f"UPDATE release_meta SET {', '.join(sets)} WHERE listing_id=?",
                     vals + [listing_id])
    return {"ok": True}


class TrackPatch(BaseModel):
    title: str


@app.patch("/api/release_track/{track_id}")
def track_patch(track_id: int, body: TrackPatch):
    title = body.title.strip()[:255]
    if not title:
        raise HTTPException(400, "Название трека не может быть пустым")
    with db() as conn:
        if not conn.execute("SELECT 1 FROM release_track WHERE id=?", (track_id,)).fetchone():
            raise HTTPException(404, "Track not found")
        conn.execute("UPDATE release_track SET title=? WHERE id=?",
                     (title, track_id))
    return {"ok": True}


@app.delete("/api/release_track/{track_id}")
def track_delete(track_id: int):
    with db() as conn:
        row = conn.execute(
            """SELECT rt.listing_id, rm.video_file_id, rm.links,
                      (SELECT COUNT(*) FROM release_track all_rt
                       WHERE all_rt.listing_id=rt.listing_id) AS track_count
               FROM release_track rt
               JOIN release_meta rm ON rm.listing_id=rt.listing_id
               WHERE rt.id=?""",
            (track_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Track not found")
        try:
            links = json.loads(row["links"] or "[]")
        except (TypeError, ValueError):
            links = []
        has_links = False
        if isinstance(links, list):
            for item in links:
                if not isinstance(item, dict):
                    continue
                try:
                    _validated_http_url(str(item.get("url") or ""))
                except HTTPException:
                    continue
                has_links = True
                break
        if row["track_count"] <= 1 and not row["video_file_id"] and not has_links:
            raise HTTPException(
                409,
                "Нельзя удалить единственный медиаисточник релиза: "
                "сначала добавьте трек, видео или ссылку",
            )
        conn.execute("DELETE FROM release_track WHERE id=?", (track_id,))
        rest = conn.execute("SELECT id FROM release_track WHERE listing_id=? "
                            "ORDER BY position", (row[0],)).fetchall()
        for i, (tid,) in enumerate(rest, start=1):
            conn.execute("UPDATE release_track SET position=? WHERE id=?", (i, tid))
    return {"ok": True}


async def _tg_upload_audio(filename: str, data: bytes) -> dict:
    """Загрузка аудио в Telegram (как _tg_upload, но sendAudio → весь объект)."""
    if not BOT_TOKEN:
        raise HTTPException(503, "Bot token not configured")
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio",
            data={"chat_id": UPLOAD_CHAT_ID, "disable_notification": "true"},
            files={"audio": (filename or "track.mp3", data)},
        )
        resp = r.json()
        if not resp.get("ok"):
            raise HTTPException(502, f"Telegram: {resp.get('description', 'upload failed')}")
        msg = resp["result"]
        try:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage",
                data={"chat_id": UPLOAD_CHAT_ID, "message_id": msg["message_id"]},
            )
        except Exception:
            pass
        return msg["audio"]


@app.post("/api/release/{listing_id}/track")
async def track_add(listing_id: int, request: Request, filename: str = ""):
    max_tracks = 50
    with db() as conn:
        if not conn.execute(
            "SELECT 1 FROM release_meta WHERE listing_id=?", (listing_id,)
        ).fetchone():
            raise HTTPException(404, "Release not found")
        if conn.execute(
            "SELECT COUNT(*) FROM release_track WHERE listing_id=?", (listing_id,)
        ).fetchone()[0] >= max_tracks:
            raise HTTPException(400, f"Максимум {max_tracks} треков")
    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty file")
    audio = await _tg_upload_audio(filename, data)
    with db() as conn:
        if not conn.execute("SELECT 1 FROM release_meta WHERE listing_id=?",
                            (listing_id,)).fetchone():
            raise HTTPException(404, "Release not found")
        n = conn.execute("SELECT COUNT(*) FROM release_track WHERE listing_id=?",
                         (listing_id,)).fetchone()[0] or 0
        if n >= max_tracks:
            raise HTTPException(409, f"Максимум {max_tracks} треков")
        conn.execute(
            "INSERT INTO release_track (listing_id, position, title, file_id, "
            "file_unique_id, duration, file_name, mime_type) VALUES (?,?,?,?,?,?,?,?)",
            (listing_id, n + 1,
             (audio.get("title") or audio.get("file_name") or filename or f"Трек {n+1}")[:255],
             audio["file_id"], audio.get("file_unique_id"), audio.get("duration"),
             audio.get("file_name"), audio.get("mime_type")))
    return {"ok": True}


# ─────────────────────── Обратная связь (вкладка «✉️ Обратная связь») ──────
# Тот же функционал, что в Telegram-Админ-панели (app/routers/admin_panel.py,
# app/routers/feedback.py): единый источник правды — таблица feedback,
# синхронизация между ботом и веб-админкой не нужна, читают/пишут одну БД.

async def _send_telegram_message(chat_id: int, text_: str, reply_markup: dict | None) -> dict:
    """Отправка сообщения через Telegram Bot API напрямую (веб-админка —
    отдельный процесс от бота, поэтому не может дёрнуть его aiogram-объект)."""
    if not BOT_TOKEN:
        return {"ok": False, "description": "Bot token not configured"}
    payload = {"chat_id": chat_id, "text": text_, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", data=payload)
        return r.json()


def _feedback_reply_message(original: str, reply_text: str) -> str:
    """Тот же формат, что и в боте (app/routers/feedback.py:_deliver_reply):
    сначала вопрос пользователя, потом ответ — чтобы легче читалось."""
    return (
        "✉️ <b>Ответ администратора</b>\n\n"
        f"<i>На Ваше сообщение:</i>\n«{html.escape(original or '')}»\n\n"
        f"➡️ {html.escape(reply_text)}"
    )


def _feedback_main_menu_markup(conn) -> dict:
    row = conn.execute("SELECT text, callback_data FROM menu WHERE code='main_menu'").fetchone()
    btn_text = (row[0] if row and row[0] else "Главное меню.")
    btn_cb = (row[1] if row and row[1] else "main_menu")
    return {"inline_keyboard": [[{"text": btn_text, "callback_data": btn_cb}]]}


@app.get("/api/feedback")
def feedback_list(unanswered: int = Query(0), offset: int = Query(0), limit: int = Query(20)):
    with db() as conn:
        where = "WHERE needs_reply=1 AND answered_at IS NULL" if unanswered else ""
        total = conn.execute(f"SELECT COUNT(*) FROM feedback {where}").fetchone()[0] or 0
        rows = conn.execute(
            f"SELECT id, user_id, username, message, created_at, is_read, needs_reply, answered_at "
            f"FROM feedback {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return {
        "total": total,
        "rows": [{
            "id": r[0], "user_id": r[1], "username": r[2] or "",
            "message": r[3] or "", "created_at": r[4] or "",
            "is_read": bool(r[5]), "needs_reply": bool(r[6]),
            "answered": r[7] is not None,
        } for r in rows],
    }


@app.get("/api/feedback/{fid}")
def feedback_get(fid: int):
    with db() as conn:
        row = conn.execute(
            "SELECT id, user_id, username, message, answer_text, answered_at, "
            "needs_reply, created_at, is_read FROM feedback WHERE id=?", (fid,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Обращение не найдено")
        if not row[8]:
            conn.execute("UPDATE feedback SET is_read=1 WHERE id=?", (fid,))
    return {
        "id": row[0], "user_id": row[1], "username": row[2] or "",
        "message": row[3] or "", "answer_text": row[4], "answered_at": row[5],
        "needs_reply": bool(row[6]), "created_at": row[7] or "",
    }


class FeedbackReplyBody(BaseModel):
    text: str


@app.post("/api/feedback/{fid}/reply")
async def feedback_reply(fid: int, body: FeedbackReplyBody):
    reply_text = (body.text or "").strip()
    if not reply_text:
        raise HTTPException(400, "Пустой ответ")
    with db() as conn:
        row = conn.execute(
            "SELECT user_id, message FROM feedback WHERE id=?", (fid,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Обращение не найдено")
        target_user_id, original = row[0], row[1]
        reply_markup = _feedback_main_menu_markup(conn)

    text_html = _feedback_reply_message(original, reply_text)
    resp = await _send_telegram_message(target_user_id, text_html, reply_markup)

    if not resp.get("ok"):
        return {"ok": False, "detail": resp.get("description") or "Не удалось доставить ответ"}

    with db() as conn:
        conn.execute(
            "UPDATE feedback SET answered_at=CURRENT_TIMESTAMP, answer_text=? WHERE id=?",
            (reply_text, fid),
        )
        # Ответ попадёт под обычную чат-гигиену бота — сметётся при
        # следующей навигации пользователя, как и ответы через сам бот.
        msg_id = (resp.get("result") or {}).get("message_id")
        if msg_id:
            conn.execute(
                "INSERT INTO botmessage (chat_id, message_id, created_at) VALUES (?, ?, ?)",
                (target_user_id, msg_id,
                 datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()),
            )
    return {"ok": True}


@app.delete("/api/feedback/{fid}")
def feedback_delete(fid: int):
    with db() as conn:
        conn.execute("DELETE FROM feedback WHERE id=?", (fid,))
    return {"ok": True}


# ─────────────────────────── Тексты (BotText + menu) ───────────────────────────
# Редактор текстов бота: BotText (сообщения/кнопки экранов) и menu (кнопки
# главного меню). Обычные textarea, без WYSIWYG — тексты содержат Telegram
# HTML-разметку (<b>, <i>, <a href>), визуальный редактор её бы поломал.

@app.get("/api/texts")
def texts_list(q: str = Query(""), offset: int = Query(0), limit: int = Query(50)):
    q = (q or "").strip()
    with db() as conn:
        if q:
            like = f"%{q}%"
            where = "WHERE code LIKE ? OR title LIKE ? OR text_ru LIKE ?"
            params: tuple = (like, like, like)
        else:
            where = ""
            params = ()
        total = conn.execute(f"SELECT COUNT(*) FROM BotText {where}", params).fetchone()[0] or 0
        rows = conn.execute(
            f"SELECT id, code, title, text_ru, text_en, text_kk, updated_at FROM BotText {where} "
            f"ORDER BY code LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    return {
        "total": total,
        "rows": [{
            "id": r[0], "code": r[1], "title": r[2] or "",
            "text_ru": r[3] or "", "text_en": r[4] or "", "text_kk": r[5] or "",
            "updated_at": r[6] or "",
        } for r in rows],
    }


@app.get("/api/texts/{code}")
def texts_get(code: str):
    with db() as conn:
        row = conn.execute(
            "SELECT id, code, title, text_ru, text_en, text_kk, updated_at FROM BotText WHERE code=?", (code,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Текст не найден")
    return {
        "id": row[0], "code": row[1], "title": row[2] or "",
        "text_ru": row[3] or "", "text_en": row[4] or "", "text_kk": row[5] or "",
        "updated_at": row[6] or "",
    }


class TextUpdateBody(BaseModel):
    title: str = ""
    text_ru: str = ""
    text_en: str = ""
    text_kk: str = ""


@app.post("/api/texts/{code}")
def texts_update(code: str, body: TextUpdateBody):
    with db() as conn:
        cur = conn.execute(
            "UPDATE BotText SET title=?, text_ru=?, text_en=?, text_kk=?, updated_at=CURRENT_TIMESTAMP WHERE code=?",
            (body.title, body.text_ru, body.text_en, body.text_kk, code),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Текст не найден")
    return {"ok": True}


@app.get("/api/menu")
def menu_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, code, parent_code, text, text_en, text_kk, icon, order_num, visible "
            "FROM menu ORDER BY (parent_code IS NOT NULL), parent_code, order_num, code"
        ).fetchall()
    return {"rows": [{
        "id": r[0], "code": r[1], "parent_code": r[2] or "",
        "text": r[3] or "", "text_en": r[4] or "", "text_kk": r[5] or "", "icon": r[6] or "",
        "order_num": r[7] or 0, "visible": bool(r[8]),
    } for r in rows]}


class MenuUpdateBody(BaseModel):
    text: str = ""
    text_en: str = ""
    text_kk: str = ""
    icon: str = ""
    visible: bool = True


@app.post("/api/menu/{item_id}")
def menu_update(item_id: int, body: MenuUpdateBody):
    with db() as conn:
        cur = conn.execute(
            "UPDATE menu SET text=?, text_en=?, text_kk=?, icon=?, visible=? WHERE id=?",
            (body.text, body.text_en, body.text_kk, body.icon, 1 if body.visible else 0, item_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Пункт меню не найден")
    return {"ok": True}


# ─────────────────────────── Настройки (feature_flags) ───────────────────────────
# Рубильники функций бота. ВАЖНО: category_admin.py и бот — разные процессы,
# у is_enabled() в app/features.py свой in-memory кэш на 30 сек (CACHE_TTL) —
# правка здесь применяется в боте не мгновенно, а в течение этого окна.

@app.get("/api/feature_flags")
def feature_flags_list():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, key, enabled, audience, note, updated_at FROM feature_flags ORDER BY key"
        ).fetchall()
    return {"rows": [{
        "id": r[0], "key": r[1], "enabled": bool(r[2]),
        "audience": r[3] or "all", "note": r[4] or "", "updated_at": r[5] or "",
    } for r in rows]}


class FeatureFlagUpdateBody(BaseModel):
    enabled: bool = False
    audience: str = "all"
    note: str = ""


@app.post("/api/feature_flags/{flag_id}")
def feature_flags_update(flag_id: int, body: FeatureFlagUpdateBody):
    audience = (body.audience or "all").strip() or "all"
    with db() as conn:
        cur = conn.execute(
            "UPDATE feature_flags SET enabled=?, audience=?, note=?, updated_at=datetime('now') WHERE id=?",
            (1 if body.enabled else 0, audience, body.note, flag_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Флаг не найден")
    return {"ok": True}


# ─────────────────────────── Пользователи (BotUser) ───────────────────────────
# is_muted — ограничение ТОЛЬКО на публикацию нового контента (Барахолка/
# Услуги/Вакансии/Афиша/Исполнители/Релизы), просмотр разделов остаётся
# доступен. Обратная связь не блокируется — канал апелляции.

@app.get("/api/users")
def users_list(q: str = Query(""), offset: int = Query(0), limit: int = Query(50)):
    # SQLite LIKE регистронезависим только для ASCII — кириллицу не сворачивает
    # (нужно ICU-расширение, которого нет). Поэтому фильтруем в Python через
    # casefold(), а не SQL LIKE: таблица пользователей небольшая, полная
    # выборка с фильтрацией — простой и корректный для любого языка вариант.
    q = (q or "").strip()
    with db() as conn:
        all_rows = conn.execute(
            'SELECT id, user_id, username, full_name, first_seen, last_seen, is_muted '
            'FROM "BotUser" ORDER BY last_seen DESC'
        ).fetchall()
    if q:
        qcf = q.casefold()
        q_digits = q.lstrip("-")
        def matches(r):
            if q_digits.isdigit() and str(r[1]) == q_digits:
                return True
            return qcf in (r[2] or "").casefold() or qcf in (r[3] or "").casefold()
        filtered = [r for r in all_rows if matches(r)]
    else:
        filtered = all_rows
    total = len(filtered)
    rows = filtered[offset:offset + limit]
    return {
        "total": total,
        "rows": [{
            "id": r[0], "user_id": r[1], "username": r[2] or "",
            "full_name": r[3] or "", "first_seen": r[4] or "", "last_seen": r[5] or "",
            "is_muted": bool(r[6]),
        } for r in rows],
    }


class UserMuteBody(BaseModel):
    is_muted: bool = False


@app.post("/api/users/{user_id}/mute")
def user_mute_update(user_id: int, body: UserMuteBody):
    with db() as conn:
        cur = conn.execute(
            'UPDATE "BotUser" SET is_muted=? WHERE user_id=?',
            (1 if body.is_muted else 0, user_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Пользователь не найден")
    return {"ok": True}


# ─────────────────────────── Рассылка (broadcast) ───────────────────────────
# Идёт напрямую через Telegram Bot API (category_admin.py — отдельный процесс
# от бота, не может воспользоваться его aiogram-объектом), с троттлингом
# ~20 сообщений/сек — с запасом от официального лимита Telegram (~30/сек для
# разных чатов). Одна рассылка за раз (не позволяем запустить вторую поверх
# ещё не завершённой) — простое ограничение через in-memory-флаг процесса.

_broadcast_state: dict = {"running": False, "sent": 0, "failed": 0, "total": 0}


@app.get("/api/broadcast/audience")
def broadcast_audience():
    with db() as conn:
        total = conn.execute('SELECT COUNT(*) FROM "BotUser"').fetchone()[0] or 0
    return {"total": total, "default_test_id": ADMIN_IDS[0] if ADMIN_IDS else None}


class BroadcastTestBody(BaseModel):
    text_ru: str
    target_id: int


@app.post("/api/broadcast/test")
async def broadcast_test(body: BroadcastTestBody):
    text_ru = (body.text_ru or "").strip()
    if not text_ru:
        raise HTTPException(400, "Пустой текст")
    resp = await _send_telegram_message(body.target_id, text_ru, _HIDE_MESSAGE_MARKUP)
    if not resp.get("ok"):
        return {"ok": False, "detail": resp.get("description") or "Не удалось отправить"}
    msg_id = (resp.get("result") or {}).get("message_id")
    if msg_id:
        with db() as conn:
            _register_bot_message_sync(conn, body.target_id, msg_id)
    return {"ok": True}


class BroadcastSendBody(BaseModel):
    text_ru: str
    user_ids: list[int] = []


# Кнопка «Скрыть сообщение» под каждой рассылкой — переиспользует уже
# существующий в боте обработчик delmsg: (app/routers/market_add.py,
# delete_msg_cb): он удаляет cb.message независимо от числа после
# двоеточия, поэтому плейсхолдер "0" достаточен — не нужно знать
# message_id заранее и делать второй API-вызов (editMessageReplyMarkup).
_HIDE_MESSAGE_MARKUP = {"inline_keyboard": [[{"text": "🗑 Скрыть сообщение", "callback_data": "delmsg:0"}]]}


def _register_bot_message_sync(conn, chat_id: int, message_id: int) -> None:
    """Гигиена чата: сообщение попадёт под обычную зачистку бота при
    следующей навигации пользователя — тот же приём, что и для ответов
    в обратной связи (см. feedback_reply)."""
    conn.execute(
        "INSERT INTO botmessage (chat_id, message_id, created_at) VALUES (?, ?, ?)",
        (chat_id, message_id, datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat()),
    )


async def _run_broadcast(text_ru: str, user_ids: list[int]) -> None:
    success = 0
    failed = 0
    for uid in user_ids:
        try:
            resp = await _send_telegram_message(uid, text_ru, _HIDE_MESSAGE_MARKUP)
            if resp.get("ok"):
                success += 1
                msg_id = (resp.get("result") or {}).get("message_id")
                if msg_id:
                    with db() as conn:
                        _register_bot_message_sync(conn, uid, msg_id)
            else:
                failed += 1
        except Exception:
            failed += 1
        _broadcast_state["sent"] = success
        _broadcast_state["failed"] = failed
        await asyncio.sleep(0.05)  # ~20 сообщений/сек — с запасом от лимита Telegram
    _broadcast_state["running"] = False
    with db() as conn:
        conn.execute(
            "INSERT INTO broadcast_log (text_ru, sent_at, total, success, failed) VALUES (?, datetime('now'), ?, ?, ?)",
            (text_ru, len(user_ids), success, failed),
        )


@app.post("/api/broadcast/send")
async def broadcast_send(body: BroadcastSendBody):
    if _broadcast_state["running"]:
        raise HTTPException(409, "Рассылка уже выполняется")
    text_ru = (body.text_ru or "").strip()
    if not text_ru:
        raise HTTPException(400, "Пустой текст")
    # Получателей выбирает фронтенд (режим "всем" / "только отмеченным" /
    # "всем, кроме отмеченных") — бэкенд просто рассылает по присланному
    # списку ID, не пересчитывает аудиторию заново.
    user_ids = list(dict.fromkeys(body.user_ids))  # без дублей, порядок сохранён
    if not user_ids:
        raise HTTPException(400, "Нет получателей")
    _broadcast_state.update({"running": True, "sent": 0, "failed": 0, "total": len(user_ids)})
    asyncio.create_task(_run_broadcast(text_ru, user_ids))
    return {"ok": True, "total": len(user_ids)}


@app.get("/api/broadcast/status")
def broadcast_status():
    return dict(_broadcast_state)


@app.get("/api/broadcast/history")
def broadcast_history():
    with db() as conn:
        rows = conn.execute(
            "SELECT id, text_ru, sent_at, total, success, failed FROM broadcast_log ORDER BY sent_at DESC LIMIT 20"
        ).fetchall()
    return {"rows": [{
        "id": r[0], "text_ru": r[1], "sent_at": r[2],
        "total": r[3], "success": r[4], "failed": r[5],
    } for r in rows]}


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
        if not video.get("video_type") and (r[8] or "") == "release":
            video = {**video, **_video_from_release(conn, r[0])}
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


# Разрешённые колонки сортировки таблицы объявлений в веб-админке —
# whitelist, чтобы sort/order из query-параметров никогда не попадали
# в SQL напрямую (защита от SQL-инъекции через ORDER BY).
LISTINGS_SORT_COLUMNS = {
    "created_at": "l.created_at",
    "title": "l.title",
    "price": "l.price",
    "city": "city_name",
    "category": "category_name",
    "type": "l.type",
    "status": "l.status",
    "opens": "opens",
}
LISTINGS_FILTER_FIELDS = ["title", "city", "category"]


@app.get("/api/listings")
def listings_catalog(offset: int = 0, limit: int = 24,
                     section: str = "", category_id: int = 0,
                     only_photo: bool = False,
                     only_active: bool = False, q: str = "",
                     activity: str = "", sort: str = "created_at", order: str = "desc",
                     filters: str = ""):
    sort_col = LISTINGS_SORT_COLUMNS.get(sort, "l.created_at")
    sort_dir = "ASC" if order == "asc" else "DESC"
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
        if activity == "active":
            wheres.append("(l.status='active' OR l.status IS NULL)")
        elif activity == "inactive":
            wheres.append("l.status IS NOT NULL AND l.status!='active'")
        try:
            f_dict = json.loads(filters) if filters else {}
        except Exception:
            f_dict = {}
        where_sql = ("WHERE " + " AND ".join(wheres)) if wheres else ""
        try:
            rows = conn.execute(f"""
                SELECT l.id, l.title, l.price, l.contact, l.photo_file_id, l.flex,
                       l.is_sold, l.created_at, l.type, l.status,
                       ci.name AS city_name,
                       cat.name AS category_name,
                       COUNT(lv.id) AS opens,
                       l.descr
                FROM listing l
                LEFT JOIN city ci ON ci.id=l.city_id
                LEFT JOIN category cat ON cat.id=l.category_id
                LEFT JOIN listing_views lv ON lv.listing_id=l.id AND lv.action='open'
                {where_sql}
                GROUP BY l.id
                ORDER BY {sort_col} {sort_dir}
            """, params).fetchall()
        except Exception as e:
            return {"total": 0, "rows": [], "error": str(e)}
        result = []
        for r in rows:
            photo_ids = [p.strip() for p in (r[4] or "").split(",") if p.strip()]
            video = _parse_video(r[5] or "")
            if not video.get("video_type") and (r[8] or "") == "release":
                video = {**video, **_video_from_release(conn, r[0])}
            result.append({
                "id": r[0], "title": r[1] or "", "price": r[2] or "",
                "contact": r[3] or "", "photo_ids": photo_ids, **video,
                # Полная дата-время (не срез [:10]) — фронтенд сам форматирует
                # в ДД.ММ.ГГГГ ЧЧ:ММ через общую fmtDateTime().
                "is_sold": bool(r[6]), "created_at": r[7] or "",
                "type": r[8] or "", "status": r[9] or "",
                "city": r[10] or "", "category": r[11] or "", "opens": r[12] or 0,
                "descr": r[13] or "",
            })
    total, page = _casefold_filter_paginate(
        result, q, ["title", "descr"], f_dict, LISTINGS_FILTER_FIELDS, offset, limit)
    for row in page:
        row.pop("descr", None)
    return {"total": total, "offset": offset, "limit": limit, "rows": page}


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
.col-resize{position:absolute;right:-3px;top:0;bottom:0;width:6px;cursor:col-resize;z-index:5}
.col-resize:hover{background:#3a5a9c}
th.txt-th{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.txt-td{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rte-toolbar{display:flex;gap:4px;margin-bottom:4px}
.rte-toolbar button{width:28px;height:24px;padding:0;border-radius:5px;border:none;
  background:#252540;color:#99a;cursor:pointer;font-size:12px}
.rte-toolbar button:hover{background:#2f2f55;color:#dde}
.rte-edit{width:100%;min-height:100px;background:#0d1424;color:#dde;border:1px solid #223;
  border-radius:6px;padding:8px;font:13px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;
  box-sizing:border-box;white-space:pre-wrap;outline:none}
.rte-edit:focus{border-color:#5a9cf5}
.rte-edit code{background:#1a2340;padding:1px 4px;border-radius:3px;font-family:monospace}
.rte-edit a{color:#7eb8f7}
.ff-switch{position:relative;display:inline-block;width:38px;height:20px;cursor:pointer}
.ff-switch input{opacity:0;width:0;height:0}
.ff-switch-track{position:absolute;inset:0;background:#2a2a45;border-radius:20px;transition:.15s}
.ff-switch-track::before{content:'';position:absolute;left:2px;top:2px;width:16px;height:16px;
  background:#889;border-radius:50%;transition:.15s}
.ff-switch input:checked + .ff-switch-track{background:#3a6a3a}
.ff-switch input:checked + .ff-switch-track::before{transform:translateX(18px);background:#8ef58e}
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

/* fb-tooltip: своя подсказка вместо нативной title — управляемый шрифт */
.fb-tooltip{position:fixed;z-index:10001;background:#161630;color:#e2e6f5;
  border:1px solid #2a3560;border-radius:8px;padding:10px 14px;font-size:15px;
  line-height:1.45;max-width:420px;white-space:pre-wrap;word-break:break-word;
  box-shadow:0 10px 30px rgba(0,0,0,.55);pointer-events:none;display:none}
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
.an-pagination button.active{background:#3a5a9c;color:#fff;cursor:default}
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
.cat-card-status{position:absolute;bottom:6px;left:8px;padding:2px 8px;border-radius:5px;
  font-size:10px;font-weight:700;letter-spacing:.3px}
/* Тот же вид, что у .cat-card-status, но без абсолютного позиционирования —
   для ячейки таблицы (там нет позиционированного родителя .cat-card-media). */
.cat-table-status{display:inline-block;padding:2px 8px;border-radius:5px;
  font-size:10px;font-weight:700;letter-spacing:.3px}
.st-active{background:rgba(16,90,50,.9);color:#a7f3c8}
.st-inactive{background:rgba(70,75,88,.9);color:#d7dbe2}
.cat-card-off .cat-card-media img{filter:grayscale(.75);opacity:.55}
.cat-card-off .cat-card-title{color:#889}
.st-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;flex-shrink:0;vertical-align:middle}
.st-dot-on{background:#34d399}
.st-dot-off{background:#5b6270;outline:1px solid #808898}
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
  <button class="tab active" onclick="switchTab('categories')">🗂 Категории</button>
  <button class="tab" onclick="switchTab('analytics')" data-i18n="tab_analytics">📊 Аналитика</button>
  <button class="tab" onclick="switchTab('catalog')" data-i18n="tab_catalog">📦 Объявления</button>
  <button class="tab" onclick="switchTab('releases')">🎵 Релизы</button>
  <button class="tab" onclick="switchTab('artists')">🎤 Исполнители</button>
  <button class="tab" onclick="switchTab('feedback')">✉️ Обратная связь</button>
  <button class="tab" onclick="switchTab('texts')">📝 Тексты</button>
  <button class="tab" onclick="switchTab('settings')">⚡ Настройки</button>
  <button class="tab" onclick="switchTab('users')">👤 Пользователи</button>
</div>

<div id="panel-categories" class="panel active">
  <div class="toolbar">
    <button class="btn btn-ghost btn-sm" id="cat-subtab-market" onclick="catSetSubtab('market')" data-i18n="tab_market">Барахолка</button>
    <button class="btn btn-ghost btn-sm" id="cat-subtab-services" onclick="catSetSubtab('services')" data-i18n="tab_services">Услуги</button>
    <button class="btn btn-ghost btn-sm" id="cat-subtab-vacancy" onclick="catSetSubtab('vacancy')" data-i18n="tab_vacancy">Вакансии</button>
  </div>

  <div id="catview-market"><div class="toolbar">
    <button class="btn btn-primary" onclick="openAdd(null,'market')" data-i18n="add_top">+ Добавить корневую</button>
    <button class="btn btn-ghost"   onclick="clearSelection()" data-i18n="clear_sel">Снять выделение</button>
  </div><div class="batch-bar" id="batch-market">
    <span class="batch-count" id="batch-count-market">0 выбрано</span>
    <button class="btn btn-fields" onclick="openBatchField('market')" data-i18n="add_field_sel">Добавить поле к выбранным</button>
    <button class="btn btn-ghost"  onclick="clearSelection()">✕</button>
  </div><div class="tree" id="tree-market"></div></div>

  <div id="catview-services" style="display:none"><div class="toolbar">
    <button class="btn btn-primary" onclick="openAdd(null,'services')" data-i18n="add_top">+ Добавить корневую</button>
    <button class="btn btn-ghost"   onclick="clearSelection()" data-i18n="clear_sel">Снять выделение</button>
  </div><div class="batch-bar" id="batch-services">
    <span class="batch-count" id="batch-count-services">0 выбрано</span>
    <button class="btn btn-fields" onclick="openBatchField('services')" data-i18n="add_field_sel">Добавить поле к выбранным</button>
    <button class="btn btn-ghost"  onclick="clearSelection()">✕</button>
  </div><div class="tree" id="tree-services"></div></div>

  <div id="catview-vacancy" style="display:none"><div class="toolbar">
    <button class="btn btn-primary" onclick="openAdd(null,'vacancy')" data-i18n="add_top">+ Добавить корневую</button>
    <button class="btn btn-ghost"   onclick="clearSelection()" data-i18n="clear_sel">Снять выделение</button>
  </div><div class="batch-bar" id="batch-vacancy">
    <span class="batch-count" id="batch-count-vacancy">0 выбрано</span>
    <button class="btn btn-fields" onclick="openBatchField('vacancy')" data-i18n="add_field_sel">Добавить поле к выбранным</button>
    <button class="btn btn-ghost"  onclick="clearSelection()">✕</button>
  </div><div class="tree" id="tree-vacancy"></div></div>
</div>

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
  <div class="toolbar" id="an-topcards-toolbar" style="display:none">
    <input type="text" id="topcards-search" placeholder="🔎 Поиск по названию, контакту…"
      style="flex:1;min-width:220px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:7px 10px;font:inherit"
      oninput="tcSearchDebounced()">
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
  <div class="toolbar">
    <input type="text" id="catalog-search" placeholder="🔎 Поиск по названию и описанию…"
      style="flex:1;min-width:220px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:7px 10px;font:inherit"
      oninput="catSearchDebounced()">
  </div>
  <div id="catalog-content"></div>
</div>

<div id="panel-releases" class="panel">
  <div class="toolbar">
    <input type="text" id="releases-search" placeholder="🔎 Поиск по названию, исполнителю, автору…"
      style="flex:1;min-width:220px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:7px 10px;font:inherit"
      oninput="relSearchDebounced()">
  </div>
  <div id="releases-content"></div>
</div>

<div id="panel-artists" class="panel">
  <div class="toolbar">
    <input type="text" id="artists-search" placeholder="🔎 Поиск по имени, жанру, городу, автору…"
      style="flex:1;min-width:220px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:7px 10px;font:inherit"
      oninput="artSearchDebounced()">
  </div>
  <div id="artists-content"></div>
</div>

<div id="panel-feedback" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost btn-sm" id="fb-tab-all" onclick="fbSetFilter(false)">Все</button>
    <button class="btn btn-ghost btn-sm" id="fb-tab-unanswered" onclick="fbSetFilter(true)">🔔 Неотвеченные</button>
    <button class="btn btn-ghost btn-sm" onclick="loadFeedback()">↻ Обновить</button>
  </div>
  <div id="feedback-content"></div>
</div>

<div id="panel-texts" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost btn-sm" id="txt-subtab-bottext" onclick="txtSetSubtab('bottext')">Тексты</button>
    <button class="btn btn-ghost btn-sm" id="txt-subtab-menu" onclick="txtSetSubtab('menu')">Кнопки меню</button>
  </div>
  <div id="texts-bottext-view">
    <div class="toolbar">
      <input type="text" id="txt-search" placeholder="Поиск по коду, названию или тексту…"
        style="flex:1;min-width:220px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:7px 10px;font:inherit"
        oninput="txtSearchDebounced()">
      <button class="btn btn-ghost btn-sm" onclick="loadTexts()">↻ Обновить</button>
    </div>
    <div id="texts-content"></div>
  </div>
  <div id="texts-menu-view" style="display:none">
    <div class="toolbar">
      <button class="btn btn-ghost btn-sm" onclick="loadMenuItems()">↻ Обновить</button>
    </div>
    <div id="menu-items-content"></div>
  </div>
</div>

<div id="panel-settings" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost btn-sm" onclick="loadFeatureFlags()">↻ Обновить</button>
  </div>
  <div style="font-size:11px;color:#556;margin-bottom:12px">
    Изменение применяется в боте не мгновенно — до 30 секунд (кэш выключателей в отдельном процессе бота).
  </div>
  <div id="feature-flags-content"></div>
</div>

<div id="panel-users" class="panel">
  <div class="toolbar">
    <button class="btn btn-ghost btn-sm" id="user-subtab-list" onclick="userSetSubtab('list')">Список</button>
    <button class="btn btn-ghost btn-sm" id="user-subtab-broadcast" onclick="userSetSubtab('broadcast')">📣 Рассылка</button>
  </div>

  <div id="users-list-view">
    <div class="toolbar">
      <input type="text" id="user-search" placeholder="Поиск по ID, username или имени…"
        style="flex:1;min-width:220px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:7px 10px;font:inherit"
        oninput="userSearchDebounced()">
      <button class="btn btn-ghost btn-sm" onclick="loadUsers()">↻ Обновить</button>
    </div>
    <div style="font-size:11px;color:#556;margin-bottom:12px">
      «Ограничить» запрещает публикацию нового контента (Барахолка/Услуги/Вакансии/Афиша/Исполнители/Релизы).
      Просмотр разделов и обратная связь остаются доступны — это не полная блокировка.
    </div>
    <div id="users-content"></div>
  </div>

  <div id="users-broadcast-view" style="display:none">
    <div style="margin-bottom:10px">
      <label style="display:block;font-size:12px;color:#889;margin-bottom:4px">Текст рассылки (RU)</label>
      <div class="rte-toolbar">
        <button type="button" title="Жирный" onmousedown="event.preventDefault()" onclick="txtExecCmd('bc-text-edit','bold')"><b>B</b></button>
        <button type="button" title="Курсив" onmousedown="event.preventDefault()" onclick="txtExecCmd('bc-text-edit','italic')"><i>I</i></button>
        <button type="button" title="Подчёркнутый" onmousedown="event.preventDefault()" onclick="txtExecCmd('bc-text-edit','underline')"><u>U</u></button>
      </div>
      <div id="bc-text-edit" class="rte-edit" contenteditable="true" onkeydown="txtEditorKeydown(event)" style="min-height:120px"></div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px">
      <label style="font-size:12px;color:#889">Тестовый ID:
        <input type="text" id="bc-test-id" style="width:130px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:6px 8px;font:inherit;margin-left:6px">
      </label>
      <button class="btn btn-ghost btn-sm" onclick="broadcastTest()">📨 Отправить тест себе</button>
      <span id="bc-test-status" style="font-size:12px"></span>
    </div>

    <div style="margin-bottom:10px">
      <label style="display:block;font-size:12px;color:#889;margin-bottom:6px">Получатели</label>
      <div style="display:flex;gap:16px;margin-bottom:8px;font-size:13px">
        <label><input type="radio" name="bc-mode" value="all" checked onchange="broadcastModeChange()"> Всем</label>
        <label><input type="radio" name="bc-mode" value="only" onchange="broadcastModeChange()"> Только отмеченным</label>
        <label><input type="radio" name="bc-mode" value="except" onchange="broadcastModeChange()"> Всем, кроме отмеченных</label>
      </div>
      <div id="bc-user-checklist" style="display:none;max-height:200px;overflow-y:auto;background:#0d1424;border:1px solid #223;border-radius:6px;padding:8px 10px;margin-bottom:8px"></div>
    </div>
    <div style="margin-bottom:14px">
      <button class="btn btn-primary" onclick="broadcastSend()">🚀 Отправить (<span id="bc-audience-count">…</span>)</button>
    </div>
    <div id="bc-progress" style="display:none;margin-bottom:16px">
      <div style="font-size:12px;color:#889;margin-bottom:6px" id="bc-progress-text"></div>
      <div style="background:#1a2050;border-radius:6px;height:8px;overflow:hidden">
        <div id="bc-progress-bar" style="background:#5a9cf5;height:100%;width:0%;transition:.2s"></div>
      </div>
    </div>
    <div style="font-size:12px;color:#556;margin-bottom:8px">История рассылок</div>
    <div id="bc-history-content"></div>
  </div>
</div>

<!-- Text edit modal -->
<div class="overlay" id="modal-text" onclick="if(event.target===this)closeTextModal()">
  <div class="modal" style="width:560px;max-width:94vw;max-height:90vh;overflow-y:auto;padding:24px">
    <div style="display:flex;justify-content:flex-end;margin-bottom:12px">
      <button class="btn btn-ghost btn-sm" onclick="closeTextModal()">✕ Закрыть</button>
    </div>
    <div id="text-modal-content"></div>
  </div>
</div>

<!-- Feedback detail/reply modal -->
<div class="overlay" id="modal-feedback" onclick="if(event.target===this)closeFeedbackModal()">
  <div class="modal" style="width:520px;max-width:94vw;max-height:90vh;overflow-y:auto;padding:24px">
    <div style="display:flex;justify-content:flex-end;margin-bottom:12px">
      <button class="btn btn-ghost btn-sm" onclick="closeFeedbackModal()">✕ Закрыть</button>
    </div>
    <div id="feedback-modal-content"><div class="empty" style="padding:40px;text-align:center">…</div></div>
  </div>
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

<!-- Openers (кто открывал) modal -->
<div class="overlay" id="modal-openers" onclick="if(event.target===this)closeOpenersModal()">
  <div class="modal" style="width:480px;max-width:94vw;max-height:80vh;overflow-y:auto;padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <h2 id="openers-title" style="font-size:14px;margin:0;color:#eef">Кто открывал</h2>
      <button class="btn btn-ghost btn-sm" onclick="closeOpenersModal()">✕ Закрыть</button>
    </div>
    <div id="openers-content"><div class="empty" style="padding:30px;text-align:center">…</div></div>
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
        <button class="btn btn-sm btn-del"    title="${T('tt_delete')}" onclick="deleteCat(${n.id},${jsArg(section)},${jsArg(n.name)})">✕</button>
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
// Единый формат даты/времени по всей веб-админке: ДД.ММ.ГГГГ ЧЧ:ММ
// (обычный для RU порядок день.месяц, в отличие от голого среза ISO-строки).
function fmtDateTime(s) {
  if (!s) return '—';
  const clean = s.replace('T',' ').split('+')[0].split('.')[0];
  const [date, time=''] = clean.split(' ');
  const [y,m,day] = (date||'').split('-');
  return y && m && day ? `${day}.${m}.${y}${time ? ' '+time.slice(0,5) : ''}` : s;
}
function jsArg(value){ return esc(JSON.stringify(String(value ?? ''))) }
function safeHttpUrl(raw) {
  try {
    const u = new URL(String(raw || ''));
    return (u.protocol === 'http:' || u.protocol === 'https:') ? u.href : '';
  } catch (_) { return ''; }
}

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

// Перерисовка таблицы теряет фокус/курсор в постолбцовых полях поиска —
// сохраняем id и позицию курсора активного инпута и восстанавливаем после рендера.
async function withFocusPreserved(reloadFn) {
  const active = document.activeElement;
  const id = active && active.id;
  const selStart = active && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  const selEnd = active && typeof active.selectionEnd === 'number' ? active.selectionEnd : null;
  await reloadFn();
  if (!id) return;
  const el = document.getElementById(id);
  if (el && typeof el.focus === 'function') {
    el.focus();
    if (selStart !== null && el.setSelectionRange) {
      try { el.setSelectionRange(selStart, selEnd); } catch(e) {}
    }
  }
}

// Пагинация с номерами страниц + переход на произвольную страницу — общая
// для Релизов/Исполнителей/Объявлений/Топ карточек (иначе с сотнями строк
// дойти до дальней страницы можно только кликая «▶» много раз подряд).
function paginationHtml(total, limit, offset, gotoFnName) {
  const totalPages = Math.max(1, Math.ceil(total / limit));
  if (totalPages <= 1) return '';
  const curPage = Math.floor(offset / limit) + 1;
  const pageBtn = (p) => `<button class="${p===curPage?'active':''}" onclick="${gotoFnName}(${(p-1)*limit})" ${p===curPage?'disabled':''}>${p}</button>`;
  let nums = [];
  if (totalPages <= 9) {
    for (let i=1;i<=totalPages;i++) nums.push(i);
  } else {
    nums.push(1,2);
    if (curPage > 4) nums.push('…');
    for (let i=Math.max(3,curPage-2); i<=Math.min(totalPages-2,curPage+2); i++) nums.push(i);
    if (curPage < totalPages-3) nums.push('…');
    nums.push(totalPages-1,totalPages);
  }
  const seen = new Set();
  nums = nums.filter(p => { const k=String(p); if (seen.has(k)) return false; seen.add(k); return true; });
  const numsHtml = nums.map(p => p==='…' ? `<span style="padding:0 4px;color:#556">…</span>` : pageBtn(p)).join('');
  return `<div class="an-pagination" style="margin-top:16px;flex-wrap:wrap">
    <button onclick="${gotoFnName}(0)" ${curPage===1?'disabled':''}>« Первая</button>
    <button onclick="${gotoFnName}(${Math.max(0,offset-limit)})" ${offset===0?'disabled':''}>◀</button>
    ${numsHtml}
    <button onclick="${gotoFnName}(${offset+limit})" ${offset+limit>=total?'disabled':''}>▶</button>
    <button onclick="${gotoFnName}(${(totalPages-1)*limit})" ${curPage===totalPages?'disabled':''}>Последняя »</button>
    <span style="margin-left:6px">Стр. ${curPage} / ${totalPages}</span>
    <input type="number" min="1" max="${totalPages}" placeholder="№ стр."
      style="width:64px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:4px;padding:3px 6px;font-size:12px"
      onkeydown="if(event.key==='Enter'){const p=Math.min(${totalPages},Math.max(1,parseInt(this.value)||1));${gotoFnName}((p-1)*${limit});this.value='';}">
  </div>`;
}

// ── «Кто открывал» — разбивка счётчика открытий по пользователям ──
function closeOpenersModal() {
  document.getElementById('modal-openers').classList.remove('open');
}
async function openOpenersModal(kind, id, title) {
  document.getElementById('openers-title').textContent = title || 'Кто открывал';
  document.getElementById('openers-content').innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  openOverlay('modal-openers');
  const url = kind === 'artist' ? `/api/artist/${id}/openers` : `/api/listing/${id}/openers`;
  try {
    const data = await fetch(url).then(r=>r.json());
    renderOpenersModal(data.rows || []);
  } catch(e) {
    document.getElementById('openers-content').innerHTML = `<div class="empty" style="color:#f66">Ошибка: ${esc(e.message)}</div>`;
  }
}
function renderOpenersModal(rows) {
  const el = document.getElementById('openers-content');
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Пока никто не открывал</div>';
    return;
  }
  const total = rows.reduce((s,r)=>s+(r.count||0), 0);
  el.innerHTML = `<div style="font-size:12px;color:#556;margin-bottom:10px">${rows.length} пользователей · ${total} открытий</div>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:6px 8px">Пользователь</th>
      <th style="padding:6px 8px;text-align:center">Открытий</th>
      <th style="padding:6px 8px">Последний раз</th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr style="border-top:1px solid #0d1628">
        <td style="padding:7px 8px">
          <div style="color:#ccd">${r.username ? '@'+esc(r.username) : esc(r.full_name || String(r.user_id))}</div>
          <div style="color:#556;font-size:11px;font-family:monospace">${r.user_id}</div>
        </td>
        <td style="padding:7px 8px;text-align:center;color:#9bc;font-weight:700">${r.count}</td>
        <td style="padding:7px 8px;color:#778;font-size:12px;white-space:nowrap">${fmtDateTime(r.last_open)}</td>
      </tr>`).join('')}</tbody>
  </table>`;
}

// ── tabs ──
const ALL_TABS = ['categories','analytics','catalog','releases','artists','feedback','texts','settings','users'];
function switchTab(section) {
  currentTab=section;
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active', ALL_TABS[i]===section));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById(`panel-${section}`).classList.add('active');
  if (section==='analytics') loadAnalytics();
  else if (section==='catalog') catalogLoad(0);
  else if (section==='artists') loadArtists();
  else if (section==='releases') loadReleases();
  else if (section==='feedback') loadFeedback();
  else if (section==='texts') txtSetSubtab(txtSubtab);
  else if (section==='categories') catSetSubtab(catSubtab);
  else if (section==='settings') loadFeatureFlags();
  else if (section==='users') userSetSubtab(userSubtab);
}

// ── Категории: под-вкладки Барахолка/Услуги/Вакансии внутри одной вкладки ──
let catSubtab = 'market';
function catSetSubtab(name) {
  catSubtab = name;
  ['market','services','vacancy'].forEach(s => {
    document.getElementById(`cat-subtab-${s}`).classList.toggle('btn-primary', s===name);
    document.getElementById(`catview-${s}`).style.display = s===name ? '' : 'none';
  });
  loadTree(name);
}

// ── Релизы (отдельная вкладка: это не объявления) ──
const RTYPE_LABELS = {single:'Сингл', ep:'EP', album:'Альбом', clip:'Клип', live:'Live'};

// ── Релизы: превью/таблица, настраиваемые столбцы, сортировка, поиск (тот же
//    паттерн, что и у таблицы объявлений «📦 Объявления» — см. cat*) ──
const REL_LIMIT = 24;
let _relOffset = 0;
let _relViewMode = localStorage.getItem('admin_rel_view_mode') || 'table';
let _relSortKey = 'created_at';
let _relSortDir = 'desc';
let _relQuery = '';
let _relSearchTimer = null;
let _relColFilters = {};

function relSearchDebounced() {
  clearTimeout(_relSearchTimer);
  _relSearchTimer = setTimeout(() => {
    _relQuery = document.getElementById('releases-search').value;
    _relOffset = 0;
    loadReleases();
  }, 300);
}
function relSetViewMode(mode) {
  _relViewMode = mode;
  localStorage.setItem('admin_rel_view_mode', mode);
  loadReleases();
}
function relGoPage(offset) { _relOffset = offset; loadReleases(); }

const REL_COLUMNS_DEF = [
  {key:'photo',      label:'',            minWidth:50,  defaultWidth:50,  locked:true, sortable:false},
  {key:'title',      label:'Релиз',       minWidth:120, defaultWidth:200, sortable:true,  filterable:true},
  {key:'artist',     label:'Исполнитель', minWidth:100, defaultWidth:150, sortable:true,  filterable:true},
  {key:'rtype',      label:'Тип',         minWidth:70,  defaultWidth:90,  sortable:true,  filterable:true},
  {key:'tracks',     label:'Треков',      minWidth:60,  defaultWidth:80,  sortable:true},
  {key:'opens',      label:'Открытий',    minWidth:70,  defaultWidth:100, sortable:true},
  {key:'username',   label:'Автор',       minWidth:90,  defaultWidth:130, sortable:true,  filterable:true},
  {key:'created_at', label:'Создан',      minWidth:90,  defaultWidth:135, sortable:true},
  {key:'status',     label:'Статус',      minWidth:70,  defaultWidth:100, sortable:true,  filterable:true},
];
const REL_COLS_STORAGE_KEY = 'admin_rel_columns_v1';

function relLoadColumnPrefs() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(REL_COLS_STORAGE_KEY) || '{}'); } catch(e) {}
  const allKeys = REL_COLUMNS_DEF.map(c => c.key);
  let order = Array.isArray(saved.order) ? saved.order.filter(k => allKeys.includes(k)) : [];
  allKeys.forEach(k => { if (!order.includes(k)) order.push(k); });
  const widths = saved.widths || {};
  const visible = saved.visible || {};
  REL_COLUMNS_DEF.forEach(c => {
    if (typeof widths[c.key] !== 'number') widths[c.key] = c.defaultWidth;
    if (typeof visible[c.key] !== 'boolean') visible[c.key] = true;
    if (c.locked) visible[c.key] = true;
  });
  return {order, widths, visible};
}
let relCols = relLoadColumnPrefs();
function relSaveColumnPrefs() { localStorage.setItem(REL_COLS_STORAGE_KEY, JSON.stringify(relCols)); }
function relVisibleColumns() {
  return relCols.order.filter(k => relCols.visible[k] !== false).map(k => REL_COLUMNS_DEF.find(c => c.key===k));
}
function relToggleColumn(key, checked) {
  relCols.visible[key] = checked;
  relSaveColumnPrefs();
  loadReleases();
}
function relToggleColsPanel() {
  const p = document.getElementById('rel-cols-panel');
  if (p) p.style.display = p.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', (e) => {
  const panel = document.getElementById('rel-cols-panel');
  const btn = document.getElementById('rel-cols-btn');
  if (panel && panel.style.display !== 'none' && !panel.contains(e.target) && e.target !== btn) {
    panel.style.display = 'none';
  }
});

let _relDragKey = null;
function relColDragStart(e) { _relDragKey = e.currentTarget.dataset.col; e.dataTransfer.effectAllowed = 'move'; }
function relColDragOver(e) { e.preventDefault(); }
function relColDrop(e) {
  e.preventDefault();
  const targetKey = e.currentTarget.dataset.col;
  if (!_relDragKey || _relDragKey === targetKey) return;
  const order = relCols.order;
  const from = order.indexOf(_relDragKey);
  const to = order.indexOf(targetKey);
  if (from === -1 || to === -1) return;
  order.splice(from, 1);
  order.splice(to, 0, _relDragKey);
  relSaveColumnPrefs();
  loadReleases();
}

let _relResizeState = null;
function relColResizeStart(e, key) {
  e.preventDefault(); e.stopPropagation();
  _relResizeState = { key, startX: e.clientX, startWidth: relCols.widths[key] };
  document.addEventListener('mousemove', relColResizeMove);
  document.addEventListener('mouseup', relColResizeEnd);
}
function relColResizeMove(e) {
  if (!_relResizeState) return;
  const def = REL_COLUMNS_DEF.find(c => c.key === _relResizeState.key);
  const delta = e.clientX - _relResizeState.startX;
  const newWidth = Math.max(def.minWidth, _relResizeState.startWidth + delta);
  relCols.widths[_relResizeState.key] = newWidth;
  const idx = relVisibleColumns().findIndex(c => c.key === _relResizeState.key);
  const table = document.querySelector('#releases-content table.rel-table');
  if (table && idx !== -1) {
    const colEl = table.querySelectorAll('colgroup col')[idx];
    if (colEl) colEl.style.width = newWidth + 'px';
  }
}
function relColResizeEnd() {
  document.removeEventListener('mousemove', relColResizeMove);
  document.removeEventListener('mouseup', relColResizeEnd);
  _relResizeState = null;
  relSaveColumnPrefs();
}

function relSortBy(key) {
  const def = REL_COLUMNS_DEF.find(c => c.key === key);
  if (!def || def.sortable === false) return;
  if (_relSortKey === key) { _relSortDir = _relSortDir === 'desc' ? 'asc' : 'desc'; }
  else { _relSortKey = key; _relSortDir = 'desc'; }
  _relOffset = 0;
  loadReleases();
}
function relFilterInput(key, value) {
  _relColFilters[key] = value;
  clearTimeout(_relSearchTimer);
  _relSearchTimer = setTimeout(() => {
    _relOffset = 0;
    withFocusPreserved(loadReleases);
  }, 300);
}

function relCellHtml(r, key) {
  if (key === 'photo') return r.photo
    ? `<img src="/api/tg_photo/${encodeURIComponent(r.photo)}" style="width:36px;height:36px;border-radius:5px;object-fit:cover" onerror="this.remove()">`
    : '🎵';
  if (key === 'title') return `<span class="st-dot ${r.status==='published'?'st-dot-on':'st-dot-off'}"></span> <a style="color:#7eb8f7;cursor:pointer" onclick="openListing(${r.id})">${esc(r.title)}</a>`;
  if (key === 'artist') return r.artist_id
    ? `<a style="color:#9bc;cursor:pointer" onclick="openArtistCard(${r.artist_id})">🎤 ${esc(r.artist)}</a>`
    : esc(r.artist);
  if (key === 'rtype') return esc(RTYPE_LABELS[r.rtype]||r.rtype);
  if (key === 'tracks') return String(r.tracks || '—');
  if (key === 'opens') return `<a style="color:#9bc;cursor:pointer;font-weight:700" onclick="openOpenersModal('listing',${r.id},${jsArg('Кто открывал: '+r.title)})">${r.opens}</a>`;
  if (key === 'username') return r.username ? '@'+esc(r.username) : String(r.owner_id);
  if (key === 'created_at') return fmtDateTime(r.created_at);
  if (key === 'status') return r.status==='published' ? '<span style="color:#6ef5aa">опубликован</span>' : '<span style="color:#888">скрыт</span>';
  return '';
}

function relTableHtml(rows) {
  const cols = relVisibleColumns();
  const colgroup = cols.map(c => `<col style="width:${relCols.widths[c.key]}px">`).join('');
  const thead = cols.map(c => {
    const sortable = c.sortable !== false;
    const arrow = _relSortKey === c.key ? (_relSortDir === 'desc' ? ' ▼' : ' ▲') : '';
    return `<th class="txt-th" data-col="${c.key}" draggable="${!c.locked}"
      ondragstart="relColDragStart(event)" ondragover="relColDragOver(event)" ondrop="relColDrop(event)"
      style="padding:6px 8px;position:relative;${c.locked?'':'cursor:grab'}${sortable?';cursor:pointer':''}"
      ${sortable ? `onclick="relSortBy('${c.key}')"` : ''}>${esc(c.label)}${arrow}<span class="col-resize" onmousedown="relColResizeStart(event,'${c.key}')"></span></th>`;
  }).join('');
  const filterRow = cols.map(c => `<th style="padding:2px 6px">${c.filterable
    ? `<input type="text" id="rel-filter-${c.key}" value="${esc(_relColFilters[c.key]||'')}" placeholder="…"
        oninput="relFilterInput('${c.key}', this.value)"
        style="width:100%;background:#0d1424;color:#dde;border:1px solid #223;border-radius:4px;padding:3px 6px;font-size:11px">`
    : ''}</th>`).join('');
  const tbody = rows.map(r => `
    <tr style="border-top:1px solid #0d1628">
      ${cols.map(c => `<td class="txt-td" style="padding:6px 8px">${relCellHtml(r, c.key)}</td>`).join('')}
    </tr>`).join('');
  const colsPanel = REL_COLUMNS_DEF.filter(c => !c.locked).map(c => `
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;padding:4px 0">
        <input type="checkbox" ${relCols.visible[c.key]!==false?'checked':''}
          onchange="relToggleColumn('${c.key}', this.checked)"> ${esc(c.label)}
      </label>`).join('');
  return `
  <div style="display:flex;justify-content:flex-end;margin-bottom:8px;position:relative">
    <button class="btn btn-ghost btn-sm" id="rel-cols-btn" onclick="relToggleColsPanel()">⚙ Столбцы</button>
    <div id="rel-cols-panel" style="display:none;position:absolute;right:0;top:28px;background:#1e1e35;
      border:1px solid #333;border-radius:8px;padding:10px 14px;z-index:20;min-width:150px">${colsPanel}</div>
  </div>
  <table class="rel-table" style="width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed">
    <colgroup>${colgroup}</colgroup>
    <thead>
      <tr style="color:#556;font-size:11px;text-align:left">${thead}</tr>
      <tr>${filterRow}</tr>
    </thead>
    <tbody>${tbody}</tbody>
  </table>`;
}

function relCardHtml(r) {
  const media = r.photo
    ? `<img src="/api/tg_photo/${encodeURIComponent(r.photo)}" onerror="this.parentElement.innerHTML='<div class=\\'cat-card-media-empty\\'>🎵</div>'" alt="">`
    : `<div class="cat-card-media-empty">🎵</div>`;
  const statusBadge = r.status === 'published'
    ? '<span class="cat-card-status st-active">опубликован</span>'
    : '<span class="cat-card-status st-inactive">скрыт</span>';
  return `<div class="cat-card${r.status==='published'?'':' cat-card-off'}" onclick="openListing(${r.id})">
    <div class="cat-card-media">
      ${media}
      ${statusBadge}
    </div>
    <div class="cat-card-body">
      <div class="cat-card-title">${esc(r.title||'Без названия')}</div>
      <div class="cat-card-meta">${esc(RTYPE_LABELS[r.rtype]||r.rtype)}${r.artist && r.artist !== '—' ? ' · '+esc(r.artist) : ''}</div>
      <div class="cat-card-stats">👁 ${r.opens} · ${fmtDateTime(r.created_at)}</div>
    </div>
  </div>`;
}

async function loadReleases() {
  const el = document.getElementById('releases-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const params = new URLSearchParams({offset: _relOffset, limit: REL_LIMIT, sort: _relSortKey, order: _relSortDir});
  if (_relQuery) params.set('q', _relQuery);
  const activeFilters = Object.fromEntries(Object.entries(_relColFilters).filter(([,v]) => v));
  if (Object.keys(activeFilters).length) params.set('filters', JSON.stringify(activeFilters));
  const data = await fetch(`/api/releases?${params}`).then(r=>r.json());
  const rows = data.rows || [];
  const viewToggle = `<div style="display:flex;gap:4px">
    <button class="btn btn-sm" style="${_relViewMode==='grid'?'background:#2a3a70':''}" onclick="relSetViewMode('grid')">🖼 Превью</button>
    <button class="btn btn-sm" style="${_relViewMode==='table'?'background:#2a3a70':''}" onclick="relSetViewMode('table')">📋 Таблица</button>
  </div>`;
  const topBar = `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap">
    <span style="font-size:12px;color:#556">${data.total||0} релизов</span>
    ${viewToggle}
  </div>`;
  if (!rows.length) {
    // В табличном режиме таблицу (шапку + строку фильтров) не убираем — иначе
    // вместе с ней пропадает и поле, где пользователь набирал текст фильтра,
    // и поправить запрос становится нечем, кроме перезагрузки страницы.
    const emptyMsg = '<div class="empty" style="padding:20px;text-align:center">Релизов не найдено</div>';
    el.innerHTML = topBar + (_relViewMode === 'table' ? relTableHtml(rows) + emptyMsg : emptyMsg);
    return;
  }
  const pag = paginationHtml(data.total||0, REL_LIMIT, _relOffset, 'relGoPage');
  const body = _relViewMode === 'table' ? relTableHtml(rows) : `<div class="catalog-listings-grid">${rows.map(r=>relCardHtml(r)).join('')}</div>`;
  el.innerHTML = topBar + body + pag;
}

async function releaseToggle(id) {
  try {
    const r = await fetch(`/api/release/${id}/toggle_status`, {method:'POST'}).then(x=>x.json());
    if (r && r.ok) loadReleases();
    else alert('Не удалось: ' + ((r&&r.detail)||'?'));
  } catch(e) { alert('Ошибка: ' + e.message); }
}

// ── Исполнители: тот же паттерн, что и у Релизов ──
const ART_LIMIT = 24;
let _artOffset = 0;
let _artViewMode = localStorage.getItem('admin_art_view_mode') || 'table';
let _artSortKey = 'created_at';
let _artSortDir = 'desc';
let _artQuery = '';
let _artSearchTimer = null;
let _artColFilters = {};

function artSearchDebounced() {
  clearTimeout(_artSearchTimer);
  _artSearchTimer = setTimeout(() => {
    _artQuery = document.getElementById('artists-search').value;
    _artOffset = 0;
    loadArtists();
  }, 300);
}
function artSetViewMode(mode) {
  _artViewMode = mode;
  localStorage.setItem('admin_art_view_mode', mode);
  loadArtists();
}
function artGoPage(offset) { _artOffset = offset; loadArtists(); }

const ART_COLUMNS_DEF = [
  {key:'name',       label:'Исполнитель', minWidth:120, defaultWidth:180, sortable:true, filterable:true},
  {key:'type',       label:'Тип',         minWidth:80,  defaultWidth:120, sortable:true, filterable:true},
  {key:'genres',     label:'Жанры',       minWidth:80,  defaultWidth:120, sortable:true, filterable:true},
  {key:'city',       label:'Город',       minWidth:70,  defaultWidth:110, sortable:true, filterable:true},
  {key:'username',   label:'Владелец',    minWidth:90,  defaultWidth:130, sortable:true, filterable:true},
  {key:'releases',   label:'Релизов',     minWidth:70,  defaultWidth:90,  sortable:true},
  {key:'opens',      label:'Открытий',    minWidth:70,  defaultWidth:100, sortable:true},
  {key:'created_at', label:'Создан',      minWidth:90,  defaultWidth:135, sortable:true},
  {key:'status',     label:'Статус',      minWidth:70,  defaultWidth:100, sortable:true, filterable:true},
];
const ART_COLS_STORAGE_KEY = 'admin_art_columns_v1';

function artLoadColumnPrefs() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(ART_COLS_STORAGE_KEY) || '{}'); } catch(e) {}
  const allKeys = ART_COLUMNS_DEF.map(c => c.key);
  let order = Array.isArray(saved.order) ? saved.order.filter(k => allKeys.includes(k)) : [];
  allKeys.forEach(k => { if (!order.includes(k)) order.push(k); });
  const widths = saved.widths || {};
  const visible = saved.visible || {};
  ART_COLUMNS_DEF.forEach(c => {
    if (typeof widths[c.key] !== 'number') widths[c.key] = c.defaultWidth;
    if (typeof visible[c.key] !== 'boolean') visible[c.key] = true;
    if (c.locked) visible[c.key] = true;
  });
  return {order, widths, visible};
}
let artCols = artLoadColumnPrefs();
function artSaveColumnPrefs() { localStorage.setItem(ART_COLS_STORAGE_KEY, JSON.stringify(artCols)); }
function artVisibleColumns() {
  return artCols.order.filter(k => artCols.visible[k] !== false).map(k => ART_COLUMNS_DEF.find(c => c.key===k));
}
function artToggleColumn(key, checked) {
  artCols.visible[key] = checked;
  artSaveColumnPrefs();
  loadArtists();
}
function artToggleColsPanel() {
  const p = document.getElementById('art-cols-panel');
  if (p) p.style.display = p.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', (e) => {
  const panel = document.getElementById('art-cols-panel');
  const btn = document.getElementById('art-cols-btn');
  if (panel && panel.style.display !== 'none' && !panel.contains(e.target) && e.target !== btn) {
    panel.style.display = 'none';
  }
});

let _artDragKey = null;
function artColDragStart(e) { _artDragKey = e.currentTarget.dataset.col; e.dataTransfer.effectAllowed = 'move'; }
function artColDragOver(e) { e.preventDefault(); }
function artColDrop(e) {
  e.preventDefault();
  const targetKey = e.currentTarget.dataset.col;
  if (!_artDragKey || _artDragKey === targetKey) return;
  const order = artCols.order;
  const from = order.indexOf(_artDragKey);
  const to = order.indexOf(targetKey);
  if (from === -1 || to === -1) return;
  order.splice(from, 1);
  order.splice(to, 0, _artDragKey);
  artSaveColumnPrefs();
  loadArtists();
}

let _artResizeState = null;
function artColResizeStart(e, key) {
  e.preventDefault(); e.stopPropagation();
  _artResizeState = { key, startX: e.clientX, startWidth: artCols.widths[key] };
  document.addEventListener('mousemove', artColResizeMove);
  document.addEventListener('mouseup', artColResizeEnd);
}
function artColResizeMove(e) {
  if (!_artResizeState) return;
  const def = ART_COLUMNS_DEF.find(c => c.key === _artResizeState.key);
  const delta = e.clientX - _artResizeState.startX;
  const newWidth = Math.max(def.minWidth, _artResizeState.startWidth + delta);
  artCols.widths[_artResizeState.key] = newWidth;
  const idx = artVisibleColumns().findIndex(c => c.key === _artResizeState.key);
  const table = document.querySelector('#artists-content table.art-table');
  if (table && idx !== -1) {
    const colEl = table.querySelectorAll('colgroup col')[idx];
    if (colEl) colEl.style.width = newWidth + 'px';
  }
}
function artColResizeEnd() {
  document.removeEventListener('mousemove', artColResizeMove);
  document.removeEventListener('mouseup', artColResizeEnd);
  _artResizeState = null;
  artSaveColumnPrefs();
}

function artSortBy(key) {
  const def = ART_COLUMNS_DEF.find(c => c.key === key);
  if (!def || def.sortable === false) return;
  if (_artSortKey === key) { _artSortDir = _artSortDir === 'desc' ? 'asc' : 'desc'; }
  else { _artSortKey = key; _artSortDir = 'desc'; }
  _artOffset = 0;
  loadArtists();
}
function artFilterInput(key, value) {
  _artColFilters[key] = value;
  clearTimeout(_artSearchTimer);
  _artSearchTimer = setTimeout(() => {
    _artOffset = 0;
    withFocusPreserved(loadArtists);
  }, 300);
}

function artCellHtml(a, key) {
  if (key === 'name') return `<span class="st-dot ${a.status==='active'?'st-dot-on':'st-dot-off'}"></span> <a style="color:#7eb8f7;cursor:pointer" onclick="openArtistCard(${a.id})">🎤 ${esc(a.name)}</a>`;
  if (key === 'type') return esc(a.type);
  if (key === 'genres') return esc(a.genres || '—');
  if (key === 'city') return esc(a.city || '—');
  if (key === 'username') return a.username ? '@'+esc(a.username) : String(a.owner_id);
  if (key === 'releases') return a.releases
    ? `<a style="color:#7eb8f7;cursor:pointer;font-weight:700" onclick="openArtistReleases(${a.id})">${a.releases}</a>`
    : '0';
  if (key === 'opens') return `<a style="color:#9bc;cursor:pointer;font-weight:700" onclick="openOpenersModal('artist',${a.id},${jsArg('Кто открывал: '+a.name)})">${a.opens}</a>`;
  if (key === 'created_at') return fmtDateTime(a.created_at);
  if (key === 'status') return a.status==='active' ? '<span style="color:#6ef5aa">активен</span>' : '<span style="color:#888">скрыт</span>';
  return '';
}

function artTableHtml(rows) {
  const cols = artVisibleColumns();
  const colgroup = cols.map(c => `<col style="width:${artCols.widths[c.key]}px">`).join('');
  const thead = cols.map(c => {
    const sortable = c.sortable !== false;
    const arrow = _artSortKey === c.key ? (_artSortDir === 'desc' ? ' ▼' : ' ▲') : '';
    return `<th class="txt-th" data-col="${c.key}" draggable="${!c.locked}"
      ondragstart="artColDragStart(event)" ondragover="artColDragOver(event)" ondrop="artColDrop(event)"
      style="padding:6px 8px;position:relative;${c.locked?'':'cursor:grab'}${sortable?';cursor:pointer':''}"
      ${sortable ? `onclick="artSortBy('${c.key}')"` : ''}>${esc(c.label)}${arrow}<span class="col-resize" onmousedown="artColResizeStart(event,'${c.key}')"></span></th>`;
  }).join('');
  const filterRow = cols.map(c => `<th style="padding:2px 6px">${c.filterable
    ? `<input type="text" id="art-filter-${c.key}" value="${esc(_artColFilters[c.key]||'')}" placeholder="…"
        oninput="artFilterInput('${c.key}', this.value)"
        style="width:100%;background:#0d1424;color:#dde;border:1px solid #223;border-radius:4px;padding:3px 6px;font-size:11px">`
    : ''}</th>`).join('');
  const tbody = rows.map(a => `
    <tr style="border-top:1px solid #0d1628">
      ${cols.map(c => `<td class="txt-td" style="padding:6px 8px">${artCellHtml(a, c.key)}</td>`).join('')}
    </tr>`).join('');
  const colsPanel = ART_COLUMNS_DEF.filter(c => !c.locked).map(c => `
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;padding:4px 0">
        <input type="checkbox" ${artCols.visible[c.key]!==false?'checked':''}
          onchange="artToggleColumn('${c.key}', this.checked)"> ${esc(c.label)}
      </label>`).join('');
  return `
  <div style="display:flex;justify-content:flex-end;margin-bottom:8px;position:relative">
    <button class="btn btn-ghost btn-sm" id="art-cols-btn" onclick="artToggleColsPanel()">⚙ Столбцы</button>
    <div id="art-cols-panel" style="display:none;position:absolute;right:0;top:28px;background:#1e1e35;
      border:1px solid #333;border-radius:8px;padding:10px 14px;z-index:20;min-width:150px">${colsPanel}</div>
  </div>
  <table class="art-table" style="width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed">
    <colgroup>${colgroup}</colgroup>
    <thead>
      <tr style="color:#556;font-size:11px;text-align:left">${thead}</tr>
      <tr>${filterRow}</tr>
    </thead>
    <tbody>${tbody}</tbody>
  </table>`;
}

function artCardHtml(a) {
  const statusBadge = a.status === 'active'
    ? '<span class="cat-card-status st-active">активен</span>'
    : '<span class="cat-card-status st-inactive">скрыт</span>';
  const meta = [a.type, [a.genres,a.city].filter(Boolean).join(' · ')].filter(Boolean).join(' · ');
  return `<div class="cat-card${a.status==='active'?'':' cat-card-off'}" onclick="openArtistCard(${a.id})">
    <div class="cat-card-media">
      <div class="cat-card-media-empty">🎤</div>
      ${statusBadge}
    </div>
    <div class="cat-card-body">
      <div class="cat-card-title">${esc(a.name)}</div>
      <div class="cat-card-meta">${esc(meta || '—')}</div>
      <div class="cat-card-stats">🎵 ${a.releases||0} релизов · 👁 ${a.opens||0}</div>
    </div>
  </div>`;
}

async function loadArtists() {
  const el = document.getElementById('artists-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const params = new URLSearchParams({offset: _artOffset, limit: ART_LIMIT, sort: _artSortKey, order: _artSortDir});
  if (_artQuery) params.set('q', _artQuery);
  const activeFilters = Object.fromEntries(Object.entries(_artColFilters).filter(([,v]) => v));
  if (Object.keys(activeFilters).length) params.set('filters', JSON.stringify(activeFilters));
  const data = await fetch(`/api/artists?${params}`).then(r=>r.json());
  const rows = data.rows || [];
  const viewToggle = `<div style="display:flex;gap:4px">
    <button class="btn btn-sm" style="${_artViewMode==='grid'?'background:#2a3a70':''}" onclick="artSetViewMode('grid')">🖼 Превью</button>
    <button class="btn btn-sm" style="${_artViewMode==='table'?'background:#2a3a70':''}" onclick="artSetViewMode('table')">📋 Таблица</button>
  </div>`;
  const topBar = `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap">
    <span style="font-size:12px;color:#556">${data.total||0} исполнителей</span>
    ${viewToggle}
  </div>`;
  if (!rows.length) {
    const emptyMsg = '<div class="empty" style="padding:20px;text-align:center">Исполнителей не найдено</div>';
    el.innerHTML = topBar + (_artViewMode === 'table' ? artTableHtml(rows) + emptyMsg : emptyMsg);
    return;
  }
  const pag = paginationHtml(data.total||0, ART_LIMIT, _artOffset, 'artGoPage');
  const body = _artViewMode === 'table' ? artTableHtml(rows) : `<div class="catalog-listings-grid">${rows.map(a=>artCardHtml(a)).join('')}</div>`;
  el.innerHTML = topBar + body + pag;
}

// ── Обратная связь ──
let fbUnansweredOnly = false, fbOffset = 0;
const FB_LIMIT = 20;

function fbSetFilter(flag) {
  fbUnansweredOnly = flag;
  fbOffset = 0;
  document.getElementById('fb-tab-all').classList.toggle('btn-primary', !flag);
  document.getElementById('fb-tab-unanswered').classList.toggle('btn-primary', flag);
  loadFeedback();
}

function fbMarker(r) {
  if (r.answered) return '✅';
  if (r.needs_reply) return '🔔';
  if (!r.is_read) return '•';
  return '';
}

async function loadFeedback() {
  const el = document.getElementById('feedback-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const data = await fetch(`/api/feedback?unanswered=${fbUnansweredOnly?1:0}&offset=${fbOffset}&limit=${FB_LIMIT}`).then(r=>r.json());
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = `<div class="empty" style="padding:30px;text-align:center">${fbUnansweredOnly ? 'Неотвеченных обращений нет 🎉' : 'Обращений пока нет'}</div>`;
    return;
  }
  el.innerHTML = `<div style="font-size:12px;color:#556;margin-bottom:10px">${data.total} обращений</div>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:6px 8px"></th>
      <th style="padding:6px 8px">От кого</th>
      <th style="padding:6px 8px">Когда</th>
      <th style="padding:6px 8px">Сообщение</th>
      <th style="padding:6px 8px"></th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr style="border-top:1px solid #0d1628">
        <td style="padding:7px 8px">${fbMarker(r)}</td>
        <td style="padding:7px 8px;font-weight:600;color:#99a">${r.username?'@'+esc(r.username):'id'+r.user_id}</td>
        <td style="padding:7px 8px;color:#778">${esc(r.created_at)}</td>
        <td style="padding:7px 8px;color:#ccd;cursor:pointer" onclick="openFeedback(${r.id})"
          onmouseenter="fbTipShow(event,this)" onmousemove="fbTipMove(event)" onmouseleave="fbTipHide()"
          data-tip="${esc(r.message||'')}">
          ${esc((r.message||'').slice(0,60))}${(r.message||'').length>60?'…':''}</td>
        <td style="padding:7px 8px;white-space:nowrap">
          <button class="btn btn-sm" onclick="openFeedback(${r.id})">👁 Открыть</button>
          <button class="btn btn-sm btn-del" onclick="fbDelete(${r.id})">🗑</button>
        </td>
      </tr>`).join('')}</tbody>
  </table>
  ${paginationHtml(data.total, FB_LIMIT, fbOffset, 'fbPage')}`;
}

function fbPage(offset) {
  fbOffset = offset;
  loadFeedback();
}

async function openFeedback(id) {
  document.getElementById('feedback-modal-content').innerHTML = '<div class="empty" style="padding:40px;text-align:center">…</div>';
  openOverlay('modal-feedback');
  try {
    const d = await fetch(`/api/feedback/${id}`).then(r=>r.json());
    renderFeedbackModal(d);
  } catch(e) {
    document.getElementById('feedback-modal-content').innerHTML = `<div class="empty" style="color:#f66">Ошибка: ${esc(e.message)}</div>`;
  }
}

function renderFeedbackModal(d) {
  const el = document.getElementById('feedback-modal-content');
  const who = d.username ? '@'+esc(d.username) : 'id'+d.user_id;
  const answerBlock = d.answer_text
    ? `<div style="margin-top:14px"><b>Ответ администратора:</b><br>${esc(d.answer_text).replace(/\n/g,'<br>')}</div>`
    : `<div style="margin-top:14px;color:#889">Пока не отвечено.</div>`;
  el.innerHTML = `
    <div style="font-size:12px;color:#778;margin-bottom:8px">Обращение №${d.id} · ${who} · ${esc(d.created_at)}</div>
    <div><b>Сообщение:</b><br>${esc(d.message).replace(/\n/g,'<br>')}</div>
    ${answerBlock}
    <div style="margin-top:16px">
      <textarea id="fb-reply-input" placeholder="Ваш ответ пользователю…"
        style="width:100%;min-height:90px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:8px;font:inherit;box-sizing:border-box"></textarea>
    </div>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn btn-primary" onclick="fbSendReply(${d.id})">✍️ Отправить ответ</button>
      <button class="btn btn-del btn-sm" onclick="fbDelete(${d.id}, true)">🗑 Удалить</button>
    </div>
    <div id="fb-reply-status" style="margin-top:8px;font-size:12px"></div>`;
}

async function fbSendReply(id) {
  const input = document.getElementById('fb-reply-input');
  const text = (input.value || '').trim();
  if (!text) return;
  const statusEl = document.getElementById('fb-reply-status');
  statusEl.style.color = '#889';
  statusEl.textContent = 'Отправка…';
  try {
    const r = await fetch(`/api/feedback/${id}/reply`, {
      method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({text})
    }).then(x=>x.json());
    if (r.ok) {
      // Явное подтверждение вместо перерисовки карточки — чтобы не было
      // неясности, отправилось или нет; «ОК» закрывает модалку и
      // возвращает к списку с уже обновлённым статусом.
      document.getElementById('feedback-modal-content').innerHTML = `
        <div style="text-align:center;padding:24px 0">
          <div style="font-size:34px;margin-bottom:10px">✅</div>
          <div style="font-size:15px;color:#dde;margin-bottom:22px">Ответ отправлен</div>
          <button class="btn btn-primary" onclick="fbConfirmSentClose()">ОК</button>
        </div>`;
    } else {
      statusEl.style.color = '#f66';
      statusEl.textContent = '⚠️ ' + (r.detail || 'не удалось доставить');
    }
  } catch(e) {
    statusEl.style.color = '#f66';
    statusEl.textContent = 'Ошибка: ' + e.message;
  }
}

function fbConfirmSentClose() {
  closeFeedbackModal();
  loadFeedback();
}

async function fbDelete(id, fromModal) {
  if (!confirm('Удалить это обращение навсегда?')) return;
  await fetch(`/api/feedback/${id}`, {method:'DELETE'});
  if (fromModal) closeFeedbackModal();
  loadFeedback();
}

function closeFeedbackModal() {
  document.getElementById('modal-feedback').classList.remove('open');
}

// ── Тексты (BotText + menu) ──
let txtSubtab = 'bottext';
let txtQuery = '';
let txtOffset = 0;
let txtPageSize = parseInt(localStorage.getItem('admin_txt_page_size') || '50', 10) || 50;
function txtSetPageSize(val) {
  txtPageSize = parseInt(val, 10) || 50;
  localStorage.setItem('admin_txt_page_size', String(txtPageSize));
  txtOffset = 0;
  loadTexts();
}
let _txtSearchTimer = null;

function txtSetSubtab(name) {
  txtSubtab = name;
  document.getElementById('txt-subtab-bottext').classList.toggle('btn-primary', name==='bottext');
  document.getElementById('txt-subtab-menu').classList.toggle('btn-primary', name==='menu');
  document.getElementById('texts-bottext-view').style.display = name==='bottext' ? '' : 'none';
  document.getElementById('texts-menu-view').style.display = name==='menu' ? '' : 'none';
  if (name==='bottext') loadTexts();
  else loadMenuItems();
}

function txtSearchDebounced() {
  clearTimeout(_txtSearchTimer);
  _txtSearchTimer = setTimeout(() => {
    txtQuery = document.getElementById('txt-search').value;
    txtOffset = 0;
    loadTexts();
  }, 300);
}

// ── Настраиваемые столбцы таблицы текстов: показать/скрыть, ширина, порядок.
// Настройки живут в localStorage браузера — это локальный инструмент одного
// администратора, серверное хранение не нужно.
const TXT_COLUMNS_DEF = [
  {key:'code',    label:'Код',      minWidth:110, defaultWidth:170, locked:true},
  {key:'title',   label:'Название', minWidth:100, defaultWidth:200},
  {key:'text_ru', label:'RU',       minWidth:120, defaultWidth:300},
  {key:'text_en', label:'EN',       minWidth:120, defaultWidth:300},
  {key:'text_kk', label:'KK',       minWidth:120, defaultWidth:300},
];
const TXT_COLS_STORAGE_KEY = 'admin_txt_columns_v1';

function txtLoadColumnPrefs() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(TXT_COLS_STORAGE_KEY) || '{}'); } catch(e) {}
  const allKeys = TXT_COLUMNS_DEF.map(c => c.key);
  let order = Array.isArray(saved.order) ? saved.order.filter(k => allKeys.includes(k)) : [];
  allKeys.forEach(k => { if (!order.includes(k)) order.push(k); });
  const widths = saved.widths || {};
  const visible = saved.visible || {};
  TXT_COLUMNS_DEF.forEach(c => {
    if (typeof widths[c.key] !== 'number') widths[c.key] = c.defaultWidth;
    if (typeof visible[c.key] !== 'boolean') visible[c.key] = true;
    if (c.locked) visible[c.key] = true;
  });
  return {order, widths, visible};
}
let txtCols = txtLoadColumnPrefs();
function txtSaveColumnPrefs() {
  localStorage.setItem(TXT_COLS_STORAGE_KEY, JSON.stringify(txtCols));
}
function txtVisibleColumns() {
  return txtCols.order
    .filter(k => txtCols.visible[k] !== false)
    .map(k => TXT_COLUMNS_DEF.find(c => c.key===k));
}
function txtToggleColumn(key, checked) {
  txtCols.visible[key] = checked;
  txtSaveColumnPrefs();
  loadTexts();
}
function txtToggleColsPanel() {
  const p = document.getElementById('txt-cols-panel');
  if (p) p.style.display = p.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', (e) => {
  const panel = document.getElementById('txt-cols-panel');
  const btn = document.getElementById('txt-cols-btn');
  if (panel && panel.style.display !== 'none' && !panel.contains(e.target) && e.target !== btn) {
    panel.style.display = 'none';
  }
});

// Перетаскивание заголовков столбцов для смены порядка
let _txtDragKey = null;
function txtColDragStart(e) {
  _txtDragKey = e.currentTarget.dataset.col;
  e.dataTransfer.effectAllowed = 'move';
}
function txtColDragOver(e) { e.preventDefault(); }
function txtColDrop(e) {
  e.preventDefault();
  const targetKey = e.currentTarget.dataset.col;
  if (!_txtDragKey || _txtDragKey === targetKey) return;
  const order = txtCols.order;
  const from = order.indexOf(_txtDragKey);
  const to = order.indexOf(targetKey);
  if (from === -1 || to === -1) return;
  order.splice(from, 1);
  order.splice(to, 0, _txtDragKey);
  txtSaveColumnPrefs();
  loadTexts();
}

// Растягивание столбца за правый край заголовка
let _txtResizeState = null;
function txtColResizeStart(e, key) {
  e.preventDefault();
  e.stopPropagation();
  _txtResizeState = { key, startX: e.clientX, startWidth: txtCols.widths[key] };
  document.addEventListener('mousemove', txtColResizeMove);
  document.addEventListener('mouseup', txtColResizeEnd);
}
function txtColResizeMove(e) {
  if (!_txtResizeState) return;
  const def = TXT_COLUMNS_DEF.find(c => c.key === _txtResizeState.key);
  const delta = e.clientX - _txtResizeState.startX;
  const newWidth = Math.max(def.minWidth, _txtResizeState.startWidth + delta);
  txtCols.widths[_txtResizeState.key] = newWidth;
  const idx = txtVisibleColumns().findIndex(c => c.key === _txtResizeState.key);
  const table = document.querySelector('#texts-content table');
  if (table && idx !== -1) {
    const colEl = table.querySelectorAll('colgroup col')[idx];
    if (colEl) colEl.style.width = newWidth + 'px';
  }
}
function txtColResizeEnd() {
  document.removeEventListener('mousemove', txtColResizeMove);
  document.removeEventListener('mouseup', txtColResizeEnd);
  _txtResizeState = null;
  txtSaveColumnPrefs();
}

function txtCellHtml(row, key) {
  if (key === 'code') return `<span style="font-family:monospace;color:#9bc">${esc(row.code)}</span>`;
  const val = row[key] || '';
  const preview = val.slice(0, 70).replace(/\n/g, ' ');
  return esc(preview) + (val.length > 70 ? '…' : '');
}

async function loadTexts() {
  const el = document.getElementById('texts-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const data = await fetch(`/api/texts?q=${encodeURIComponent(txtQuery)}&offset=${txtOffset}&limit=${txtPageSize}`).then(r=>r.json());
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Ничего не найдено</div>';
    return;
  }
  const cols = txtVisibleColumns();
  const colgroup = cols.map(c => `<col style="width:${txtCols.widths[c.key]}px">`).join('') + '<col style="width:90px">';
  const thead = cols.map(c => `
      <th class="txt-th" data-col="${c.key}" draggable="${!c.locked}"
        ondragstart="txtColDragStart(event)" ondragover="txtColDragOver(event)" ondrop="txtColDrop(event)"
        style="padding:6px 8px;position:relative;${c.locked?'':'cursor:grab'}">
        ${esc(c.label)}<span class="col-resize" onmousedown="txtColResizeStart(event,'${c.key}')"></span>
      </th>`).join('') + '<th></th>';
  const tbody = rows.map(r => `
      <tr style="border-top:1px solid #0d1628;cursor:pointer" onclick="openText(${jsArg(r.code)})">
        ${cols.map(c => `<td class="txt-td" style="padding:7px 8px">${txtCellHtml(r, c.key)}</td>`).join('')}
        <td style="padding:7px 8px;white-space:nowrap"><button class="btn btn-sm" onclick="event.stopPropagation();openText(${jsArg(r.code)})">✏️ Править</button></td>
      </tr>`).join('');
  const colsPanel = TXT_COLUMNS_DEF.map(c => `
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;padding:4px 0;${c.locked?'opacity:.5':''}">
        <input type="checkbox" ${txtCols.visible[c.key]!==false?'checked':''} ${c.locked?'disabled':''}
          onchange="txtToggleColumn('${c.key}', this.checked)"> ${esc(c.label)}
      </label>`).join('');
  const pageSizeOptions = [10, 20, 50, 100].map(n =>
    `<option value="${n}" ${txtPageSize===n?'selected':''}>${n}</option>`).join('');
  el.innerHTML = `
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
    <div style="font-size:12px;color:#556">${data.total} текстов</div>
    <div style="position:relative">
      <button class="btn btn-ghost btn-sm" id="txt-cols-btn" onclick="txtToggleColsPanel()">⚙ Столбцы</button>
      <div id="txt-cols-panel" style="display:none;position:absolute;right:0;top:28px;background:#1e1e35;
        border:1px solid #333;border-radius:8px;padding:10px 14px;z-index:20;min-width:150px">${colsPanel}</div>
    </div>
  </div>
  <table style="width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed">
    <colgroup>${colgroup}</colgroup>
    <thead><tr style="color:#556;font-size:11px;text-align:left">${thead}</tr></thead>
    <tbody>${tbody}</tbody>
  </table>
  <div class="an-pagination">
    <label style="display:flex;align-items:center;gap:6px">на странице
      <select onchange="txtSetPageSize(this.value)" style="background:#1a2050;color:#aab;border:none;border-radius:5px;padding:4px 6px;font:inherit">${pageSizeOptions}</select>
    </label>
  </div>
  ${paginationHtml(data.total, txtPageSize, txtOffset, 'txtPage')}`;
}

function txtPage(offset) {
  txtOffset = offset;
  loadTexts();
}

async function openText(code) {
  document.getElementById('text-modal-content').innerHTML = '<div class="empty" style="padding:40px;text-align:center">…</div>';
  openOverlay('modal-text');
  try {
    const d = await fetch(`/api/texts/${encodeURIComponent(code)}`).then(r=>r.json());
    renderTextModal(d);
  } catch(e) {
    document.getElementById('text-modal-content').innerHTML = `<div class="empty" style="color:#f66">Ошибка: ${esc(e.message)}</div>`;
  }
}

// ── Форматированный редактор (B/I/U) ──
// contenteditable показывает текст ТАК, КАК ОН БУДЕТ ВЫГЛЯДЕТЬ в Telegram
// (реальные жирный/курсив/подчёркнутый), вместо того чтобы вручную писать
// <b>/<i>/<u>. Но при сохранении содержимое ВСЕГДА прогоняется через
// sanitizeTelegramHtml() — она оставляет только теги, которые понимает
// Telegram (b/i/u, плюс то, что уже было в тексте: code/a), и превращает
// любой браузерный мусор (div/span/style от вставки, переносы строк) в
// обычный текст с переносами \n. Это и есть защита от «бот упал с ошибкой
// парсинга» — риск, о котором предупреждали при выборе WYSIWYG-подхода.
function txtRteToolbar(editId) {
  return `<div class="rte-toolbar">
    <button type="button" title="Жирный" onmousedown="event.preventDefault()" onclick="txtExecCmd('${editId}','bold')"><b>B</b></button>
    <button type="button" title="Курсив" onmousedown="event.preventDefault()" onclick="txtExecCmd('${editId}','italic')"><i>I</i></button>
    <button type="button" title="Подчёркнутый" onmousedown="event.preventDefault()" onclick="txtExecCmd('${editId}','underline')"><u>U</u></button>
  </div>`;
}

function txtExecCmd(editId, cmd) {
  document.getElementById(editId).focus();
  document.execCommand(cmd, false, null);
}

// Enter не должен создавать <div>/<p> (обычное поведение contenteditable) —
// иначе на сохранении санитайзер получит блочную разметку вместо простого
// текста. Вставляем обычный символ переноса строки в текстовый узел.
function txtEditorKeydown(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const range = sel.getRangeAt(0);
  range.deleteContents();
  const node = document.createTextNode('\n');
  range.insertNode(node);
  range.setStartAfter(node);
  range.setEndAfter(node);
  sel.removeAllRanges();
  sel.addRange(range);
}

function _rteEscText(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
function _rteEscAttr(s) {
  return s.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Строгий белый список: контенту из contenteditable нельзя доверять как есть
// (браузер/паста может принести что угодно) — на выходе только теги,
// которые Telegram parse_mode="HTML" реально понимает.
const RTE_TAG_MAP = {b:'b', strong:'b', i:'i', em:'i', u:'u', ins:'u', s:'s', strike:'s', del:'s', code:'code', pre:'pre', a:'a'};

function sanitizeTelegramHtml(container) {
  let out = '';
  function walk(n) {
    if (n.nodeType === Node.TEXT_NODE) {
      out += _rteEscText(n.nodeValue);
      return;
    }
    if (n.nodeType !== Node.ELEMENT_NODE) return;
    const tag = n.tagName.toLowerCase();
    if (tag === 'script' || tag === 'style' || tag === 'noscript') {
      // Не унаследованный мусор от вставки — целиком выбрасываем (и тег,
      // и содержимое), в отличие от span/font, у которых снимаем только тег.
      return;
    }
    if (tag === 'br') { out += '\n'; return; }
    if (tag === 'div' || tag === 'p') {
      // Блочные теги (обычно из вставки чужого HTML) — не переносим как есть,
      // Telegram их не поддерживает; вместо этого просто добавляем перенос строки.
      if (out.length && !out.endsWith('\n')) out += '\n';
      Array.from(n.childNodes).forEach(walk);
      return;
    }
    const mapped = RTE_TAG_MAP[tag];
    if (mapped === 'a') {
      const href = n.getAttribute('href') || '';
      // Разрешаем только http(s)-ссылки — javascript:/data: и подобные схемы
      // не поддерживаются Telegram и не должны просачиваться в текст.
      if (/^https?:\/\//i.test(href.trim())) {
        out += `<a href="${_rteEscAttr(href.trim())}">`;
        Array.from(n.childNodes).forEach(walk);
        out += '</a>';
      } else {
        Array.from(n.childNodes).forEach(walk);
      }
      return;
    }
    if (mapped) {
      out += `<${mapped}>`;
      Array.from(n.childNodes).forEach(walk);
      out += `</${mapped}>`;
      return;
    }
    // Неизвестный тег (span со стилями от браузера, font и т.п.) — снимаем
    // тег, оставляя только его содержимое.
    Array.from(n.childNodes).forEach(walk);
  }
  Array.from(container.childNodes).forEach(walk);
  return out;
}

function renderTextModal(d) {
  const el = document.getElementById('text-modal-content');
  el.innerHTML = `
    <div style="font-size:12px;color:#778;margin-bottom:4px">Код: <span style="font-family:monospace;color:#9bc">${esc(d.code)}</span></div>
    <div style="margin-bottom:10px">
      <label style="display:block;font-size:12px;color:#889;margin-bottom:4px">Название (для себя, не видно пользователям)</label>
      <input id="txt-title-input" value="${esc(d.title||'')}"
        style="width:100%;background:#0d1424;color:#dde;border:1px solid #223;border-radius:6px;padding:7px 10px;font:inherit;box-sizing:border-box">
    </div>
    <div style="margin-bottom:10px">
      <label style="display:block;font-size:12px;color:#889;margin-bottom:4px">Текст RU</label>
      ${txtRteToolbar('txt-ru-edit')}
      <div id="txt-ru-edit" class="rte-edit" contenteditable="true" onkeydown="txtEditorKeydown(event)">${d.text_ru||''}</div>
    </div>
    <div style="margin-bottom:10px">
      <label style="display:block;font-size:12px;color:#889;margin-bottom:4px">Текст EN</label>
      ${txtRteToolbar('txt-en-edit')}
      <div id="txt-en-edit" class="rte-edit" contenteditable="true" onkeydown="txtEditorKeydown(event)">${d.text_en||''}</div>
    </div>
    <div style="margin-bottom:10px">
      <label style="display:block;font-size:12px;color:#889;margin-bottom:4px">Текст KK</label>
      ${txtRteToolbar('txt-kk-edit')}
      <div id="txt-kk-edit" class="rte-edit" contenteditable="true" onkeydown="txtEditorKeydown(event)">${d.text_kk||''}</div>
    </div>
    <div style="font-size:11px;color:#556;margin-bottom:12px">Показано так, как будет выглядеть в Telegram. Выделите текст и нажмите B/I/U для форматирования.</div>
    <div style="display:flex;gap:8px">
      <button class="btn btn-primary" onclick="txtSave(${jsArg(d.code)})">💾 Сохранить</button>
    </div>
    <div id="txt-save-status" style="margin-top:8px;font-size:12px"></div>`;
}

async function txtSave(code) {
  const title = document.getElementById('txt-title-input').value;
  const text_ru = sanitizeTelegramHtml(document.getElementById('txt-ru-edit'));
  const text_en = sanitizeTelegramHtml(document.getElementById('txt-en-edit'));
  const text_kk = sanitizeTelegramHtml(document.getElementById('txt-kk-edit'));
  const statusEl = document.getElementById('txt-save-status');
  statusEl.style.color = '#889';
  statusEl.textContent = 'Сохранение…';
  try {
    const r = await fetch(`/api/texts/${encodeURIComponent(code)}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({title, text_ru, text_en, text_kk})
    }).then(x=>x.json());
    if (r.ok) {
      statusEl.style.color = '#7c9';
      statusEl.textContent = '✅ Сохранено';
      loadTexts();
    } else {
      statusEl.style.color = '#f66';
      statusEl.textContent = '⚠️ ' + (r.detail || 'не удалось сохранить');
    }
  } catch(e) {
    statusEl.style.color = '#f66';
    statusEl.textContent = 'Ошибка: ' + e.message;
  }
}

function closeTextModal() {
  document.getElementById('modal-text').classList.remove('open');
}

async function loadMenuItems() {
  const el = document.getElementById('menu-items-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const data = await fetch('/api/menu').then(r=>r.json());
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Пунктов меню нет</div>';
    return;
  }
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:6px 8px">Код</th>
      <th style="padding:6px 8px">Иконка</th>
      <th style="padding:6px 8px">Текст RU</th>
      <th style="padding:6px 8px">Текст EN</th>
      <th style="padding:6px 8px">Текст KK</th>
      <th style="padding:6px 8px">Видно</th>
      <th style="padding:6px 8px"></th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr style="border-top:1px solid #0d1628" id="menu-row-${r.id}">
        <td style="padding:7px 8px;font-family:monospace;color:#9bc">${esc(r.code)}${r.parent_code ? `<div style="color:#556;font-size:11px">в ${esc(r.parent_code)}</div>` : ''}</td>
        <td style="padding:7px 8px"><input id="menu-icon-${r.id}" value="${esc(r.icon||'')}" style="width:44px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:5px;padding:5px 6px;font:inherit;text-align:center"></td>
        <td style="padding:7px 8px"><input id="menu-text-${r.id}" value="${esc(r.text||'')}" style="width:100%;min-width:120px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:5px;padding:5px 8px;font:inherit"></td>
        <td style="padding:7px 8px"><input id="menu-texten-${r.id}" value="${esc(r.text_en||'')}" style="width:100%;min-width:120px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:5px;padding:5px 8px;font:inherit"></td>
        <td style="padding:7px 8px"><input id="menu-textkk-${r.id}" value="${esc(r.text_kk||'')}" style="width:100%;min-width:120px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:5px;padding:5px 8px;font:inherit"></td>
        <td style="padding:7px 8px;text-align:center"><input type="checkbox" id="menu-vis-${r.id}" ${r.visible?'checked':''}></td>
        <td style="padding:7px 8px;white-space:nowrap">
          <button class="btn btn-sm" onclick="menuSave(${r.id})">💾</button>
          <span id="menu-status-${r.id}" style="font-size:11px;margin-left:6px"></span>
        </td>
      </tr>`).join('')}</tbody>
  </table>`;
}

async function menuSave(id) {
  const text = document.getElementById(`menu-text-${id}`).value;
  const text_en = document.getElementById(`menu-texten-${id}`).value;
  const text_kk = document.getElementById(`menu-textkk-${id}`).value;
  const icon = document.getElementById(`menu-icon-${id}`).value;
  const visible = document.getElementById(`menu-vis-${id}`).checked;
  const statusEl = document.getElementById(`menu-status-${id}`);
  statusEl.style.color = '#889';
  statusEl.textContent = '…';
  try {
    const r = await fetch(`/api/menu/${id}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text, text_en, text_kk, icon, visible})
    }).then(x=>x.json());
    statusEl.style.color = r.ok ? '#7c9' : '#f66';
    statusEl.textContent = r.ok ? '✅' : '⚠️';
  } catch(e) {
    statusEl.style.color = '#f66';
    statusEl.textContent = '⚠️';
  }
}

// ── Настройки (feature_flags) ──
async function loadFeatureFlags() {
  const el = document.getElementById('feature-flags-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const data = await fetch('/api/feature_flags').then(r=>r.json());
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Флагов нет</div>';
    return;
  }
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:6px 8px">Ключ</th>
      <th style="padding:6px 8px;text-align:center">Включено</th>
      <th style="padding:6px 8px">Аудитория</th>
      <th style="padding:6px 8px">Заметка</th>
      <th style="padding:6px 8px">Изменено</th>
      <th style="padding:6px 8px"></th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr style="border-top:1px solid #0d1628" id="ff-row-${r.id}">
        <td style="padding:7px 8px;font-family:monospace;color:#9bc">${esc(r.key)}</td>
        <td style="padding:7px 8px;text-align:center">
          <label class="ff-switch">
            <input type="checkbox" id="ff-enabled-${r.id}" ${r.enabled?'checked':''}>
            <span class="ff-switch-track"></span>
          </label>
        </td>
        <td style="padding:7px 8px"><input id="ff-audience-${r.id}" value="${esc(r.audience)}"
          title="all / admins / список user_id через запятую"
          style="width:100%;min-width:110px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:5px;padding:5px 8px;font:inherit"></td>
        <td style="padding:7px 8px"><input id="ff-note-${r.id}" value="${esc(r.note)}"
          style="width:100%;min-width:140px;background:#0d1424;color:#dde;border:1px solid #223;border-radius:5px;padding:5px 8px;font:inherit"></td>
        <td style="padding:7px 8px;color:#556;font-size:11px;white-space:nowrap">${esc(r.updated_at)}</td>
        <td style="padding:7px 8px;white-space:nowrap">
          <button class="btn btn-sm" onclick="featureFlagSave(${r.id})">💾</button>
          <span id="ff-status-${r.id}" style="font-size:11px;margin-left:6px"></span>
        </td>
      </tr>`).join('')}</tbody>
  </table>
  <div style="font-size:11px;color:#556;margin-top:10px">Аудитория: <code>all</code> — всем, <code>admins</code> — только администраторам, либо список ID через запятую (например <code>123,456</code>).</div>`;
}

async function featureFlagSave(id) {
  const enabled = document.getElementById(`ff-enabled-${id}`).checked;
  const audience = document.getElementById(`ff-audience-${id}`).value;
  const note = document.getElementById(`ff-note-${id}`).value;
  const statusEl = document.getElementById(`ff-status-${id}`);
  statusEl.style.color = '#889';
  statusEl.textContent = '…';
  try {
    const r = await fetch(`/api/feature_flags/${id}`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({enabled, audience, note})
    }).then(x=>x.json());
    statusEl.style.color = r.ok ? '#7c9' : '#f66';
    statusEl.textContent = r.ok ? '✅' : '⚠️';
    if (r.ok) loadFeatureFlags();
  } catch(e) {
    statusEl.style.color = '#f66';
    statusEl.textContent = '⚠️';
  }
}

// ── Пользователи (BotUser) ──
let userQuery = '';
let _userSearchTimer = null;
function userSearchDebounced() {
  clearTimeout(_userSearchTimer);
  _userSearchTimer = setTimeout(() => {
    userQuery = document.getElementById('user-search').value;
    loadUsers();
  }, 300);
}

async function loadUsers() {
  const el = document.getElementById('users-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  const data = await fetch(`/api/users?q=${encodeURIComponent(userQuery)}&limit=100`).then(r=>r.json());
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">Пользователи не найдены</div>';
    return;
  }
  el.innerHTML = `<div style="font-size:12px;color:#556;margin-bottom:10px">${data.total} пользователей</div>
  <table style="width:100%;border-collapse:collapse;font-size:13px">
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:6px 8px">ID</th>
      <th style="padding:6px 8px">Username</th>
      <th style="padding:6px 8px">Имя</th>
      <th style="padding:6px 8px">Первый визит</th>
      <th style="padding:6px 8px">Последний визит</th>
      <th style="padding:6px 8px;text-align:center">Ограничен</th>
      <th style="padding:6px 8px"></th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr style="border-top:1px solid #0d1628" id="user-row-${r.user_id}">
        <td style="padding:7px 8px;font-family:monospace;color:#9bc">${r.user_id}</td>
        <td style="padding:7px 8px;color:#ccd">${r.username ? '@'+esc(r.username) : '—'}</td>
        <td style="padding:7px 8px;color:#889">${esc(r.full_name)}</td>
        <td style="padding:7px 8px;color:#556;font-size:11px;white-space:nowrap">${esc((r.first_seen||'').slice(0,16))}</td>
        <td style="padding:7px 8px;color:#556;font-size:11px;white-space:nowrap">${esc((r.last_seen||'').slice(0,16))}</td>
        <td style="padding:7px 8px;text-align:center">
          <label class="ff-switch">
            <input type="checkbox" id="user-muted-${r.user_id}" ${r.is_muted?'checked':''} onchange="userMuteToggle(${r.user_id})">
            <span class="ff-switch-track"></span>
          </label>
        </td>
        <td style="padding:7px 8px"><span id="user-status-${r.user_id}" style="font-size:11px"></span></td>
      </tr>`).join('')}</tbody>
  </table>`;
}

async function userMuteToggle(userId) {
  const is_muted = document.getElementById(`user-muted-${userId}`).checked;
  const statusEl = document.getElementById(`user-status-${userId}`);
  statusEl.style.color = '#889';
  statusEl.textContent = '…';
  try {
    const r = await fetch(`/api/users/${userId}/mute`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({is_muted})
    }).then(x=>x.json());
    statusEl.style.color = r.ok ? '#7c9' : '#f66';
    statusEl.textContent = r.ok ? '✅' : '⚠️';
  } catch(e) {
    statusEl.style.color = '#f66';
    statusEl.textContent = '⚠️';
  }
}

// ── Пользователи: под-вкладки Список/Рассылка ──
let userSubtab = 'list';
function userSetSubtab(name) {
  userSubtab = name;
  document.getElementById('user-subtab-list').classList.toggle('btn-primary', name==='list');
  document.getElementById('user-subtab-broadcast').classList.toggle('btn-primary', name==='broadcast');
  document.getElementById('users-list-view').style.display = name==='list' ? '' : 'none';
  document.getElementById('users-broadcast-view').style.display = name==='broadcast' ? '' : 'none';
  if (name==='list') loadUsers();
  else broadcastInit();
}

// ── Рассылка (broadcast) ──
let _bcPollTimer = null;

let bcUsers = [];
let bcMode = 'all';

async function broadcastInit() {
  const data = await fetch('/api/broadcast/audience').then(r=>r.json());
  const testInput = document.getElementById('bc-test-id');
  if (!testInput.value && data.default_test_id) testInput.value = data.default_test_id;
  await broadcastLoadUserChecklist();
  await broadcastLoadHistory();
  await broadcastCheckStatus();
}

async function broadcastLoadUserChecklist() {
  const data = await fetch('/api/users?limit=1000').then(r=>r.json());
  bcUsers = data.rows || [];
  const el = document.getElementById('bc-user-checklist');
  el.innerHTML = bcUsers.length ? bcUsers.map(u => `
      <label style="display:flex;align-items:center;gap:8px;padding:3px 0;font-size:12px">
        <input type="checkbox" class="bc-user-chk" value="${u.user_id}" onchange="updateBcRecipientCount()">
        <span style="font-family:monospace;color:#9bc">${u.user_id}</span>
        <span style="color:#ccd">${u.username ? '@'+esc(u.username) : ''}</span>
        <span style="color:#556">${esc(u.full_name||'')}</span>
      </label>`).join('') : '<div class="empty">Пользователей нет</div>';
  updateBcRecipientCount();
}

function broadcastModeChange() {
  bcMode = document.querySelector('input[name="bc-mode"]:checked').value;
  document.getElementById('bc-user-checklist').style.display = bcMode==='all' ? 'none' : '';
  updateBcRecipientCount();
}

function bcSelectedIds() {
  return Array.from(document.querySelectorAll('.bc-user-chk:checked')).map(c => parseInt(c.value, 10));
}

function bcRecipientIds() {
  const selected = new Set(bcSelectedIds());
  if (bcMode === 'all') return bcUsers.map(u => u.user_id);
  if (bcMode === 'only') return bcUsers.filter(u => selected.has(u.user_id)).map(u => u.user_id);
  return bcUsers.filter(u => !selected.has(u.user_id)).map(u => u.user_id);  // except
}

function updateBcRecipientCount() {
  document.getElementById('bc-audience-count').textContent = bcRecipientIds().length;
}

async function broadcastTest() {
  const text_ru = sanitizeTelegramHtml(document.getElementById('bc-text-edit'));
  const target_id = parseInt(document.getElementById('bc-test-id').value, 10);
  const statusEl = document.getElementById('bc-test-status');
  if (!text_ru.trim()) { statusEl.style.color = '#f66'; statusEl.textContent = 'Пустой текст'; return; }
  if (!target_id) { statusEl.style.color = '#f66'; statusEl.textContent = 'Укажите тестовый ID'; return; }
  statusEl.style.color = '#889';
  statusEl.textContent = 'Отправка…';
  try {
    const r = await fetch('/api/broadcast/test', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text_ru, target_id})
    }).then(x=>x.json());
    statusEl.style.color = r.ok ? '#7c9' : '#f66';
    statusEl.textContent = r.ok ? '✅ Отправлено' : '⚠️ ' + (r.detail || 'ошибка');
  } catch(e) {
    statusEl.style.color = '#f66';
    statusEl.textContent = 'Ошибка: ' + e.message;
  }
}

async function broadcastSend() {
  const text_ru = sanitizeTelegramHtml(document.getElementById('bc-text-edit'));
  if (!text_ru.trim()) { alert('Пустой текст рассылки.'); return; }
  const ids = bcRecipientIds();
  if (!ids.length) { alert('Нет получателей для отправки — проверьте режим и отметки.'); return; }
  if (!confirm(`Отправить это сообщение ${ids.length} получателям? Действие необратимо.`)) return;
  try {
    const r = await fetch('/api/broadcast/send', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text_ru, user_ids: ids})
    }).then(x=>x.json());
    if (!r.ok && r.detail) { alert(r.detail); return; }
    document.getElementById('bc-progress').style.display = '';
    broadcastPoll();
  } catch(e) {
    alert('Ошибка: ' + e.message);
  }
}

async function broadcastCheckStatus() {
  const s = await fetch('/api/broadcast/status').then(r=>r.json());
  if (s.running || s.total > 0) {
    document.getElementById('bc-progress').style.display = '';
    _updateBcProgress(s);
  }
  if (s.running) broadcastPoll();
}

function _updateBcProgress(s) {
  const done = s.sent + s.failed;
  const pct = s.total ? Math.round(done / s.total * 100) : 0;
  document.getElementById('bc-progress-bar').style.width = pct + '%';
  document.getElementById('bc-progress-text').textContent =
    `Отправлено: ${s.sent} · Ошибок: ${s.failed} · Всего: ${s.total}${s.running ? ' — идёт рассылка…' : ' — завершено'}`;
}

function broadcastPoll() {
  clearTimeout(_bcPollTimer);
  _bcPollTimer = setTimeout(async () => {
    const s = await fetch('/api/broadcast/status').then(r=>r.json());
    _updateBcProgress(s);
    if (s.running) {
      broadcastPoll();
    } else {
      broadcastLoadHistory();
    }
  }, 1000);
}

async function broadcastLoadHistory() {
  const el = document.getElementById('bc-history-content');
  const data = await fetch('/api/broadcast/history').then(r=>r.json());
  const rows = data.rows || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty" style="padding:14px 0">Рассылок ещё не было</div>';
    return;
  }
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:12px;table-layout:fixed">
    <colgroup><col style="width:110px"><col><col style="width:60px"><col style="width:70px"><col style="width:60px"></colgroup>
    <thead><tr style="color:#556;font-size:11px;text-align:left">
      <th style="padding:5px 8px">Когда</th>
      <th style="padding:5px 8px">Текст (как в Telegram)</th>
      <th style="padding:5px 8px">Всего</th>
      <th style="padding:5px 8px">Успешно</th>
      <th style="padding:5px 8px">Ошибок</th>
    </tr></thead>
    <tbody>${rows.map(r => `
      <tr style="border-top:1px solid #0d1628">
        <td style="padding:5px 8px;color:#556;white-space:nowrap">${esc((r.sent_at||'').slice(0,16))}</td>
        <td style="padding:5px 8px;color:#ccd;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.text_ru}</td>
        <td style="padding:5px 8px;color:#889">${r.total}</td>
        <td style="padding:5px 8px;color:#7c9">${r.success}</td>
        <td style="padding:5px 8px;color:${r.failed?'#f66':'#556'}">${r.failed}</td>
      </tr>`).join('')}</tbody>
  </table>`;
}

// Своя подсказка (вместо нативной title) — можно управлять размером шрифта
let _fbTipEl = null;
function _fbTip() {
  if (!_fbTipEl) {
    _fbTipEl = document.createElement('div');
    _fbTipEl.className = 'fb-tooltip';
    document.body.appendChild(_fbTipEl);
  }
  return _fbTipEl;
}
function fbTipShow(e, el) {
  const tip = _fbTip();
  tip.textContent = el.getAttribute('data-tip') || '';
  tip.style.display = 'block';
  fbTipMove(e);
}
function fbTipMove(e) {
  const tip = _fbTip();
  const pad = 16;
  let x = e.clientX + pad, y = e.clientY + pad;
  const rect = tip.getBoundingClientRect();
  if (x + rect.width > window.innerWidth) x = Math.max(pad, window.innerWidth - rect.width - pad);
  if (y + rect.height > window.innerHeight) y = Math.max(pad, window.innerHeight - rect.height - pad);
  tip.style.left = x + 'px';
  tip.style.top = y + 'px';
}
function fbTipHide() {
  if (_fbTipEl) _fbTipEl.style.display = 'none';
}

function _artistReleaseRows(releases) {
  if (!releases.length) return '<div class="empty" style="padding:16px;text-align:center">Релизов нет</div>';
  return releases.map(r => `
    <div class="card-row" onclick="openListing(${r.id})" style="cursor:pointer">
      ${r.photo ? `<img class="card-thumb" src="/api/tg_photo/${encodeURIComponent(r.photo)}"
          onerror="this.outerHTML='<div class=\\'card-thumb-empty\\'>🎵</div>'">`
        : '<div class="card-thumb-empty">🎵</div>'}
      <div class="card-info">
        <div class="card-title"><span class="st-dot ${r.status==='published'?'st-dot-on':'st-dot-off'}"></span>${esc(r.title)}</div>
        <div class="card-meta">${esc(sectionName('release'))} · ${esc(r.rtype)} · ${esc(r.created_at)}
          ${r.status!=='published'?'· <span style="color:#888">скрыт</span>':''}</div>
      </div>
      <div class="card-stats"><b style="font-size:16px">${r.opens}</b> откр.</div>
    </div>`).join('');
}

async function openArtistCard(id) {
  const el = document.getElementById('drill-content');
  document.getElementById('drill-title').textContent = '…';
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  document.getElementById('modal-drill').classList.add('open');
  const a = await fetch(`/api/artist/${id}`).then(r=>r.json());
  document.getElementById('drill-title').textContent = `🎤 ${a.name}`;
  const meta = [
    ['Тип', a.type], ['Жанры', a.genres], ['Город', a.city],
    ['Контакты', a.contact], ['Описание', a.descr],
    ['Владелец', a.username ? '@'+a.username : a.owner_id],
    ['Создан', a.created_at],
    ['Статус', a.status==='active' ? '🟢 активен' : '⚪ скрыт'],
    ['Ссылки', (a.links||[]).map(l=>{
      const u = safeHttpUrl(l.url);
      return u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(l.label)}</a>` : '';
    }).filter(Boolean).join(' · ')],
  ].filter(([,v])=>v).map(([k,v]) =>
    `<span class="listing-meta-key">${k}</span><span class="listing-meta-val">${k==='Ссылки'?v:esc(String(v))}</span>`).join('');
  el.innerHTML = `
    <div style="display:flex;gap:14px;margin-bottom:14px">
      ${a.photo_file_id ? `<img src="/api/tg_photo/${encodeURIComponent(a.photo_file_id)}"
        style="width:110px;height:110px;object-fit:cover;border-radius:10px;flex-shrink:0"
        onerror="this.remove()">` : ''}
      <div class="listing-meta" style="flex:1">${meta}</div>
    </div>
    <button class="btn btn-sm" style="background:#1a3a6a;color:#7af;margin-bottom:10px"
      onclick="openArtistEdit(${a.id})">✏️ Редактировать</button>
    <div style="font-size:12px;color:#556;margin:8px 0">Релизы (${a.releases.length}):</div>
    ${_artistReleaseRows(a.releases)}`;
}

const ARTIST_TYPES_JS = ['Сольный исполнитель','Группа','Дуэт','Проект','DJ','Другое'];

async function openArtistEdit(id) {
  const el = document.getElementById('drill-content');
  const a = await fetch(`/api/artist/${id}`).then(r=>r.json());
  document.getElementById('drill-title').textContent = `✏️ ${a.name}`;
  const typeOpts = ARTIST_TYPES_JS.map(t =>
    `<option ${t===a.type?'selected':''}>${t}</option>`).join('');
  el.innerHTML = `
    <div class="listing-meta">
      <span class="listing-meta-key">Название</span>
      <span class="listing-meta-val"><input id="ae-name" class="lm-edit-input" value="${esc(a.name)}"></span>
      <span class="listing-meta-key">Тип</span>
      <span class="listing-meta-val"><select id="ae-type" class="lm-edit-input">${typeOpts}</select></span>
      <span class="listing-meta-key">Жанры</span>
      <span class="listing-meta-val"><input id="ae-genres" class="lm-edit-input" value="${esc(a.genres)}"></span>
      <span class="listing-meta-key">Город</span>
      <span class="listing-meta-val"><input id="ae-city" class="lm-edit-input" value="${esc(a.city)}"></span>
      <span class="listing-meta-key">Контакты</span>
      <span class="listing-meta-val"><input id="ae-contact" class="lm-edit-input" value="${esc(a.contact)}"></span>
      <span class="listing-meta-key">Описание</span>
      <span class="listing-meta-val"><textarea id="ae-descr" class="lm-edit-input" rows="4">${esc(a.descr)}</textarea></span>
      <span class="listing-meta-key">Ссылки</span>
      <span class="listing-meta-val"><textarea id="ae-links" class="lm-edit-input" rows="3"
        placeholder="по одной ссылке на строку">${esc((a.links||[]).map(l=>l.url).join('\n'))}</textarea></span>
      <span class="listing-meta-key">Фото</span>
      <span class="listing-meta-val">
        <button class="btn btn-sm" style="background:#1a3a6a;color:#7af"
          onclick="document.getElementById('ae-photo').click()">🖼 ${a.photo_file_id?'Заменить':'Загрузить'}</button>
        <span id="ae-photo-status" style="color:#9bc;font-size:12px;margin-left:6px"></span>
        <input type="file" id="ae-photo" accept="image/*" style="display:none"
          onchange="artistPhotoUpload(this, ${a.id})">
      </span>
    </div>
    <div style="margin-top:12px">
      <button class="btn btn-sm" style="background:#1a4a2a;color:#7f7" onclick="artistEditSave(${a.id})">💾 Сохранить</button>
      <button class="btn btn-sm" style="background:#333;color:#aaa" onclick="openArtistCard(${a.id})">✕ Отмена</button>
    </div>`;
}

async function artistPhotoUpload(input, id) {
  const file = input.files && input.files[0];
  if (!file) return;
  const st = document.getElementById('ae-photo-status');
  if (st) st.textContent = '⏳…';
  try {
    const r = await fetch(`/api/artist/${id}/photo?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST', headers: {'Content-Type':'application/octet-stream'}, body: file,
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    if (st) st.textContent = '✔️';
  } catch(e) {
    if (st) st.textContent = '';
    alert('Ошибка загрузки: ' + e.message);
  }
  input.value = '';
}

async function artistEditSave(id) {
  const body = {
    name: document.getElementById('ae-name').value,
    type: document.getElementById('ae-type').value,
    genres: document.getElementById('ae-genres').value,
    city: document.getElementById('ae-city').value,
    contact: document.getElementById('ae-contact').value,
    descr: document.getElementById('ae-descr').value,
    links_text: document.getElementById('ae-links').value,
  };
  try {
    const r = await fetch(`/api/artist/${id}`, {
      method: 'PATCH', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body)
    });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    await openArtistCard(id);
    if (currentTab === 'artists') loadArtists();
  } catch(e) { alert('Ошибка сохранения: ' + e.message); }
}

async function openArtistReleases(id) {
  const el = document.getElementById('drill-content');
  document.getElementById('drill-title').textContent = '…';
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  document.getElementById('modal-drill').classList.add('open');
  const a = await fetch(`/api/artist/${id}`).then(r=>r.json());
  document.getElementById('drill-title').textContent = `${a.releases.length} релизов — ${a.name}`;
  el.innerHTML = _artistReleaseRows(a.releases);
}

async function artistToggle(id) {
  try {
    const r = await fetch(`/api/artist/${id}/toggle_status`, {method:'POST'}).then(x=>x.json());
    if (r && r.ok) loadArtists();
    else alert('Не удалось: ' + ((r&&r.detail)||'?'));
  } catch(e) { alert('Ошибка: ' + e.message); }
}

// ── Catalog tree browser ──
const SECTION_ICONS = {market:'🛒', service:'💼', vacancy:'🤝', events:'🎭', release:'🎵'};
const SECTION_NAMES_JS = {market:'Барахолка', service:'Услуги', vacancy:'Вакансии', events:'Афиша', release:'Релизы'};
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
    <div class="cat-tree-row" onclick="catalogOpenSection(${jsArg(s.type)},${jsArg(s.name)},${s.root_id})">
      <span class="cat-tree-icon">${SECTION_ICONS[s.type]||'📦'}</span>
      <span class="cat-tree-name">${esc(s.name)}</span>
      <span class="cat-tree-subcount" style="opacity:.3">—</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillListings(0,${jsArg(s.type)},${jsArg(s.name)})"><b>${s.listings}</b> объявл.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(0,${jsArg(s.type)},${jsArg(s.name)},'open')"><b>${s.views}</b> просм.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillUsers(0,${jsArg(s.type)},${jsArg(s.name)})"><b>${s.viewers}</b> польз.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(0,${jsArg(s.type)},${jsArg(s.name)},'contact')"><b>${s.contacts}</b> контакт.</span>
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
    <div class="cat-tree-row" onclick="catalogOpenCat(${r.id},${jsArg(r.name)},${jsArg(stype||'')})">
      <span class="cat-tree-icon">📁</span>
      <span class="cat-tree-name">${esc(r.name)}</span>
      <span class="cat-tree-subcount">${r.subcats ? `<span>${r.subcats} подкат.</span>` : '<span style="opacity:.2">—</span>'}</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillListings(${r.id},0,${jsArg(r.name)})"><b>${r.listings}</b> объявл.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(${r.id},0,${jsArg(r.name)},'open')"><b>${r.views}</b> просм.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillUsers(${r.id},0,${jsArg(r.name)})"><b>${r.viewers}</b> польз.</span>
      <span class="cat-tree-col cat-drill" onclick="event.stopPropagation();openDrillViews(${r.id},0,${jsArg(r.name)},'contact')"><b>${r.contacts||0}</b> контакт.</span>
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
let _catCurCatId = 0;
let _catCurStype = '';

let _catActivity = '';

// Общий поиск (по названию/описанию) — поле живёт вне #catalog-content,
// чтобы не терять фокус при перерисовке (см. #user-search).
let _catQuery = '';
let _catSearchTimer = null;
function catSearchDebounced() {
  clearTimeout(_catSearchTimer);
  _catSearchTimer = setTimeout(() => {
    _catQuery = document.getElementById('catalog-search').value;
    catalogShowListings(_catCurCatId, _catCurStype, 0);
  }, 300);
}

// Поиск по отдельному столбцу таблицы (название/город/категория).
let _catColFilters = {};
function catFilterInput(key, value) {
  _catColFilters[key] = value;
  clearTimeout(_catSearchTimer);
  _catSearchTimer = setTimeout(() => {
    withFocusPreserved(() => catalogShowListings(_catCurCatId, _catCurStype, 0));
  }, 300);
}

// Превью (карточки) или таблица — выбор живёт в localStorage браузера,
// как и настройки столбцов таблицы «Тексты».
let _catViewMode = localStorage.getItem('admin_cat_view_mode') || 'grid';
function catSetViewMode(mode, catId, stype) {
  _catViewMode = mode;
  localStorage.setItem('admin_cat_view_mode', mode);
  catalogShowListings(catId, stype, _catListingOffset);
}
function catGoPage(offset) { catalogShowListings(_catCurCatId, _catCurStype, offset); }

// Сортировка таблицы (клик по заголовку столбца) — например, чтобы найти
// объявление/релиз с наибольшим числом просмотров.
let _catSortKey = 'created_at';
let _catSortDir = 'desc';
function catSortBy(key, catId, stype) {
  const def = CAT_COLUMNS_DEF.find(c => c.key === key);
  if (!def || def.sortable === false) return;
  if (_catSortKey === key) {
    _catSortDir = _catSortDir === 'desc' ? 'asc' : 'desc';
  } else {
    _catSortKey = key;
    _catSortDir = 'desc';
  }
  catalogShowListings(catId, stype, 0);
}

function catSetActivity(v, catId, stype) {
  _catActivity = v;
  catalogShowListings(catId, stype, 0);
}

// Перерисовка текущей сетки каталога после действий в модалке
// (продление, скрытие, удаление) — иначе бейджи статуса устаревают
window._catalogRefresh = null;
function refreshCatalogIfOpen() {
  try { if (window._catalogRefresh) window._catalogRefresh(); } catch(e) {}
}

async function catalogShowListings(catId, stype, offset=0) {
  _catListingOffset = offset;
  _catCurCatId = catId;
  _catCurStype = stype;
  window._catalogRefresh = () => catalogShowListings(catId, stype, _catListingOffset);
  const el = document.getElementById('catalog-content');
  el.innerHTML = '<div class="empty" style="padding:30px;text-align:center">…</div>';
  catalogSetBreadcrumb();
  const params = new URLSearchParams({
    category_id: catId, offset, limit: CAT_LISTING_LIMIT,
    sort: _catSortKey, order: _catSortDir,
  });
  if (_catActivity) params.set('activity', _catActivity);
  if (_catQuery) params.set('q', _catQuery);
  const activeFilters = Object.fromEntries(Object.entries(_catColFilters).filter(([,v]) => v));
  if (Object.keys(activeFilters).length) params.set('filters', JSON.stringify(activeFilters));
  const data = await fetch(`/api/listings?${params}`).then(r=>r.json());
  const viewToggle = `<div style="display:flex;gap:4px">
    <button class="btn btn-sm" style="${_catViewMode==='grid'?'background:#2a3a70':''}" onclick="catSetViewMode('grid',${catId},'${stype}')">🖼 Превью</button>
    <button class="btn btn-sm" style="${_catViewMode==='table'?'background:#2a3a70':''}" onclick="catSetViewMode('table',${catId},'${stype}')">📋 Таблица</button>
  </div>`;
  const activityBar = `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap">
    <div style="display:flex;align-items:center;gap:10px">
      <span style="font-size:12px;color:#556">${data.total} объявлений</span>
      <select onchange="catSetActivity(this.value,${catId},'${stype}')"
        style="background:#0d1f45;color:#ccd;border:1px solid #1a2a55;border-radius:6px;padding:3px 8px;font-size:12px">
        <option value="" ${_catActivity===''?'selected':''}>Все</option>
        <option value="active" ${_catActivity==='active'?'selected':''}>🟢 Активные</option>
        <option value="inactive" ${_catActivity==='inactive'?'selected':''}>⚪ Неактивные</option>
      </select>
    </div>
    ${viewToggle}
  </div>`;
  if (!data.rows.length) {
    const emptyMsg = '<div class="empty" style="padding:20px;text-align:center">Нет объявлений</div>';
    el.innerHTML = activityBar + (_catViewMode === 'table' ? catTableHtml(data.rows, catId, stype) + emptyMsg : emptyMsg);
    return;
  }
  const pag = paginationHtml(data.total, CAT_LISTING_LIMIT, offset, 'catGoPage');
  const body = _catViewMode === 'table'
    ? catTableHtml(data.rows, catId, stype)
    : `<div class="catalog-listings-grid">${data.rows.map(r=>catCardHtml(r)).join('')}</div>`;
  el.innerHTML = activityBar + body + pag;
}

// ── Таблица объявлений: настраиваемые столбцы (тот же паттерн, что и у
//    таблицы «Тексты») + сортировка по клику на заголовок ──
const CAT_COLUMNS_DEF = [
  {key:'photo',      label:'',          minWidth:50,  defaultWidth:50,  locked:true, sortable:false},
  {key:'title',      label:'Название',  minWidth:120, defaultWidth:220, sortable:true, filterable:true},
  {key:'price',      label:'Цена',      minWidth:70,  defaultWidth:100, sortable:true},
  {key:'city',       label:'Город',     minWidth:70,  defaultWidth:110, sortable:true, filterable:true},
  {key:'category',   label:'Категория', minWidth:90,  defaultWidth:150, sortable:true, filterable:true},
  {key:'type',       label:'Раздел',    minWidth:70,  defaultWidth:100, sortable:true},
  {key:'status',     label:'Статус',    minWidth:70,  defaultWidth:90,  sortable:true},
  {key:'opens',      label:'Просмотры',minWidth:70,  defaultWidth:100, sortable:true},
  {key:'created_at', label:'Дата',      minWidth:90,  defaultWidth:135, sortable:true},
];
const CAT_COLS_STORAGE_KEY = 'admin_cat_columns_v1';

function catLoadColumnPrefs() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(CAT_COLS_STORAGE_KEY) || '{}'); } catch(e) {}
  const allKeys = CAT_COLUMNS_DEF.map(c => c.key);
  let order = Array.isArray(saved.order) ? saved.order.filter(k => allKeys.includes(k)) : [];
  allKeys.forEach(k => { if (!order.includes(k)) order.push(k); });
  const widths = saved.widths || {};
  const visible = saved.visible || {};
  CAT_COLUMNS_DEF.forEach(c => {
    if (typeof widths[c.key] !== 'number') widths[c.key] = c.defaultWidth;
    if (typeof visible[c.key] !== 'boolean') visible[c.key] = true;
    if (c.locked) visible[c.key] = true;
  });
  return {order, widths, visible};
}
let catCols = catLoadColumnPrefs();
function catSaveColumnPrefs() {
  localStorage.setItem(CAT_COLS_STORAGE_KEY, JSON.stringify(catCols));
}
function catVisibleColumns() {
  return catCols.order
    .filter(k => catCols.visible[k] !== false)
    .map(k => CAT_COLUMNS_DEF.find(c => c.key===k));
}
function catToggleColumn(key, checked, catId, stype) {
  catCols.visible[key] = checked;
  catSaveColumnPrefs();
  catalogShowListings(catId, stype, _catListingOffset);
}
function catToggleColsPanel() {
  const p = document.getElementById('cat-cols-panel');
  if (p) p.style.display = p.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', (e) => {
  const panel = document.getElementById('cat-cols-panel');
  const btn = document.getElementById('cat-cols-btn');
  if (panel && panel.style.display !== 'none' && !panel.contains(e.target) && e.target !== btn) {
    panel.style.display = 'none';
  }
});

let _catDragKey = null;
function catColDragStart(e) {
  _catDragKey = e.currentTarget.dataset.col;
  e.dataTransfer.effectAllowed = 'move';
}
function catColDragOver(e) { e.preventDefault(); }
function catColDrop(e, catId, stype) {
  e.preventDefault();
  const targetKey = e.currentTarget.dataset.col;
  if (!_catDragKey || _catDragKey === targetKey) return;
  const order = catCols.order;
  const from = order.indexOf(_catDragKey);
  const to = order.indexOf(targetKey);
  if (from === -1 || to === -1) return;
  order.splice(from, 1);
  order.splice(to, 0, _catDragKey);
  catSaveColumnPrefs();
  catalogShowListings(catId, stype, _catListingOffset);
}

let _catResizeState = null;
function catColResizeStart(e, key) {
  e.preventDefault();
  e.stopPropagation();
  _catResizeState = { key, startX: e.clientX, startWidth: catCols.widths[key] };
  document.addEventListener('mousemove', catColResizeMove);
  document.addEventListener('mouseup', catColResizeEnd);
}
function catColResizeMove(e) {
  if (!_catResizeState) return;
  const def = CAT_COLUMNS_DEF.find(c => c.key === _catResizeState.key);
  const delta = e.clientX - _catResizeState.startX;
  const newWidth = Math.max(def.minWidth, _catResizeState.startWidth + delta);
  catCols.widths[_catResizeState.key] = newWidth;
  const idx = catVisibleColumns().findIndex(c => c.key === _catResizeState.key);
  const table = document.querySelector('#catalog-content table.cat-table');
  if (table && idx !== -1) {
    const colEl = table.querySelectorAll('colgroup col')[idx];
    if (colEl) colEl.style.width = newWidth + 'px';
  }
}
function catColResizeEnd() {
  document.removeEventListener('mousemove', catColResizeMove);
  document.removeEventListener('mouseup', catColResizeEnd);
  _catResizeState = null;
  catSaveColumnPrefs();
}

function catCellHtml(row, key) {
  if (key === 'photo') {
    const fp = (row.photo_ids||[])[0];
    if (fp) return `<img src="/api/tg_photo/${encodeURIComponent(fp)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px" onerror="this.style.display='none'">`;
    if (row.video_type === 'youtube' && row.video_id) return `<img src="https://img.youtube.com/vi/${row.video_id}/mqdefault.jpg" style="width:36px;height:36px;object-fit:cover;border-radius:4px">`;
    return `<span style="opacity:.4">${row.video_type?'▶️':'📋'}</span>`;
  }
  if (key === 'title') return esc(row.title || 'Без названия');
  if (key === 'price') return row.price ? esc(row.price) : '—';
  if (key === 'city') return row.city ? esc(row.city) : '—';
  if (key === 'category') return row.category ? esc(row.category) : '—';
  if (key === 'type') return esc(sectionName(row.type));
  if (key === 'status') {
    const isActive = !row.status || row.status === 'active';
    return isActive ? '<span class="cat-table-status st-active">активно</span>'
      : `<span class="cat-table-status st-inactive">${row.status==='archived'?'в архиве':esc(row.status)}</span>`;
  }
  if (key === 'opens') return `<a style="color:#9bc;cursor:pointer;font-weight:700" onclick="event.stopPropagation();openOpenersModal('listing',${row.id},${jsArg('Кто открывал: '+(row.title||'Без названия'))})">${row.opens || 0}</a>`;
  if (key === 'created_at') return fmtDateTime(row.created_at);
  return '';
}

function catTableHtml(rows, catId, stype) {
  const cols = catVisibleColumns();
  const colgroup = cols.map(c => `<col style="width:${catCols.widths[c.key]}px">`).join('');
  const thead = cols.map(c => {
    const sortable = c.sortable !== false;
    const arrow = _catSortKey === c.key ? (_catSortDir === 'desc' ? ' ▼' : ' ▲') : '';
    return `<th class="txt-th" data-col="${c.key}" draggable="${!c.locked}"
      ondragstart="catColDragStart(event)" ondragover="catColDragOver(event)" ondrop="catColDrop(event,${catId},'${stype}')"
      style="padding:6px 8px;position:relative;${c.locked?'':'cursor:grab'}${sortable?';cursor:pointer':''}"
      ${sortable ? `onclick="catSortBy('${c.key}',${catId},'${stype}')"` : ''}>${esc(c.label)}${arrow}<span class="col-resize" onmousedown="catColResizeStart(event,'${c.key}')"></span></th>`;
  }).join('');
  const filterRow = cols.map(c => `<th style="padding:2px 6px">${c.filterable
    ? `<input type="text" id="cat-filter-${c.key}" value="${esc(_catColFilters[c.key]||'')}" placeholder="…"
        oninput="catFilterInput('${c.key}', this.value)"
        style="width:100%;background:#0d1424;color:#dde;border:1px solid #223;border-radius:4px;padding:3px 6px;font-size:11px">`
    : ''}</th>`).join('');
  const tbody = rows.map(r => `
    <tr style="border-top:1px solid #0d1628;cursor:pointer" onclick="openListing(${r.id})">
      ${cols.map(c => `<td class="txt-td" style="padding:6px 8px">${catCellHtml(r, c.key)}</td>`).join('')}
    </tr>`).join('');
  const colsPanel = CAT_COLUMNS_DEF.filter(c => !c.locked).map(c => `
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;padding:4px 0">
        <input type="checkbox" ${catCols.visible[c.key]!==false?'checked':''}
          onchange="catToggleColumn('${c.key}', this.checked, ${catId}, '${stype}')"> ${esc(c.label)}
      </label>`).join('');
  return `
  <div style="display:flex;justify-content:flex-end;margin-bottom:8px;position:relative">
    <button class="btn btn-ghost btn-sm" id="cat-cols-btn" onclick="catToggleColsPanel()">⚙ Столбцы</button>
    <div id="cat-cols-panel" style="display:none;position:absolute;right:0;top:28px;background:#1e1e35;
      border:1px solid #333;border-radius:8px;padding:10px 14px;z-index:20;min-width:150px">${colsPanel}</div>
  </div>
  <table class="cat-table" style="width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed">
    <colgroup>${colgroup}</colgroup>
    <thead>
      <tr style="color:#556;font-size:11px;text-align:left">${thead}</tr>
      <tr>${filterRow}</tr>
    </thead>
    <tbody>${tbody}</tbody>
  </table>`;
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
  const isActive = !r.status || r.status === 'active';
  const statusBadge = isActive
    ? '<span class="cat-card-status st-active">активно</span>'
    : `<span class="cat-card-status st-inactive">${r.status==='archived'?'в архиве':esc(r.status)}</span>`;
  return `<div class="cat-card${isActive?'':' cat-card-off'}" onclick="openListing(${r.id})">
    <div class="cat-card-media">
      ${mediaHtml}
      ${hasVideo ? `<span class="cat-card-video-badge">${r.video_type==='youtube'?'▶ YouTube':'▶ Видео'}</span>` : ''}
      ${r.is_sold ? '<span class="cat-card-sold">продано</span>' : ''}
      ${statusBadge}
      ${photoCount > 1 ? `<span class="cat-card-photos-count">📷 ${photoCount}</span>` : ''}
    </div>
    <div class="cat-card-body">
      <div class="cat-card-title">${esc(r.title||'Без названия')}</div>
      ${r.price ? `<div class="cat-card-price">${esc(r.price)}</div>` : ''}
      <div class="cat-card-meta">${r.city?esc(r.city):''}${r.category?' · '+esc(r.category):''}</div>
      <div class="cat-card-stats">👁 ${r.opens} · ${fmtDateTime(r.created_at)}</div>
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
  release:'Релизы',releases:'Релизы',artists:'Исполнители',
  vacancy:'Вакансии',vacancies:'Вакансии',events:'Афиша',afisha:'Афиша','':(v)=>v||'—'};
function sectionName(s){ return SECTION_LABELS[s] || s || '—'; }
const SOURCE_LABELS = {search:'поиск',catalog:'каталог',my:'мои объявления',track:'трек',
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
  const tcToolbar = document.getElementById('an-topcards-toolbar');
  if (tcToolbar) tcToolbar.style.display = section === 'top_cards' ? 'flex' : 'none';
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
      <div class="card-title">${r.status!=null?`<span class="st-dot ${(!r.status||r.status==='active')?'st-dot-on':'st-dot-off'}" title="${(!r.status||r.status==='active')?'активно':'не активно'}"></span>`:''}${idx!=null?idx+'. ':''}${esc(r.title)}</div>
      <div class="card-meta">${esc(sectionName(r.section||r.type||''))}${r.price?' · '+esc(r.price):''}${r.contact?' · '+esc(r.contact):''} ${r.is_sold?'· <span style="color:#f88">продано</span>':''}</div>
      <div class="card-meta" style="margin-top:2px">🔍 ${r.search_opens||0} · 📂 ${r.catalog_opens||0}</div>
    </div>
    <div class="card-stats" style="text-align:right;white-space:nowrap">
      <b style="font-size:16px">${opens}</b> откр. · ${viewers} чел.
    </div>
  </div>`;
}

// ── Топ карточек: превью/таблица, настраиваемые столбцы, сортировка, поиск
//    (тот же паттерн, что у Релизов/Исполнителей/Объявлений — см. rel*/art*/cat*) ──
const TC_LIMIT = 24;
let _tcOffset = 0;
let _tcViewMode = localStorage.getItem('admin_tc_view_mode') || 'table';
let _tcSortKey = 'opens';
let _tcSortDir = 'desc';
let _tcQuery = '';
let _tcSearchTimer = null;
let _tcColFilters = {};

function tcSearchDebounced() {
  clearTimeout(_tcSearchTimer);
  _tcSearchTimer = setTimeout(() => {
    _tcQuery = document.getElementById('topcards-search').value;
    _tcOffset = 0;
    anTopCards(document.getElementById('an-content'));
  }, 300);
}
function tcSetViewMode(mode) {
  _tcViewMode = mode;
  localStorage.setItem('admin_tc_view_mode', mode);
  anTopCards(document.getElementById('an-content'));
}
function tcGoPage(offset) { _tcOffset = offset; anTopCards(document.getElementById('an-content')); }

const TC_COLUMNS_DEF = [
  {key:'photo',         label:'',           minWidth:50,  defaultWidth:50,  locked:true, sortable:false},
  {key:'title',         label:'Название',   minWidth:130, defaultWidth:220, sortable:true, filterable:true},
  {key:'section',       label:'Раздел',     minWidth:80,  defaultWidth:110, sortable:true, filterable:true},
  {key:'price',         label:'Цена',       minWidth:70,  defaultWidth:100, sortable:true},
  {key:'contact',       label:'Контакт',    minWidth:90,  defaultWidth:140, sortable:true, filterable:true},
  {key:'opens',         label:'Открытий',   minWidth:70,  defaultWidth:100, sortable:true},
  {key:'users',         label:'Чел.',       minWidth:60,  defaultWidth:80,  sortable:true},
  {key:'search_opens',  label:'🔍 Поиск',   minWidth:70,  defaultWidth:90,  sortable:true},
  {key:'catalog_opens', label:'📂 Каталог', minWidth:70,  defaultWidth:90,  sortable:true},
  {key:'status',        label:'Статус',     minWidth:70,  defaultWidth:100, sortable:true, filterable:true},
];
const TC_COLS_STORAGE_KEY = 'admin_tc_columns_v1';

function tcLoadColumnPrefs() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(TC_COLS_STORAGE_KEY) || '{}'); } catch(e) {}
  const allKeys = TC_COLUMNS_DEF.map(c => c.key);
  let order = Array.isArray(saved.order) ? saved.order.filter(k => allKeys.includes(k)) : [];
  allKeys.forEach(k => { if (!order.includes(k)) order.push(k); });
  const widths = saved.widths || {};
  const visible = saved.visible || {};
  TC_COLUMNS_DEF.forEach(c => {
    if (typeof widths[c.key] !== 'number') widths[c.key] = c.defaultWidth;
    if (typeof visible[c.key] !== 'boolean') visible[c.key] = true;
    if (c.locked) visible[c.key] = true;
  });
  return {order, widths, visible};
}
let tcCols = tcLoadColumnPrefs();
function tcSaveColumnPrefs() { localStorage.setItem(TC_COLS_STORAGE_KEY, JSON.stringify(tcCols)); }
function tcVisibleColumns() {
  return tcCols.order.filter(k => tcCols.visible[k] !== false).map(k => TC_COLUMNS_DEF.find(c => c.key===k));
}
function tcToggleColumn(key, checked) {
  tcCols.visible[key] = checked;
  tcSaveColumnPrefs();
  anTopCards(document.getElementById('an-content'));
}
function tcToggleColsPanel() {
  const p = document.getElementById('tc-cols-panel');
  if (p) p.style.display = p.style.display === 'none' ? 'block' : 'none';
}
document.addEventListener('click', (e) => {
  const panel = document.getElementById('tc-cols-panel');
  const btn = document.getElementById('tc-cols-btn');
  if (panel && panel.style.display !== 'none' && !panel.contains(e.target) && e.target !== btn) {
    panel.style.display = 'none';
  }
});

let _tcDragKey = null;
function tcColDragStart(e) { _tcDragKey = e.currentTarget.dataset.col; e.dataTransfer.effectAllowed = 'move'; }
function tcColDragOver(e) { e.preventDefault(); }
function tcColDrop(e) {
  e.preventDefault();
  const targetKey = e.currentTarget.dataset.col;
  if (!_tcDragKey || _tcDragKey === targetKey) return;
  const order = tcCols.order;
  const from = order.indexOf(_tcDragKey);
  const to = order.indexOf(targetKey);
  if (from === -1 || to === -1) return;
  order.splice(from, 1);
  order.splice(to, 0, _tcDragKey);
  tcSaveColumnPrefs();
  anTopCards(document.getElementById('an-content'));
}

let _tcResizeState = null;
function tcColResizeStart(e, key) {
  e.preventDefault(); e.stopPropagation();
  _tcResizeState = { key, startX: e.clientX, startWidth: tcCols.widths[key] };
  document.addEventListener('mousemove', tcColResizeMove);
  document.addEventListener('mouseup', tcColResizeEnd);
}
function tcColResizeMove(e) {
  if (!_tcResizeState) return;
  const def = TC_COLUMNS_DEF.find(c => c.key === _tcResizeState.key);
  const delta = e.clientX - _tcResizeState.startX;
  const newWidth = Math.max(def.minWidth, _tcResizeState.startWidth + delta);
  tcCols.widths[_tcResizeState.key] = newWidth;
  const idx = tcVisibleColumns().findIndex(c => c.key === _tcResizeState.key);
  const table = document.querySelector('#an-content table.tc-table');
  if (table && idx !== -1) {
    const colEl = table.querySelectorAll('colgroup col')[idx];
    if (colEl) colEl.style.width = newWidth + 'px';
  }
}
function tcColResizeEnd() {
  document.removeEventListener('mousemove', tcColResizeMove);
  document.removeEventListener('mouseup', tcColResizeEnd);
  _tcResizeState = null;
  tcSaveColumnPrefs();
}

function tcSortBy(key) {
  const def = TC_COLUMNS_DEF.find(c => c.key === key);
  if (!def || def.sortable === false) return;
  if (_tcSortKey === key) { _tcSortDir = _tcSortDir === 'desc' ? 'asc' : 'desc'; }
  else { _tcSortKey = key; _tcSortDir = 'desc'; }
  _tcOffset = 0;
  anTopCards(document.getElementById('an-content'));
}
function tcFilterInput(key, value) {
  _tcColFilters[key] = value;
  clearTimeout(_tcSearchTimer);
  _tcSearchTimer = setTimeout(() => {
    _tcOffset = 0;
    withFocusPreserved(() => anTopCards(document.getElementById('an-content')));
  }, 300);
}

function tcCellHtml(r, key) {
  if (key === 'photo') {
    const fp = (r.photo_file_id||'').split(',')[0].trim();
    return fp
      ? `<img src="/api/tg_photo/${encodeURIComponent(fp)}" style="width:36px;height:36px;object-fit:cover;border-radius:4px" onerror="this.style.display='none'">`
      : '<span style="opacity:.4">📋</span>';
  }
  if (key === 'title') {
    const isActive = !r.status || r.status === 'active';
    return `<span class="st-dot ${isActive?'st-dot-on':'st-dot-off'}"></span> <a style="color:#7eb8f7;cursor:pointer" onclick="openListing(${r.id})">${esc(r.title)}</a>${r.is_sold?' <span style="color:#f88;font-size:11px">продано</span>':''}`;
  }
  if (key === 'section') return esc(sectionName(r.section||''));
  if (key === 'price') return r.price ? esc(r.price) : '—';
  if (key === 'contact') return r.contact ? esc(r.contact) : '—';
  if (key === 'opens') return `<a style="color:#9bc;cursor:pointer;font-weight:700" onclick="openOpenersModal('listing',${r.id},${jsArg('Кто открывал: '+r.title)})">${r.opens||0}</a>`;
  if (key === 'users') return String(r.users||0);
  if (key === 'search_opens') return String(r.search_opens||0);
  if (key === 'catalog_opens') return String(r.catalog_opens||0);
  if (key === 'status') {
    const isActive = !r.status || r.status === 'active';
    return isActive ? '<span class="cat-table-status st-active">активно</span>'
      : `<span class="cat-table-status st-inactive">${r.status==='archived'?'в архиве':esc(r.status)}</span>`;
  }
  return '';
}

function tcTableHtml(rows) {
  const cols = tcVisibleColumns();
  const colgroup = cols.map(c => `<col style="width:${tcCols.widths[c.key]}px">`).join('');
  const thead = cols.map(c => {
    const sortable = c.sortable !== false;
    const arrow = _tcSortKey === c.key ? (_tcSortDir === 'desc' ? ' ▼' : ' ▲') : '';
    return `<th class="txt-th" data-col="${c.key}" draggable="${!c.locked}"
      ondragstart="tcColDragStart(event)" ondragover="tcColDragOver(event)" ondrop="tcColDrop(event)"
      style="padding:6px 8px;position:relative;${c.locked?'':'cursor:grab'}${sortable?';cursor:pointer':''}"
      ${sortable ? `onclick="tcSortBy('${c.key}')"` : ''}>${esc(c.label)}${arrow}<span class="col-resize" onmousedown="tcColResizeStart(event,'${c.key}')"></span></th>`;
  }).join('');
  const filterRow = cols.map(c => `<th style="padding:2px 6px">${c.filterable
    ? `<input type="text" id="tc-filter-${c.key}" value="${esc(_tcColFilters[c.key]||'')}" placeholder="…"
        oninput="tcFilterInput('${c.key}', this.value)"
        style="width:100%;background:#0d1424;color:#dde;border:1px solid #223;border-radius:4px;padding:3px 6px;font-size:11px">`
    : ''}</th>`).join('');
  const tbody = rows.map(r => `
    <tr style="border-top:1px solid #0d1628">
      ${cols.map(c => `<td class="txt-td" style="padding:6px 8px">${tcCellHtml(r, c.key)}</td>`).join('')}
    </tr>`).join('');
  const colsPanel = TC_COLUMNS_DEF.filter(c => !c.locked).map(c => `
      <label style="display:flex;align-items:center;gap:6px;font-size:12px;padding:4px 0">
        <input type="checkbox" ${tcCols.visible[c.key]!==false?'checked':''}
          onchange="tcToggleColumn('${c.key}', this.checked)"> ${esc(c.label)}
      </label>`).join('');
  return `
  <div style="display:flex;justify-content:flex-end;margin-bottom:8px;position:relative">
    <button class="btn btn-ghost btn-sm" id="tc-cols-btn" onclick="tcToggleColsPanel()">⚙ Столбцы</button>
    <div id="tc-cols-panel" style="display:none;position:absolute;right:0;top:28px;background:#1e1e35;
      border:1px solid #333;border-radius:8px;padding:10px 14px;z-index:20;min-width:150px">${colsPanel}</div>
  </div>
  <table class="tc-table" style="width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed">
    <colgroup>${colgroup}</colgroup>
    <thead>
      <tr style="color:#556;font-size:11px;text-align:left">${thead}</tr>
      <tr>${filterRow}</tr>
    </thead>
    <tbody>${tbody}</tbody>
  </table>`;
}

async function anTopCards(el) {
  const params = new URLSearchParams({offset: _tcOffset, limit: TC_LIMIT, sort: _tcSortKey, order: _tcSortDir});
  if (_tcQuery) params.set('q', _tcQuery);
  const activeFilters = Object.fromEntries(Object.entries(_tcColFilters).filter(([,v]) => v));
  if (Object.keys(activeFilters).length) params.set('filters', JSON.stringify(activeFilters));
  const data = await fetch(`/api/analytics/top_cards?${params}`).then(r=>r.json());
  const rows = data.rows || [];
  const viewToggle = `<div style="display:flex;gap:4px">
    <button class="btn btn-sm" style="${_tcViewMode==='grid'?'background:#2a3a70':''}" onclick="tcSetViewMode('grid')">🖼 Превью</button>
    <button class="btn btn-sm" style="${_tcViewMode==='table'?'background:#2a3a70':''}" onclick="tcSetViewMode('table')">📋 Таблица</button>
  </div>`;
  const topBar = `<div style="display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:10px;flex-wrap:wrap">
    <span style="font-size:12px;color:#556">${data.total||0} карточек</span>
    ${viewToggle}
  </div>`;
  if (!rows.length) {
    const emptyMsg = '<div class="empty" style="padding:20px;text-align:center">Открытий не найдено</div>';
    el.innerHTML = topBar + (_tcViewMode === 'table' ? tcTableHtml(rows) + emptyMsg : emptyMsg);
    return;
  }
  const pag = paginationHtml(data.total||0, TC_LIMIT, _tcOffset, 'tcGoPage');
  const body = _tcViewMode === 'table'
    ? tcTableHtml(rows)
    : `<div class="card-grid">${rows.map((r,i)=>cardRowHtml(r, _tcOffset+i+1)).join('')}</div>`;
  el.innerHTML = topBar + body + pag;
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

function anOwnersGoPage(offset) { anOwners(document.getElementById('an-content'), offset); }

async function anOwners(el, offset=0) {
  _ownersOffset = offset;
  const d = await fetch(`/api/analytics/owners?offset=${offset}&limit=${OWNERS_LIMIT}`).then(r=>r.json());
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
    ${paginationHtml(d.total, OWNERS_LIMIT, offset, 'anOwnersGoPage')}`;
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

  const tgUsername = d.contact && /^@[A-Za-z0-9_]{1,32}$/.test(d.contact)
    ? d.contact.slice(1) : '';
  const contact = d.contact
    ? (tgUsername
        ? `<a href="https://t.me/${encodeURIComponent(tgUsername)}" target="_blank" rel="noopener noreferrer">${esc(d.contact)}</a>`
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
      ${d.release ? `
      <span class="listing-meta-key">Раздел</span><span class="listing-meta-val">${esc(sectionName(d.type))}</span>
      <span class="listing-meta-key">Исполнитель</span><span class="listing-meta-val">${d.release.artist?`<a style="color:#7eb8f7;cursor:pointer" onclick="openArtistCard(${d.release.artist_id||0})">🎤 ${esc(d.release.artist)}</a>`:'—'}</span>
      <span class="listing-meta-key">Тип релиза</span><span class="listing-meta-val">${esc(RTYPE_LABELS[d.release.rtype]||d.release.rtype||'—')}${d.release.date?' · '+esc(d.release.date):''}</span>
      <span class="listing-meta-key">Контакт</span><span class="listing-meta-val" id="lm-contact">${contact}</span>
      <span class="listing-meta-key">Опубликовано</span><span class="listing-meta-val">${fmtDateTime(d.created_at)}</span>
      <span class="listing-meta-key">Статус релиза</span><span class="listing-meta-val">${d.release.status==='published'?'🟢 опубликован':'⚪ '+(d.release.status==='hidden'?'скрыт':esc(d.release.status||'—'))}</span>
      <span class="listing-meta-key">Треков / ссылок</span><span class="listing-meta-val">${d.release.tracks} / ${d.release.links}</span>
      ${d.release.recorded ? `<span class="listing-meta-key">Записано</span><span class="listing-meta-val">${esc(d.release.recorded)}</span>` : ''}
      ${(d.release.links_list||[]).length ? `<span class="listing-meta-key">Ссылки</span><span class="listing-meta-val">${(d.release.links_list||[]).map(l=>{
        const u = safeHttpUrl(l.url);
        return u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(l.label||'Ссылка')}</a>` : '';
      }).filter(Boolean).join(' · ')}</span>` : ''}`
      : `${d.category ? `<span class="listing-meta-key">Категория</span><span class="listing-meta-val">${esc(d.category)}</span>` : ''}
      ${d.city ? `<span class="listing-meta-key">Город</span><span class="listing-meta-val">${esc(d.city)}</span>` : ''}
      <span class="listing-meta-key">Контакт</span><span class="listing-meta-val" id="lm-contact">${contact}</span>
      <span class="listing-meta-key">Раздел</span><span class="listing-meta-val">${esc(sectionName(d.type))}</span>
      <span class="listing-meta-key">Опубликовано</span><span class="listing-meta-val">${fmtDateTime(d.created_at)}</span>
      <span class="listing-meta-key">Статус</span><span class="listing-meta-val">${(!d.status||d.status==='active')
        ? '🟢 активно'
        : '⚪ '+(d.status==='archived'?'в архиве':esc(d.status))+(d.archive_reason?' · '+esc(d.archive_reason):'')}</span>
      ${d.expires_at ? `<span class="listing-meta-key">Действует до</span><span class="listing-meta-val">${fmtDateTime(d.expires_at)}</span>` : ''}`}
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
      ${d.release
        ? `<button class="btn btn-sm" id="lm-toggle-btn" style="background:${d.release.status==='published'?'#4a2a1a':'#1a4a2a'};color:${d.release.status==='published'?'#fa8':'#7f7'}" onclick="lmToggleRelease()">${d.release.status==='published' ? '🚫 Скрыть релиз' : '✅ Показать релиз'}</button>`
        : `<button class="btn btn-sm" id="lm-toggle-btn" style="background:#1a4a2a;color:#7f7" onclick="lmToggleSold()">${d.is_sold ? '🔓 Открыть' : '🔒 Скрыть'}</button>`}
      ${['market','service','vacancy'].includes(d.type) ? `<button class="btn btn-sm" style="background:#3a2c0a;color:#fc6" onclick="lmExtend()">⏳ Продлить 30 дн</button>` : ''}
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

async function lmExtend() {
  const d = window._currentListing;
  if (!d) return;
  try {
    const r = await fetch(`/api/listing/${d.id}/extend`, {method:'POST'}).then(x=>x.json());
    if (r && r.ok) {
      openListing(d.id);  // перерисовать модалку со свежим статусом и сроком
      refreshCatalogIfOpen();
    } else {
      alert('Не удалось продлить: ' + ((r && r.detail) || 'неизвестная ошибка'));
    }
  } catch(e) {
    alert('Не удалось продлить: ' + e.message);
  }
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
  if (d.release) {
    priceEl.style.display = 'none';  // у релизов нет цены — это не объявление
  } else {
    priceEl.style.display = '';
    priceEl.innerHTML =
      `<div class="lm-field-group"><label class="lm-field-label">Цена</label>
       <input id="lm-inp-price" class="lm-edit-input" value="${esc(d.price||'')}"></div>`;
  }
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
    const photoLimit = ['market','service'].includes(d.type)
      ? 3 : (['events','release'].includes(d.type) ? 1 : 0);
    bar.innerHTML = `
      ${photoLimit && nPhotos < photoLimit ? `<button class="btn btn-sm" style="background:#1a3a6a;color:#7af" onclick="document.getElementById('lm-file-photo').click()">➕ Фото (${nPhotos}/${photoLimit})</button>` : ''}
      <button class="btn btn-sm" style="background:#1a3a6a;color:#7af" onclick="document.getElementById('lm-file-video').click()">🎥 ${d.video_type ? 'Заменить видео' : 'Добавить видео'}</button>
      <span id="lm-upload-status" style="color:#9bc;font-size:12px;align-self:center"></span>
      <input type="file" id="lm-file-photo" accept="image/*" style="display:none" onchange="lmUploadMedia(this, 'photo')">
      <input type="file" id="lm-file-video" accept="video/*" style="display:none" onchange="lmUploadMedia(this, 'video')">`;
    photosWrap.after(bar);
  }
  if (d.release) lmRenderReleaseEdit(d);
}

// ── Редактирование полей релиза (тип, дата, ссылки, исполнитель, треки) ──
async function lmRenderReleaseEdit(d) {
  const host = document.getElementById('lm-flex');
  if (!host) return;
  let artistOptions = `<option value="">—</option>`;
  try {
    const arts = await fetch('/api/artists').then(r=>r.json());
    artistOptions = (arts.rows||[]).map(a =>
      `<option value="${a.id}" ${a.id===d.release.artist_id?'selected':''}>🎤 ${esc(a.name)}</option>`).join('');
  } catch(e) {}
  const typeOptions = Object.entries(RTYPE_LABELS).map(([code,label]) =>
    `<option value="${code}" ${code===d.release.rtype?'selected':''}>${label}</option>`).join('');
  host.innerHTML = `
  <div style="border:1px solid #1a2a55;border-radius:8px;padding:10px;margin:10px 0">
    <div style="font-size:11px;color:#667;margin-bottom:8px">ДАННЫЕ РЕЛИЗА</div>
    <div class="listing-meta">
      <span class="listing-meta-key">Исполнитель</span>
      <span class="listing-meta-val"><select id="lm-rel-artist" class="lm-edit-input">${artistOptions}</select></span>
      <span class="listing-meta-key">Тип</span>
      <span class="listing-meta-val"><select id="lm-rel-rtype" class="lm-edit-input">${typeOptions}</select></span>
      <span class="listing-meta-key">Дата</span>
      <span class="listing-meta-val"><input id="lm-rel-date" class="lm-edit-input" value="${esc(d.release.date||'')}"></span>
      <span class="listing-meta-key">Записано</span>
      <span class="listing-meta-val"><input id="lm-rel-recorded" class="lm-edit-input" value="${esc(d.release.recorded||'')}" placeholder="студия (по желанию)"></span>
      <span class="listing-meta-key">Ссылки</span>
      <span class="listing-meta-val"><textarea id="lm-rel-links" class="lm-edit-input" rows="3"
        placeholder="по одной ссылке на строку">${esc((d.release.links_urls||[]).join('\n'))}</textarea></span>
    </div>
    <div style="font-size:11px;color:#667;margin:10px 0 6px">ТРЕКИ</div>
    <div id="lm-rel-tracks">${lmTrackRowsHtml(d)}</div>
    <button class="btn btn-sm" style="background:#1a3a6a;color:#7af;margin-top:6px"
      onclick="document.getElementById('lm-file-track').click()">➕ Добавить трек (аудио)</button>
    <span id="lm-track-status" style="color:#9bc;font-size:12px;margin-left:8px"></span>
    <input type="file" id="lm-file-track" accept="audio/*" style="display:none" onchange="lmUploadTrack(this)">
  </div>`;
}

function lmTrackRowsHtml(d) {
  const tracks = d.release.tracks_list || [];
  if (!tracks.length) return '<div style="color:#556;font-size:12px">Треков нет</div>';
  return tracks.map(t => `
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:4px">
      <span style="color:#667;font-size:12px;width:18px">${t.position}.</span>
      <input class="lm-edit-input" style="flex:1" data-track-id="${t.id}" value="${esc(t.title)}">
      <button class="btn btn-sm" style="background:#4a1a1a;color:#f77" onclick="lmTrackDelete(${t.id})">🗑</button>
    </div>`).join('');
}

async function lmTrackDelete(trackId) {
  if (!confirm('Удалить трек?')) return;
  const d = window._currentListing;
  try {
    const r = await fetch(`/api/release_track/${trackId}`, {method:'DELETE'});
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const fresh = await fetch(`/api/listing/${d.id}`).then(x=>x.json());
    d.release = fresh.release;
    window._currentListing = d;
    document.getElementById('lm-rel-tracks').innerHTML = lmTrackRowsHtml(d);
  } catch(e) { alert('Ошибка: ' + e.message); }
}

async function lmUploadTrack(input) {
  const d = window._currentListing;
  const file = input.files && input.files[0];
  if (!file) return;
  const status = document.getElementById('lm-track-status');
  if (status) status.textContent = '⏳ Загрузка…';
  try {
    const r = await fetch(`/api/release/${d.id}/track?filename=${encodeURIComponent(file.name)}`, {
      method: 'POST', headers: {'Content-Type':'application/octet-stream'}, body: file,
    });
    const res = await r.json();
    if (!r.ok) throw new Error(res.detail || r.status);
    const fresh = await fetch(`/api/listing/${d.id}`).then(x=>x.json());
    d.release = fresh.release;
    window._currentListing = d;
    document.getElementById('lm-rel-tracks').innerHTML = lmTrackRowsHtml(d);
    if (status) status.textContent = '✔️';
  } catch(e) {
    if (status) status.textContent = '';
    alert('Ошибка загрузки: ' + e.message);
  }
  input.value = '';
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
  body.title = String(body.title ?? '').trim();
  body.contact = String(body.contact ?? '').trim();
  if (!body.title) { alert('Название не может быть пустым'); return; }
  if (!body.contact) { alert('Контакт не может быть пустым'); return; }
  try {
    // Поля релиза: тип, дата, «записано», ссылки, исполнитель, названия треков
    if (d.release) {
      const relBody = {
        rtype: document.getElementById('lm-rel-rtype')?.value,
        date: document.getElementById('lm-rel-date')?.value ?? '',
        recorded: document.getElementById('lm-rel-recorded')?.value ?? '',
        links_text: document.getElementById('lm-rel-links')?.value ?? '',
      };
      const aSel = document.getElementById('lm-rel-artist');
      if (aSel && aSel.value) relBody.artist_id = parseInt(aSel.value);
      const tracks = [];
      for (const inp of document.querySelectorAll('[data-track-id]')) {
        const orig = (d.release.tracks_list || []).find(t => t.id == inp.dataset.trackId);
        if (orig && inp.value.trim() !== orig.title) {
          tracks.push({id: parseInt(inp.dataset.trackId), title: inp.value.trim()});
        }
      }
      const rr = await fetch(`/api/release/${d.id}/full`, {
        method: 'PATCH', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({...body, release: relBody, tracks})
      });
      if (!rr.ok) {
        let detail = rr.status;
        try { detail = (await rr.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      openListing(d.id);         // перечитать всё с сервера, включая мету
      refreshCatalogIfOpen();
      if (currentTab === 'releases') loadReleases();
      return;
    }
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
    refreshCatalogIfOpen();
  } catch(e) { alert('Ошибка сохранения: ' + e.message); }
}

async function lmToggleRelease() {
  const d = window._currentListing;
  if (!d || !d.release) return;
  try {
    const r = await fetch(`/api/release/${d.id}/toggle_status`, {method:'POST'}).then(x=>x.json());
    if (r && r.ok) {
      openListing(d.id);           // перерисовать модалку со свежим статусом
      refreshCatalogIfOpen();
      if (currentTab === 'releases') loadReleases();
    } else alert('Не удалось: ' + ((r&&r.detail)||'?'));
  } catch(e) { alert('Ошибка: ' + e.message); }
}

async function lmToggleSold() {
  const d = window._currentListing;
  try {
    const r = await fetch(`/api/listing/${d.id}/toggle_sold`, {method:'POST'});
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const res = await r.json();
    window._currentListing.is_sold = res.is_sold;
    renderListingModal(window._currentListing);
    refreshCatalogIfOpen();
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
    refreshCatalogIfOpen();
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

function drillListingsGoPage(offset) {
  openDrillListings(_drillState.catId, _drillState.stype, _drillState.title, offset);
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
  const pag = paginationHtml(data.total, 24, offset, 'drillListingsGoPage');
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

function drillViewsGoPage(offset) {
  openDrillViews(_drillState.catId, _drillState.stype, _drillState.title, _drillState.action, offset);
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
  const pag = paginationHtml(data.total, 50, offset, 'drillViewsGoPage');
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
  const TYPE_NAMES = {market:'Барахолка', service:'Услуги', vacancy:'Вакансии', events:'Афиша', release:'Релизы'};
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
(async()=>{ applyLang(); setupDnD(); await loadAll(); catSetSubtab('market'); })();
</script>
</body>
</html>
"""

@app.get("/healthz")
def healthz():
    with db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

if __name__ == "__main__":
    host = (
        config_value(_ROOT, "CATEGORY_ADMIN_HOST", "127.0.0.1")
        or "127.0.0.1"
    ).strip()
    if host not in {"127.0.0.1", "localhost", "::1"} and not (_ADMIN_USER and _ADMIN_PASSWORD):
        raise RuntimeError(
            "Remote category admin requires CATEGORY_ADMIN_USER and "
            "CATEGORY_ADMIN_PASSWORD"
        )
    print(f"Open: http://{host}:8001")
    uvicorn.run(app, host=host, port=8001, log_level="warning")
