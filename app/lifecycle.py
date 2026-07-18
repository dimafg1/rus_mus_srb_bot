# app/lifecycle.py
"""Общая логика жизненного цикла объявлений.

Файл не содержит SQL-миграций и не выполняет фоновых задач сам по себе.
Он только предоставляет функции, которые роутеры и будущий планировщик смогут вызывать.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Optional

from app.models import Listing


LISTING_TYPE_MARKET = "market"
LISTING_TYPE_SERVICE = "service"
LISTING_TYPE_VACANCY = "vacancy"
LISTING_TYPE_EVENTS = "events"

EXPIRABLE_TYPES = {
    LISTING_TYPE_MARKET,
    LISTING_TYPE_SERVICE,
    LISTING_TYPE_VACANCY,
}

EVENT_TYPES = {LISTING_TYPE_EVENTS}

STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

REASON_EXPIRED = "expired"
REASON_SOLD = "sold"
REASON_CLOSED = "closed"
REASON_UNPUBLISHED = "unpublished"
REASON_USER_DELETED = "user_deleted"
REASON_ADMIN_REMOVED = "admin_removed"
REASON_EVENT_PASSED = "event_passed"

ACTOR_USER = "user"
ACTOR_ADMIN = "admin"
ACTOR_SYSTEM = "system"

ACTIVE_DAYS = 30
REMIND_BEFORE_DAYS = 5
PURGE_AFTER_DAYS_FROM_CREATED = 365


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _strip_tz(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.replace(tzinfo=None)


def _safe_dt(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return _strip_tz(dt)


def is_expirable(listing: Listing) -> bool:
    return (listing.type or "").strip() in EXPIRABLE_TYPES


def is_event(listing: Listing) -> bool:
    return (listing.type or "").strip() in EVENT_TYPES


def is_active(listing: Listing) -> bool:
    return (listing.status or STATUS_ACTIVE) == STATUS_ACTIVE


def is_archived(listing: Listing) -> bool:
    return (listing.status or STATUS_ACTIVE) == STATUS_ARCHIVED


def default_expires_at(listing: Listing) -> Optional[datetime]:
    if not is_expirable(listing):
        return None

    created_at = _safe_dt(listing.created_at) or utcnow()
    return created_at + timedelta(days=ACTIVE_DAYS)


def ensure_expires_at(listing: Listing) -> bool:
    if not is_expirable(listing):
        return False
    if listing.expires_at is not None:
        return False

    listing.expires_at = default_expires_at(listing)
    return True


def days_left(listing: Listing, *, now: Optional[datetime] = None) -> Optional[int]:
    expires_at = _safe_dt(listing.expires_at)
    if expires_at is None:
        return None

    current = _safe_dt(now) or utcnow()
    seconds = (expires_at - current).total_seconds()
    if seconds <= 0:
        return 0
    return int(ceil(seconds / 86400))


def should_show_extend_button(listing: Listing, *, now: Optional[datetime] = None) -> bool:
    if not is_expirable(listing):
        return False

    if is_active(listing):
        left = days_left(listing, now=now)
        return left is not None and left <= REMIND_BEFORE_DAYS

    # Архивное по истечению срока или закрытое владельцем вручную можно
    # реактивировать: extend_listing() возвращает статус active и сдвигает срок.
    return is_archived(listing) and listing.archive_reason in (REASON_EXPIRED, REASON_CLOSED)


def needs_expiry_reminder(listing: Listing, *, now: Optional[datetime] = None) -> bool:
    # Напоминаем только про ещё активные объявления (до архивации)
    if not is_active(listing):
        return False
    if not should_show_extend_button(listing, now=now):
        return False
    if listing.reminded_at is not None:
        return False
    return True


def can_owner_reactivate(listing: Listing) -> bool:
    """Может ли владелец сам вернуть/продлить объявление.

    Активное — можно продлевать; архивное — только если истекло само или
    закрыто владельцем. Снятое админом (admin_removed) или снятое с
    публикации (unpublished) старой кнопкой не возвращается.
    """
    if is_active(listing):
        return True
    return is_archived(listing) and listing.archive_reason in (REASON_EXPIRED, REASON_CLOSED)


def extend_listing(listing: Listing, *, days: int = ACTIVE_DAYS, now: Optional[datetime] = None) -> None:
    if not is_expirable(listing):
        return

    current = _safe_dt(now) or utcnow()

    if is_archived(listing):
        # Реактивация из архива (закрыто владельцем или истекло): срок строго
        # от «сейчас». Иначе цикл «закрыть → вернуть» накручивал бы
        # остаток + 30 дней при каждом обороте.
        base = current
    else:
        base = _safe_dt(listing.expires_at)
        if base is None:
            base = default_expires_at(listing) or current
        if base < current:
            base = current

    listing.status = STATUS_ACTIVE
    listing.archive_reason = None
    listing.archived_at = None
    listing.archived_by = None
    listing.archived_by_user_id = None
    listing.reminded_at = None
    listing.expires_at = base + timedelta(days=days)


def archive_listing(
    listing: Listing,
    *,
    reason: str,
    actor: str = ACTOR_SYSTEM,
    actor_user_id: Optional[int] = None,
    now: Optional[datetime] = None,
) -> None:
    current = _safe_dt(now) or utcnow()

    listing.status = STATUS_ARCHIVED
    listing.archive_reason = reason
    listing.archived_by = actor
    listing.archived_by_user_id = actor_user_id
    listing.archived_at = current

    if reason == REASON_SOLD:
        listing.is_sold = True


def archive_as_expired(listing: Listing, *, now: Optional[datetime] = None) -> None:
    archive_listing(listing, reason=REASON_EXPIRED, actor=ACTOR_SYSTEM, now=now)


def archive_as_sold(listing: Listing, *, user_id: Optional[int] = None, now: Optional[datetime] = None) -> None:
    archive_listing(listing, reason=REASON_SOLD, actor=ACTOR_USER, actor_user_id=user_id, now=now)


def archive_as_closed(listing: Listing, *, user_id: Optional[int] = None, now: Optional[datetime] = None) -> None:
    archive_listing(listing, reason=REASON_CLOSED, actor=ACTOR_USER, actor_user_id=user_id, now=now)


def archive_as_unpublished(listing: Listing, *, user_id: Optional[int] = None, now: Optional[datetime] = None) -> None:
    archive_listing(listing, reason=REASON_UNPUBLISHED, actor=ACTOR_USER, actor_user_id=user_id, now=now)


def archive_as_user_deleted(listing: Listing, *, user_id: Optional[int] = None, now: Optional[datetime] = None) -> None:
    archive_listing(listing, reason=REASON_USER_DELETED, actor=ACTOR_USER, actor_user_id=user_id, now=now)


def archive_as_admin_removed(listing: Listing, *, admin_id: Optional[int] = None, now: Optional[datetime] = None) -> None:
    archive_listing(listing, reason=REASON_ADMIN_REMOVED, actor=ACTOR_ADMIN, actor_user_id=admin_id, now=now)


def archive_as_event_passed(listing: Listing, *, now: Optional[datetime] = None) -> None:
    archive_listing(listing, reason=REASON_EVENT_PASSED, actor=ACTOR_SYSTEM, now=now)


def is_expired(listing: Listing, *, now: Optional[datetime] = None) -> bool:
    if not is_active(listing) or not is_expirable(listing):
        return False

    expires_at = _safe_dt(listing.expires_at)
    if expires_at is None:
        return False

    current = _safe_dt(now) or utcnow()
    return expires_at <= current


def should_purge(listing: Listing, *, now: Optional[datetime] = None) -> bool:
    created_at = _safe_dt(listing.created_at)
    if created_at is None:
        return False

    current = _safe_dt(now) or utcnow()
    return created_at + timedelta(days=PURGE_AFTER_DAYS_FROM_CREATED) <= current


def days_left_text(listing: Listing, *, now: Optional[datetime] = None) -> Optional[str]:
    left = days_left(listing, now=now)
    if left is None:
        return None

    if left == 0:
        return "⏳ До архивации: сегодня"
    if left == 1:
        return "⏳ До архивации: 1 день"
    if 2 <= left <= 4:
        return f"⏳ До архивации: {left} дня"
    return f"⏳ До архивации: {left} дней"
