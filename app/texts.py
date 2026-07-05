from app.models import BotText
from sqlalchemy import select
from app.database import SessionLocal

async def get_text(code: str, lang: str = "ru") -> str:
    async with SessionLocal() as session:
        result = await session.execute(select(BotText).where(BotText.code == code))
        obj = result.scalar_one_or_none()
        if obj:
            return getattr(obj, f"text_{lang}", None) or obj.text_ru or ""
        return ""
