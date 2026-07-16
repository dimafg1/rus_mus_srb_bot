# app/analytics/__init__.py
"""
Аналитика бота.

Единый поток аналитических событий — таблица analytics_events
(Strategy v2, слой 1, §6 «Измеримость») — живёт здесь, в log_event().

Словарь событий (event_type):
    user_started      — вход /start; source = deep-link параметр (None = органика)
    listing_created   — публикация объявления; section + entity_id
    listing_extended  — продление объявления; section + entity_id
    partner_shown     — показ партнёрской кампании (появится на шаге «кампании»)
    partner_opened    — открытие/клик кампании (появится на шаге «кампании»)
    artist_opened     — открытие карточки исполнителя (source: list / rel<id>)

События, которые ЖИВУТ В ОТДЕЛЬНЫХ МОДУЛЯХ ЭТОГО ПАКЕТА и в analytics_events
НЕ дублируются:
    listing_opened / contact_clicked  → listing_views.py (action = open / contact)
    search_performed / search_no_results → search_log.py (results_count = 0)

Правила:
    - section: market / services / vacancy / events (как в listing_views);
    - новые типы событий добавлять в словарь выше и в KNOWN_EVENTS;
    - log_event никогда не бросает исключений — аналитика не ломает бота.
"""
import json
import logging
from typing import Any, Optional

from app.database import SessionLocal
from app.models import AnalyticsEvent

log = logging.getLogger("app.analytics")

KNOWN_EVENTS = {
    "user_started",
    "listing_created",
    "listing_extended",
    "partner_shown",
    "partner_opened",
    "artist_opened",
}


async def log_event(
    event_type: str,
    *,
    user_id: Optional[int] = None,
    section: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    source: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> None:
    try:
        if event_type not in KNOWN_EVENTS:
            log.warning("log_event: неизвестный event_type=%r (записан как есть)", event_type)
        async with SessionLocal() as s:
            s.add(AnalyticsEvent(
                event_type=event_type,
                user_id=user_id,
                section=section,
                entity_type=entity_type,
                entity_id=entity_id,
                source=source,
                meta=json.dumps(meta, ensure_ascii=False) if meta else None,
            ))
            await s.commit()
    except Exception as e:
        log.warning("log_event(%s) failed: %s", event_type, e)
