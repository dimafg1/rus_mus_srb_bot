# -*- coding: utf-8 -*-
"""
app/routers/utils_kb.py
RU: Грид для кнопок городов по правилу:
- максимум 3 в ряд;
- если остаток 1 (и рядов троек >= 1) -> разбиваем одну «3» на «2+2».
"""

from __future__ import annotations
from typing import List
from aiogram.types import InlineKeyboardButton

def grid3(buttons: List[InlineKeyboardButton]) -> List[List[InlineKeyboardButton]]:
    n = len(buttons)
    if n <= 0:
        return []
    # базовая разбивка на "3"
    full_rows = n // 3
    rem = n % 3

    pattern = []  # список размеров рядов
    if rem == 0:
        pattern = [3] * full_rows
    elif rem == 1:
        if full_rows >= 1:
            # пример: 4 -> 2+2; 7 -> 3+2+2; 10 -> 3+3+2+2 …
            pattern = [3] * (full_rows - 1) + [2, 2]
        else:
            # n == 1
            pattern = [1]
    else:  # rem == 2
        pattern = [3] * full_rows + [2]

    rows: List[List[InlineKeyboardButton]] = []
    i = 0
    for size in pattern:
        rows.append(buttons[i:i+size])
        i += size
    return rows
