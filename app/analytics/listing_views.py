# -*- coding: utf-8 -*-
"""
app/analytics/listing_views.py

RU: Логирование просмотров карточек и нажатий на контакт в таблицу listing_views.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text as sql

from app.database import SessionLocal


async def log_listing_view(
    *,
    listing_id: int,
    user_id: int,
    section: str,
    action: str,
    source: str | None = None,
) -> None:
    """
    RU: Записать событие по объявлению в listing_views.

    action:
    - open
    - contact

    source:
    - search
    - catalog
    - my
    - direct
    - None

    Исключения наружу не бросаем, чтобы не ломать UX.
    """
    created_at = int(datetime.now(timezone.utc).timestamp())

    q = sql("""
        INSERT INTO listing_views (
            listing_id,
            user_id,
            section,
            action,
            source,
            created_at
        )
        VALUES (
            :listing_id,
            :user_id,
            :section,
            :action,
            :source,
            :created_at
        )
    """)

    params = {
        "listing_id": int(listing_id),
        "user_id": int(user_id),
        "section": str(section),
        "action": str(action),
        "source": (str(source).strip() if source else None),
        "created_at": created_at,
    }

    try:
        async with SessionLocal() as s:
            await s.execute(q, params)
            await s.commit()

        print(
            f"[listing_views.py] log_listing_view ok | "
            f"listing_id={params['listing_id']} user_id={params['user_id']} "
            f"section={params['section']} action={params['action']} "
            f"source={params['source']!r}"
        )
    except Exception as e:
        print(
            f"[listing_views.py] log_listing_view error | "
            f"listing_id={params['listing_id']} user_id={params['user_id']} "
            f"section={params['section']} action={params['action']} "
            f"source={params['source']!r} err={e!r}"
        )