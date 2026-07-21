# app/fsm_cleanup_worker.py
"""Фоновая чистка брошенных черновиков FsmState.

Раз в сутки удаляет строки FsmState, к которым не притрагивались
TTL_DAYS дней — пользователь, не вернувшийся к мастеру за это время,
считается ушедшим. Кэш в памяти (SQLiteFsmStorage) тут ни при чём: если
пользователь всё же вернётся после чистки, мастер просто начнётся заново,
как для нового пользователя — не крашится и не теряет ничего важного,
кроме уже устаревшего черновика.
Запуск: asyncio.create_task(fsm_cleanup_worker()) в main().
"""

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete

from app.database import SessionLocal
from app.models import FsmState, utcnow_naive

CHECK_INTERVAL_SECONDS = 86400  # раз в сутки
TTL_DAYS = 30

log = logging.getLogger("app.fsm_cleanup")


async def fsm_cleanup_worker() -> None:
    """Бесконечный цикл: тик раз в CHECK_INTERVAL_SECONDS. Ошибки не роняют бот."""
    log.info("fsm cleanup worker started (interval %ss, ttl %sd)", CHECK_INTERVAL_SECONDS, TTL_DAYS)
    while True:
        try:
            await _tick()
        except Exception:
            log.exception("fsm cleanup tick failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _tick() -> None:
    cutoff = utcnow_naive() - timedelta(days=TTL_DAYS)
    async with SessionLocal() as s:
        result = await s.execute(delete(FsmState).where(FsmState.updated_at < cutoff))
        await s.commit()
    if result.rowcount:
        log.info("fsm cleanup: удалено %d брошенных черновиков (старше %d дн.)", result.rowcount, TTL_DAYS)
