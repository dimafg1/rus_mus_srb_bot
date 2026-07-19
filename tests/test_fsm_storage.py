import json
import tempfile
import unittest
from pathlib import Path

from aiogram.fsm.storage.base import StorageKey
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.fsm_storage import SQLiteFsmStorage
from app.models import FsmState  # noqa: F401 — регистрирует таблицу в metadata


KEY = StorageKey(bot_id=1, chat_id=100, user_id=100)
OTHER_KEY = StorageKey(bot_id=1, chat_id=200, user_id=200)


class FsmStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "fsm_test.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmp.cleanup()

    def _storage(self) -> SQLiteFsmStorage:
        return SQLiteFsmStorage(session_factory=self.session_factory)

    async def test_state_and_data_roundtrip(self):
        storage = self._storage()
        await storage.set_state(KEY, "Sell:descr")
        await storage.set_data(KEY, {"title": "BD 770 PRO", "photos": ["f1", "f2"]})

        self.assertEqual(await storage.get_state(KEY), "Sell:descr")
        self.assertEqual(
            await storage.get_data(KEY),
            {"title": "BD 770 PRO", "photos": ["f1", "f2"]},
        )

    async def test_restart_mid_wizard_restores_state_and_data(self):
        # «Рестарт»: первый экземпляр пишет, второй (с пустым кэшем) читает из БД.
        first = self._storage()
        await first.set_state(KEY, "Sell:price")
        await first.set_data(KEY, {"title": "Заголовок", "descr": "Описание", "photos": ["fid"]})

        second = self._storage()
        self.assertEqual(await second.get_state(KEY), "Sell:price")
        self.assertEqual(
            await second.get_data(KEY),
            {"title": "Заголовок", "descr": "Описание", "photos": ["fid"]},
        )

    async def test_clear_survives_restart(self):
        # FSMContext.clear() → set_state(None) + set_data({}); после рестарта пусто.
        first = self._storage()
        await first.set_state(KEY, "Sell:photo")
        await first.set_data(KEY, {"title": "x"})
        await first.set_state(KEY, None)
        await first.set_data(KEY, {})

        second = self._storage()
        self.assertIsNone(await second.get_state(KEY))
        self.assertEqual(await second.get_data(KEY), {})

    async def test_keys_are_isolated(self):
        storage = self._storage()
        await storage.set_state(KEY, "Sell:title")
        await storage.set_data(KEY, {"title": "a"})

        self.assertIsNone(await storage.get_state(OTHER_KEY))
        self.assertEqual(await storage.get_data(OTHER_KEY), {})

    async def test_get_data_returns_copy_not_shared_reference(self):
        storage = self._storage()
        await storage.set_data(KEY, {"photos": ["f1"]})

        snapshot = await storage.get_data(KEY)
        snapshot["photos"].append("mutated")
        snapshot["extra"] = True

        fresh = self._storage()
        self.assertEqual(await fresh.get_data(KEY), {"photos": ["f1"]})

    async def test_update_data_merges_and_persists(self):
        # update_data из BaseStorage должен опираться на наши get/set.
        first = self._storage()
        await first.set_data(KEY, {"title": "a"})
        merged = await first.update_data(KEY, {"descr": "b"})
        self.assertEqual(merged, {"title": "a", "descr": "b"})

        second = self._storage()
        self.assertEqual(await second.get_data(KEY), {"title": "a", "descr": "b"})

    async def test_concurrent_set_state_and_set_data_both_persist(self):
        # Гонка: параллельные set_state и set_data не должны затирать друг друга в БД.
        import asyncio
        storage = self._storage()
        await asyncio.gather(
            storage.set_state(KEY, "Sell:photo"),
            storage.set_data(KEY, {"title": "x", "photos": ["f1"]}),
        )

        fresh = self._storage()  # читает только из БД
        self.assertEqual(await fresh.get_state(KEY), "Sell:photo")
        self.assertEqual(await fresh.get_data(KEY), {"title": "x", "photos": ["f1"]})

    async def test_concurrent_update_data_keeps_both_updates(self):
        # Гонка: два параллельных update_data — оба обновления должны выжить.
        import asyncio
        storage = self._storage()
        await storage.set_data(KEY, {"base": 1})
        await asyncio.gather(
            storage.update_data(KEY, {"a": "A"}),
            storage.update_data(KEY, {"b": "B"}),
        )

        fresh = self._storage()
        self.assertEqual(await fresh.get_data(KEY), {"base": 1, "a": "A", "b": "B"})

    async def test_concurrent_burst_all_keys_survive(self):
        # Шквал параллельных обновлений (как альбом из 10 фото) — ни одно не теряется.
        import asyncio
        storage = self._storage()
        await asyncio.gather(*[
            storage.update_data(KEY, {f"k{i}": i}) for i in range(10)
        ])

        fresh = self._storage()
        data = await fresh.get_data(KEY)
        self.assertEqual(data, {f"k{i}": i for i in range(10)})

    async def test_cyrillic_stored_readably(self):
        storage = self._storage()
        await storage.set_data(KEY, {"title": "Синтезатор"})
        async with self.session_factory() as s:
            from sqlalchemy import select
            row = (await s.execute(select(FsmState))).scalars().first()
        self.assertIn("Синтезатор", row.data)
        self.assertEqual(json.loads(row.data)["title"], "Синтезатор")

    async def test_clear_evicts_in_memory_draft_but_db_stays_cleared(self):
        # Завершённый мастер не должен оставаться в кэше памяти (иначе рост
        # O(число пользователей)); при этом в БД остаётся очищенное состояние.
        storage = self._storage()
        k = "1:100:100:default"
        await storage.set_state(KEY, "Sell:photo")
        await storage.set_data(KEY, {"title": "x", "photos": ["f1", "f2"]})
        self.assertIn(k, storage._data_cache)

        await storage.set_state(KEY, None)
        await storage.set_data(KEY, {})  # как делает clear()

        # Тяжёлый черновик выселен из обоих кэшей памяти
        self.assertNotIn(k, storage._data_cache)
        self.assertNotIn(k, storage._state_cache)
        # Но чтение (из БД) отдаёт корректное очищенное состояние
        self.assertIsNone(await storage.get_state(KEY))
        self.assertEqual(await storage.get_data(KEY), {})

    async def test_cancel_step_keeps_data_cached(self):
        # set_state(None) без set_data({}) — это «отмена шага» (напр. правка
        # вакансии): данные (контекст поиска) должны остаться, не выселяться.
        storage = self._storage()
        k = "1:100:100:default"
        await storage.set_state(KEY, "Edit:title")
        await storage.set_data(KEY, {"search_ctx": [1, 2, 3]})
        await storage.set_state(KEY, None)

        self.assertIn(k, storage._data_cache)
        self.assertEqual(await storage.get_data(KEY), {"search_ctx": [1, 2, 3]})


if __name__ == "__main__":
    unittest.main()
