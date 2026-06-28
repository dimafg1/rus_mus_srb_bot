# -*- coding: utf-8 -*-
"""
RU: Codex-клиент для вызова OpenAI Responses API.
    Формирует промпт, отправляет код и возвращает 3 секции:
    - Summary: краткие проблемы/замечания
    - Patch: unified diff (можно git apply)
    - Tests: pytest-скелет

Безопасность:
    - Ключ OPENAI_API_KEY берётся из окружения.
    - Модель можно задать переменной CODEX_MODEL (по умолчанию gpt-5-codex, замените на доступную).

Отладка:
    - Печатаем этапы с префиксом [codex_client] для grep/логов.

Автор: вы
Дата: 2025-09-17
"""

import os
import textwrap
from typing import Tuple, Optional

try:
    from openai import OpenAI
except Exception as e:
    # Чтобы не падать при импорте, но видеть проблему
    print("[codex_client] Ошибка импорта openai SDK:", e)
    OpenAI = None  # type: ignore


MODEL_DEFAULT = os.getenv("CODEX_MODEL", "gpt-5-codex")  # замените на актуальную модель, если недоступна


class CodexClient:
    def __init__(self, model: Optional[str] = None):
        print("[codex_client] init CodexClient")
        if OpenAI is None:
            raise RuntimeError("OpenAI SDK не установлен. Выполните: pip install openai")
        self.client = OpenAI()  # читает OPENAI_API_KEY из окружения
        self.model = model or MODEL_DEFAULT

    def build_prompt(self, code: str, filename: str = "snippet.py") -> str:
        print("[codex_client] build_prompt filename=", filename)
        return textwrap.dedent(f"""
        You are Codex — an expert coding assistant for Python/aiogram.

        TASK:
        1) Review the following code and list concrete issues (bugs, race, style, security).
        2) Propose a unified diff patch (git-style) that fixes issues without changing public behavior.
        3) Suggest minimal pytest tests (no external I/O) to cover the fixes.

        PROJECT RULES:
        - Russian headers in modules.
        - Explicit debug prints with file/func names.
        - Keep handler names stable where possible.

        FILENAME: {filename}

        CODE:
        ----------------
        {code}
        ----------------

        FORMAT:
        - "Summary:" short bullet list of key problems.
        - "Patch:" one ```diff fenced block with unified diff.
        - "Tests:" a ```python fenced block with pytest tests.
        """)

    def review_code(self, code: str, filename: str = "snippet.py") -> Tuple[str, str, str]:
        """
        Возвращает кортеж (summary, patch, tests) — каждое поле строка.
        """
        print("[codex_client] review_code start, len(code)=", len(code))
        prompt = self.build_prompt(code, filename=filename)

        resp = self.client.responses.create(
            model=self.model,
            input=prompt,
            # Можно тюнить: temperature=0.2, max_output_tokens=2000
        )
        print("[codex_client] got response")

        # Универсальная распаковка (Responses API даёт удобный output_text)
        text = resp.output_text if hasattr(resp, "output_text") else str(resp)
        # Парсим три секции
        summary, patch, tests = "", "", ""
        cur = None
        for line in text.splitlines():
            low = line.strip().lower()
            if low.startswith("summary:"):
                cur = "summary"; continue
            if low.startswith("patch:"):
                cur = "patch"; continue
            if low.startswith("tests:"):
                cur = "tests"; continue

            if cur == "summary":
                summary += line + "\n"
            elif cur == "patch":
                patch += line + "\n"
            elif cur == "tests":
                tests += line + "\n"

        summary = (summary or "").strip() or "No summary parsed."
        patch = (patch or "").strip() or "No patch parsed."
        tests = (tests or "").strip() or "# No tests parsed."

        print("[codex_client] review_code done")
        return summary, patch, tests
