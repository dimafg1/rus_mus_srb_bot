# -*- coding: utf-8 -*-
"""
app/routers/utils_category_title.py (v3)
RU: Добавляет "🔽" к имени категории, если у неё есть дети.
Фикс: используем sqlalchemy.text(...) и корректный AsyncResult.all()
"""

from __future__ import annotations
import time
from typing import Optional, Set
from sqlalchemy import text  # <-- критично

FILE = "routers/utils_category_title"
VERSION = "v3"

_parent_ids_cache: Optional[Set[int]] = None
_parent_ids_cache_ts: float = 0.0
_PARENT_IDS_TTL = 60.0  # сек

async def _load_parent_ids_with_children_async(session_factory) -> Set[int]:
    try:
        async with session_factory() as s:
            result = await s.execute(
                text("SELECT DISTINCT parent_id FROM category WHERE parent_id IS NOT NULL")
            )
            rows = result.all()
            parent_ids = {int(pid) for (pid,) in rows if pid is not None}
            return parent_ids
    except Exception as e:
        print(f"[{FILE}] load_parent_ids ERROR: {e}")
        return set()

async def _ensure_cache(session_factory) -> Set[int]:
    global _parent_ids_cache, _parent_ids_cache_ts
    now = time.time()
    if _parent_ids_cache is None or (now - _parent_ids_cache_ts) > _PARENT_IDS_TTL:
        _parent_ids_cache = await _load_parent_ids_with_children_async(session_factory)
        _parent_ids_cache_ts = now
    return _parent_ids_cache or set()

async def format_category_title(cat_id: int, base_name: str, session_factory) -> str:
    """
    Возвращает base_name + ' 🔽', если у категории есть дочерние.
    Не дублирует значок, если он уже есть в name.
    """
    try:
        name = (base_name or "").strip()
        parents = await _ensure_cache(session_factory)
        if "🔽" in name:
            out = name
        elif cat_id in parents:
            out = f"{name} 🔽"
        else:
            out = name
        return out
    except Exception as e:
        print(f"[{FILE}] format_category_title ERROR cat_id={cat_id}: {e}")
        return base_name
