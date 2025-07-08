from app.models import BotText
from sqlalchemy import select
from app.database import SessionLocal

async def get_text(code: str, lang: str = "ru") -> str:
    """
    Возвращает текст по коду (например, greeting, help).
    :param code: Кодовое имя текста
    :param lang: Язык (по умолчанию 'ru')
    :return: Строка с текстом или пустая строка если не найдено
    """
    async with SessionLocal() as session:
        result = await session.execute(
            select(BotText).where(BotText.code == code, BotText.lang == lang)
        )
        obj = result.scalar_one_or_none()
        if obj:
            return obj.text
        return ""
