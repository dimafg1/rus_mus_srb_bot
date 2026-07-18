# app/fsm_storage.py
"""FSM-хранилище aiogram поверх SQLite: и в память, и в БД.

Принцип бота «каждое состояние переживает рестарт»: шаг мастера и уже
введённые пользователем данные (заголовок, описание, фото и т.д.) пишутся
в таблицу FsmState при каждом изменении. Чтение идёт из кэша в памяти;
после рестарта кэш пуст и данные поднимаются из БД — пользователь
продолжает мастер с того же места.

Конкурентность: aiogram обрабатывает апдейты параллельно (быстрая серия
фото, двойные нажатия), поэтому запись в БД — атомарный UPSERT, а все
операции по одному ключу сериализуются asyncio.Lock. Иначе параллельные
set_state/set_data затирают друг друга, а update_data теряет обновления.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any, Dict, Mapping, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.models import FsmState, utcnow_naive


def _key_str(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}:{key.destiny}"


class SQLiteFsmStorage(BaseStorage):
    def __init__(self, session_factory=None) -> None:
        # Ленивая привязка: не тянем engine при импорте модуля (важно для тестов)
        if session_factory is None:
            from app.database import SessionLocal
            session_factory = SessionLocal
        self._session_factory = session_factory
        self._state_cache: Dict[str, Optional[str]] = {}
        self._data_cache: Dict[str, Dict[str, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    # ── внутренние помощники ────────────────────────────────────────────
    async def _load_row(self, k: str) -> Optional[FsmState]:
        async with self._session_factory() as s:
            return (
                await s.execute(select(FsmState).where(FsmState.key == k))
            ).scalar_one_or_none()

    async def _upsert(self, k: str, **cols) -> None:
        """Атомарный UPSERT: обновляет только переданные колонки (state и/или data),
        не затирая соседнюю при параллельной записи."""
        cols["updated_at"] = utcnow_naive()
        # На INSERT нужны все NOT NULL-колонки; на UPDATE трогаем только переданные.
        insert_vals = {"key": k, "state": None, "data": "{}"}
        insert_vals.update(cols)
        stmt = sqlite_insert(FsmState).values(**insert_vals)
        stmt = stmt.on_conflict_do_update(
            index_elements=[FsmState.key],
            set_={name: getattr(stmt.excluded, name) for name in cols},
        )
        async with self._session_factory() as s:
            await s.execute(stmt)
            await s.commit()

    # ── интерфейс BaseStorage ───────────────────────────────────────────
    async def set_state(self, key: StorageKey, state: Optional[str | State] = None) -> None:
        k = _key_str(key)
        value = state.state if isinstance(state, State) else state
        async with self._locks[k]:
            self._state_cache[k] = value
            try:
                await self._upsert(k, state=value)
            except Exception as e:
                print(f"[fsm_storage] set_state failed | key={k} | {e}")

    async def get_state(self, key: StorageKey) -> Optional[str]:
        k = _key_str(key)
        async with self._locks[k]:
            return await self._get_state_locked(k)

    async def _get_state_locked(self, k: str) -> Optional[str]:
        if k in self._state_cache:
            return self._state_cache[k]
        try:
            row = await self._load_row(k)
        except Exception as e:
            print(f"[fsm_storage] get_state failed | key={k} | {e}")
            row = None
        value = row.state if row else None
        self._state_cache[k] = value
        return value

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        k = _key_str(key)
        async with self._locks[k]:
            await self._set_data_locked(k, data)

    async def _set_data_locked(self, k: str, data: Mapping[str, Any]) -> None:
        plain = dict(data)
        self._data_cache[k] = plain
        try:
            await self._upsert(k, data=json.dumps(plain, ensure_ascii=False, default=str))
        except Exception as e:
            print(f"[fsm_storage] set_data failed | key={k} | {e}")

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        k = _key_str(key)
        async with self._locks[k]:
            return await self._get_data_locked(k)

    async def _get_data_locked(self, k: str) -> Dict[str, Any]:
        if k in self._data_cache:
            return self._data_cache[k].copy()
        plain: Dict[str, Any] = {}
        try:
            row = await self._load_row(k)
            if row and row.data:
                loaded = json.loads(row.data)
                if isinstance(loaded, dict):
                    plain = loaded
        except Exception as e:
            print(f"[fsm_storage] get_data failed | key={k} | {e}")
        self._data_cache[k] = plain
        return plain.copy()

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> Dict[str, Any]:
        # Перекрываем дефолт BaseStorage (get + set без блокировки):
        # чтение-изменение-запись целиком под замком ключа, иначе
        # параллельные update_data теряют одно из обновлений.
        k = _key_str(key)
        async with self._locks[k]:
            current = await self._get_data_locked(k)
            current.update(data)
            await self._set_data_locked(k, current)
            return current.copy()

    async def close(self) -> None:
        self._state_cache.clear()
        self._data_cache.clear()
        self._locks.clear()
