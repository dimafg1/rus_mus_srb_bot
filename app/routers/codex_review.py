# -*- coding: utf-8 -*-
"""
RU: Роутер /codex_review — быстрая проверка кода через Codex.
ОГРАНИЧЕНИЕ ДОСТУПА:
    - Допущены только пользователи из ALLOWED_USER_IDS (переменная окружения .env.ai)
      или из ALLOWED_USERS_HARDCODE ниже.

Сценарий:
    - /codex_review -> инструкция
    - Принимаем .py как документ ИЛИ текстом
    - Отправляем в CodexClient -> возвращаем Summary / Patch / Tests

Отладка:
    - Префикс печати [codex_review].

Автор: вы
Дата: 2025-09-17
"""

import os
import re
from typing import Set

from aiogram import Router, types, F
from aiogram.filters import Command

from app.ai.codex_client import CodexClient

router = Router(name="codex_review")

# ───────────────────── Доступ ───────────────────── #
def _parse_allowed_from_env() -> Set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            pass
    return ids

# 1) из .env.ai
ALLOWED_USERS_ENV: Set[int] = _parse_allowed_from_env()
# 2) «хардкод» на всякий случай — можно оставить пустым
ALLOWED_USERS_HARDCODE: Set[int] = set()  # пример: {123456789}

def _is_allowed(user_id: int) -> bool:
    return (user_id in ALLOWED_USERS_ENV) or (user_id in ALLOWED_USERS_HARDCODE)

def _deny_text() -> str:
    return "⛔ У вас нет доступа к этой команде."

# ───────────────────── Основное ───────────────────── #
MAX_TEXT = 32_000
PY_EXT = re.compile(r".*\\.py$", re.IGNORECASE)

@router.message(Command("codex_review"))
async def codex_review_help(m: types.Message):
    print("[codex_review] /codex_review invoked chat=", m.chat.id, "user=", m.from_user.id)
    if not _is_allowed(m.from_user.id):
        await m.answer(_deny_text())
        return

    await m.answer(
        "Отправьте *.py файл документом или вставьте код одним сообщением.\n"
        "Я верну краткий обзор, unified diff и заготовку pytest-тестов."
    )

@router.message(F.document)
async def codex_from_document(m: types.Message):
    print("[codex_review] document received chat=", m.chat.id, "user=", m.from_user.id)
    if not _is_allowed(m.from_user.id):
        await m.answer(_deny_text())
        return

    doc = m.document
    if not doc or not PY_EXT.match(doc.file_name or ""):
        await m.answer("Пришлите, пожалуйста, именно *.py файл документом.")
        return

    try:
        file = await m.bot.get_file(doc.file_id)
        file_bytes = await m.bot.download_file(file.file_path)
        code = file_bytes.read().decode("utf-8", errors="replace")
    except Exception as e:
        print("[codex_review] file download error:", e)
        await m.answer("Не удалось скачать файл.")
        return

    code = code[:MAX_TEXT]
    await _process_and_reply(m, code=code, filename=doc.file_name or "snippet.py")

@router.message(F.text)
async def codex_from_text(m: types.Message):
    print("[codex_review] text received chat=", m.chat.id, "user=", m.from_user.id)
    if not _is_allowed(m.from_user.id):
        await m.answer(_deny_text())
        return

    code = (m.text or "").strip()
    if len(code) < 10:
        await m.answer("Похоже, это не код. Вставьте, пожалуйста, более развёрнутый фрагмент.")
        return
    code = code[:MAX_TEXT]
    await _process_and_reply(m, code=code, filename="snippet.py")

async def _process_and_reply(m: types.Message, code: str, filename: str):
    try:
        await m.answer("Анализирую код в Codex…")
        client = CodexClient()
        summary, patch, tests = client.review_code(code, filename=filename)

        # 1) Summary
        for part in _split_for_tg("Summary:\n" + summary):
            await m.answer(part)

        # 2) Patch (гарантируем code fence)
        patch_block = patch if patch.startswith("```") else f"```diff\n{patch}\n```"
        for part in _split_for_tg(patch_block, prefer_code=True):
            await m.answer(part)

        # 3) Tests
        tests_block = tests if tests.startswith("```") else f"```python\n{tests}\n```"
        for part in _split_for_tg(tests_block, prefer_code=True):
            await m.answer(part)

        print("[codex_review] done for chat=", m.chat.id)

    except Exception as e:
        print("[codex_review] error:", e)
        await m.answer("Ошибка обращения к Codex. Проверьте OPENAI_API_KEY и доступ к модели.")

def _split_for_tg(text: str, prefer_code: bool = False, limit: int = 3800):
    """
    RU: Грубая нарезка больших сообщений для Telegram.
    Если prefer_code=True — закрываем незавершённые ```.
    """
    res = []
    cur = ""
    for line in text.splitlines(True):
        if len(cur) + len(line) > limit:
            res.append(cur)
            cur = ""
        cur += line
    if cur:
        res.append(cur)

    if prefer_code:
        balanced = []
        for chunk in res:
            if chunk.count("```") % 2 != 0:
                chunk += "\n```"
            balanced.append(chunk)
        return balanced
    return res
