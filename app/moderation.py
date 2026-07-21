# app/moderation.py
"""
Ограничение на запись (mute) — админ может запретить пользователю публиковать
новый контент (Барахолка/Услуги/Вакансии/Афиша/Исполнители/Релизы), не трогая
доступ к просмотру разделов бота. Обратная связь НЕ проверяется этой функцией:
это единственный канал апелляции для замьюченного пользователя.

Использование:
    from app.moderation import is_muted
    if await is_muted(user_id):
        ...сообщить об ограничении и не пускать в мастер...
"""
from sqlmodel import select

from app.database import SessionLocal
from app.models import BotUser


async def is_muted(user_id: int) -> bool:
    try:
        async with SessionLocal() as s:
            user = (await s.execute(
                select(BotUser).where(BotUser.user_id == user_id)
            )).scalar_one_or_none()
        return bool(user and user.is_muted)
    except Exception:
        # Сбой проверки не должен блокировать публикацию для всех подряд —
        # безопасный дефолт здесь "не замьючен", как и для is_enabled().
        return False
