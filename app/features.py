# app/features.py
"""
Выключатели функций — таблица feature_flags (Strategy v2, слой 1, §6).

Флаги монетизации (посеяны в init_db, все выключены):
    monetization_enabled     — общий рубильник коммерческих функций
    paid_plans_enabled       — тарифы (Pro/Business)
    paid_ranking_enabled     — платное влияние на выдачу
    partner_rotation_enabled — ротация партнёрских кампаний
    payments_enabled         — приём платежей

Использование:
    from app.features import is_enabled
    if await is_enabled("partner_rotation_enabled", user_id=uid):
        ...

Правила:
    - несуществующий флаг = выключен (безопасный дефолт);
    - audience: "all" — всем; "admins" — только админам;
      иначе — список user_id через запятую ("123,456");
    - is_enabled никогда не бросает исключений (ошибка = выключено);
    - значения кэшируются на CACHE_TTL секунд — после правки флага в БД
      бот подхватит изменение без перезапуска, но не мгновенно.
"""
import logging
import time
from typing import Optional

from sqlmodel import select

from app.database import SessionLocal
from app.models import FeatureFlag

log = logging.getLogger("app.features")

CACHE_TTL = 30.0  # секунд
_cache: dict[str, tuple[float, Optional[FeatureFlag]]] = {}


def _audience_allows(audience: str, user_id: Optional[int]) -> bool:
    audience = (audience or "all").strip()
    if audience == "all":
        return True
    if audience == "admins":
        if user_id is None:
            return False
        from app.routers.admin_panel import is_admin  # лениво: избегаем цикла импортов
        return is_admin(user_id)
    # список user_id через запятую
    if user_id is None:
        return False
    try:
        allowed = {int(x) for x in audience.split(",") if x.strip()}
    except ValueError:
        log.warning("audience %r не разобран — считаю флаг выключенным", audience)
        return False
    return user_id in allowed


async def is_enabled(key: str, *, user_id: Optional[int] = None) -> bool:
    try:
        now = time.monotonic()
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL:
            flag = cached[1]
        else:
            async with SessionLocal() as s:
                flag = (await s.execute(
                    select(FeatureFlag).where(FeatureFlag.key == key)
                )).scalar_one_or_none()
            _cache[key] = (now, flag)

        if flag is None:
            log.warning("is_enabled: флаг %r не найден — считаю выключенным", key)
            return False
        if not flag.enabled:
            return False
        return _audience_allows(flag.audience, user_id)
    except Exception as e:
        log.warning("is_enabled(%s) failed: %s", key, e)
        return False


def clear_cache() -> None:
    """Сбросить кэш (для тестов и после ручной правки флагов)."""
    _cache.clear()
