# -*- coding: utf-8 -*-
"""
app/search/fuzzy.py

RU: Общий helper для поиска:
- нормализация строки
- exact / partial
- исправление одиночной опечатки через словарь токенов
- возврат режима совпадения для аналитики

Ничего не знает о Telegram/aiogram/FSM.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class SearchOutcome:
    query_raw: str
    query_normalized: str
    query_effective: str
    match_mode: str   # exact | partial | corrected | none
    results: list[T]


def normalize_search_text(text: str | None) -> str:
    """
    RU: Нормализация текста для поиска.

    Делает:
    - casefold
    - ё -> е
    - удаление лишней пунктуации
    - схлопывание пробелов
    """
    s = (text or "").casefold()
    s = s.replace("ё", "е")
    s = re.sub(r"[^\w\s]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _collect_tokens(parts: list[str]) -> set[str]:
    """
    RU: Собираем словарь токенов из title/descr и т.п.
    Берём только токены длиной 3+, чтобы не плодить мусор.
    """
    out: set[str] = set()
    for part in parts:
        for token in part.split():
            if len(token) >= 3:
                out.add(token)
    return out


def _run_exact_partial(
    prepared: list[tuple[int, T, list[str]]],
    query_normalized: str,
) -> tuple[str, list[T]]:
    """
    RU: Обычный поиск.
    Возвращает:
    - exact, если найдено только точное совпадение
    - partial, если есть вхождение
    - none, если пусто
    """
    exact_hits: list[T] = []
    partial_hits: list[T] = []

    for _, item, parts in prepared:
        is_exact = any(query_normalized == part for part in parts)
        is_partial = any(query_normalized in part for part in parts)

        if is_exact:
            exact_hits.append(item)

        if is_partial:
            partial_hits.append(item)

    if partial_hits:
        exact_ids = {id(x) for x in exact_hits}
        ordered_results = exact_hits + [x for x in partial_hits if id(x) not in exact_ids]

        match_mode = "exact" if exact_hits and len(ordered_results) == len(exact_hits) else "partial"
        return match_mode, ordered_results

    return "none", []

def _damerau_levenshtein_distance(a: str, b: str) -> int:
    """
    RU: Расстояние Дамерау–Левенштейна.
    Учитывает:
    - вставку
    - удаление
    - замену
    - соседнюю перестановку
    """
    len_a = len(a)
    len_b = len(b)

    if len_a == 0:
        return len_b
    if len_b == 0:
        return len_a

    d = [[0] * (len_b + 1) for _ in range(len_a + 1)]

    for i in range(len_a + 1):
        d[i][0] = i
    for j in range(len_b + 1):
        d[0][j] = j

    for i in range(1, len_a + 1):
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1

            d[i][j] = min(
                d[i - 1][j] + 1,      # удаление
                d[i][j - 1] + 1,      # вставка
                d[i - 1][j - 1] + cost,  # замена
            )

            if (
                i > 1 and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)

    return d[len_a][len_b]


def _correct_single_token_query(
    query_normalized: str,
    vocabulary: set[str],
    prepared: list[tuple[int, T, list[str]]],
    *,
    min_len_for_correction: int = 4,
    min_score: float = 0.84,
    max_distance: int = 1,
    top_k: int = 7,
) -> str | None:
    """
    RU: Пытаемся исправить ОДНОСЛОВНЫЙ запрос.

    Логика:
    - берём кандидатов по двум критериям:
      1) высокая похожесть SequenceMatcher
      2) малая редакционная дистанция (включая соседнюю перестановку)
    - потом выбираем не просто "самое похожее",
      а то слово, которое даёт лучшую реальную выдачу.
    """

    tokens = query_normalized.split()
    if len(tokens) != 1:
        return None

    token = tokens[0]
    if len(token) < min_len_for_correction:
        return None

    scored: list[tuple[float, int, str]] = []

    for cand in vocabulary:
        if not cand:
            continue
        if cand == token:
            continue
        if cand[0] != token[0]:
            continue
        if abs(len(cand) - len(token)) > 2:
            continue

        score = _similarity(token, cand)
        dist = _damerau_levenshtein_distance(token, cand)

        # RU: Кандидат берём либо по хорошей похожести,
        # либо по маленькой дистанции редактирования.
        if score >= min_score or dist <= max_distance:
            scored.append((score, dist, cand))

    if not scored:
        return None

    scored.sort(key=lambda x: (x[1], -x[0], len(x[2]), x[2]))
    near_candidates = [cand for _, _, cand in scored[:top_k]]

    best_candidate: str | None = None
    best_hits = -1
    best_mode_rank = -1
    best_len = 10**9
    best_score_value = -1.0
    best_dist = 10**9

    def _mode_rank(mode: str) -> int:
        if mode == "exact":
            return 2
        if mode == "partial":
            return 1
        return 0

    for cand in near_candidates:
        mode, results = _run_exact_partial(prepared, cand)
        hits = len(results)
        mode_rank = _mode_rank(mode)

        cand_row = next((row for row in scored if row[2] == cand), None)
        cand_score = cand_row[0] if cand_row else 0.0
        cand_dist = cand_row[1] if cand_row else 999

        # Приоритет:
        # 1) больше результатов
        # 2) stronger mode
        # 3) меньшая дистанция
        # 4) более короткое слово
        # 5) чуть лучший similarity
        if (
            hits > best_hits
            or (
                hits == best_hits
                and (
                    mode_rank > best_mode_rank
                    or (
                        mode_rank == best_mode_rank
                        and (
                            cand_dist < best_dist
                            or (
                                cand_dist == best_dist
                                and (
                                    len(cand) < best_len
                                    or (
                                        len(cand) == best_len
                                        and cand_score > best_score_value
                                    )
                                )
                            )
                        )
                    )
                )
            )
        ):
            best_candidate = cand
            best_hits = hits
            best_mode_rank = mode_rank
            best_len = len(cand)
            best_score_value = cand_score
            best_dist = cand_dist

    return best_candidate if best_hits > 0 else None



def _generate_adjacent_swaps(token: str) -> list[str]:
    """
    RU: Генерируем варианты строки с перестановкой соседних символов.
    Пример:
    наушинк -> наушник
    """
    variants: list[str] = []
    chars = list(token)

    for i in range(len(chars) - 1):
        swapped = chars[:]
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
        variants.append("".join(swapped))

    # сохраняем порядок, убираем дубли
    seen = set()
    out = []
    for v in variants:
        if v not in seen and v != token:
            seen.add(v)
            out.append(v)
    return out
    

def search_items(
    items: Iterable[T],
    query: str,
    text_getter: Callable[[T], list[str]],
) -> SearchOutcome:
    """
    RU: Общий поиск по списку объектов.

    Порядок:
    1) exact / partial по исходному запросу
    2) если пусто — пробуем соседнюю перестановку букв в самом запросе
    3) если пусто — пробуем словарную коррекцию
    4) иначе none
    """
    query_raw = (query or "").strip()
    query_normalized = normalize_search_text(query_raw)

    if not query_normalized:
        return SearchOutcome(
            query_raw=query_raw,
            query_normalized=query_normalized,
            query_effective=query_normalized,
            match_mode="none",
            results=[],
        )

    prepared: list[tuple[int, T, list[str]]] = []
    vocabulary: set[str] = set()

    for idx, item in enumerate(items):
        raw_parts = text_getter(item) or []
        norm_parts = [normalize_search_text(x) for x in raw_parts if normalize_search_text(x)]
        prepared.append((idx, item, norm_parts))
        vocabulary |= _collect_tokens(norm_parts)

    # 1. Обычный поиск по исходному запросу
    base_mode, base_results = _run_exact_partial(prepared, query_normalized)
    if base_results:
        return SearchOutcome(
            query_raw=query_raw,
            query_normalized=query_normalized,
            query_effective=query_normalized,
            match_mode=base_mode,
            results=base_results,
        )

    # 2. Проверяем соседнюю перестановку букв в самом запросе
    tokens = query_normalized.split()
    if len(tokens) == 1 and len(tokens[0]) >= 4:
        swap_variants = _generate_adjacent_swaps(tokens[0])

        best_swap: str | None = None
        best_swap_mode_rank = -1
        best_swap_hits = -1
        best_swap_results: list[T] = []

        def _mode_rank(mode: str) -> int:
            if mode == "exact":
                return 2
            if mode == "partial":
                return 1
            return 0

        for variant in swap_variants:
            mode, results = _run_exact_partial(prepared, variant)
            hits = len(results)
            mode_rank = _mode_rank(mode)

            if (
                hits > best_swap_hits
                or (
                    hits == best_swap_hits
                    and mode_rank > best_swap_mode_rank
                )
            ):
                best_swap = variant
                best_swap_hits = hits
                best_swap_mode_rank = mode_rank
                best_swap_results = results

        if best_swap and best_swap_hits > 0:
            return SearchOutcome(
                query_raw=query_raw,
                query_normalized=query_normalized,
                query_effective=best_swap,
                match_mode="corrected",
                results=best_swap_results,
            )

    # 3. Пытаемся исправить запрос через словарь
    corrected_query = _correct_single_token_query(query_normalized, vocabulary, prepared)
    if corrected_query:
        corrected_mode, corrected_results = _run_exact_partial(prepared, corrected_query)
        if corrected_results:
            return SearchOutcome(
                query_raw=query_raw,
                query_normalized=query_normalized,
                query_effective=corrected_query,
                match_mode="corrected",
                results=corrected_results,
            )

    # 4. Ничего не найдено
    return SearchOutcome(
        query_raw=query_raw,
        query_normalized=query_normalized,
        query_effective=query_normalized,
        match_mode="none",
        results=[],
    )

