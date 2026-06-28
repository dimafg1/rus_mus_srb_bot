# -*- coding: utf-8 -*-
"""
app/analytics/search_log.py

RU: Логирование поисковых запросов в таблицу search_log.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text as sql

from app.database import SessionLocal


async def log_search(
    *,
    user_id: int,
    section: str,
    query_raw: str,
    query_normalized: str | None,
    query_effective: str | None,
    match_mode: str,
    results_count: int,
) -> None:
    """
    RU: Записать факт поискового запроса в search_log.
    Никаких исключений наружу не бросаем, чтобы не ломать UX поиска.
    """
    created_at = int(datetime.now(timezone.utc).timestamp())

    q = sql("""
        INSERT INTO search_log (
            user_id,
            section,
            query_raw,
            query_normalized,
            query_effective,
            match_mode,
            results_count,
            created_at
        )
        VALUES (
            :user_id,
            :section,
            :query_raw,
            :query_normalized,
            :query_effective,
            :match_mode,
            :results_count,
            :created_at
        )
    """)

    params = {
        "user_id": int(user_id),
        "section": str(section),
        "query_raw": (query_raw or "").strip(),
        "query_normalized": (query_normalized or "").strip() or None,
        "query_effective": (query_effective or "").strip() or None,
        "match_mode": str(match_mode or "none"),
        "results_count": int(results_count or 0),
        "created_at": created_at,
    }

    try:
        async with SessionLocal() as s:
            await s.execute(q, params)
            await s.commit()
        print(
            f"[search_log.py] log_search ok | "
            f"user_id={params['user_id']} section={params['section']} "
            f"raw={params['query_raw']!r} effective={params['query_effective']!r} "
            f"mode={params['match_mode']} results={params['results_count']}"
        )
    except Exception as e:
        print(
            f"[search_log.py] log_search error | "
            f"user_id={params['user_id']} section={params['section']} "
            f"raw={params['query_raw']!r} err={e!r}"
        )