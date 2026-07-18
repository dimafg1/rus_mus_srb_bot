# app/fsm_storage.py
"""FSM-хранилище aiogram поверх SQLite: и в память, и в БД.

Принцип бота «каждое состояние переживает рестарт»: шаг мастера и уже
введённые пользователем данные (заголовок, описание, фото и т.д.) пишутся
в таблицу FsmState при каждом изменении. Чтение идёт из кэша в памяти;
после рестарта кэш пуст и данные поднимаются из БД — пользователь
продолжает мастер с того же места.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from sqlalchemy import select

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

    # ── внутренние помощники ────────────────────────────────────────────
    async def _load_row(self, k: str) -> Optional[FsmState]:
        async with self._session_factory() as s:
            return (
                await s.execute(select(FsmState).where(FsmState.key == k))
            ).scalar_one_or_none()

    async def _upsert(self, k: str, *, state=..., data=...) -> None:
        async with self._session_factory() as s:
            row = (
                await s.execute(select(FsmState).where(FsmState.key == k))
            ).scalar_one_or_none()
            if row is None:
                row = FsmState(key=k)
                s.add(row)
            if state is not ...:
                row.state = state
            if data is not ...:
                row.data = data
            row.updated_at = utcnow_naive()
            await s.commit()

    # ── интерфейс BaseStorage ───────────────────────────────────────────
    async def set_state(self, key: StorageKey, state: Optional[str | State] = None) -> None:
        k = _key_str(key)
        value = state.state if isinstance(state, State) else state
        self._state_cache[k] = value
        try:
            await self._upsert(k, state=value)
        except Exception as e:
            print(f"[fsm_storage] set_state failed | key={k} | {e}")

    async def get_state(self, key: StorageKey) -> Optional[str]:
        k = _key_str(key)
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
        plain = dict(data)
        self._data_cache[k] = plain
        try:
            await self._upsert(k, data=json.dumps(plain, ensure_ascii=False, default=str))
        except Exception as e:
            print(f"[fsm_storage] set_data failed | key={k} | {e}")

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        k = _key_str(key)
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

    async def close(self) -> None:
        self._state_cache.clear()
        self._data_cache.clear()
