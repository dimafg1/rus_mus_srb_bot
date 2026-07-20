# tests/test_audit_package_fixes.py
"""Регрессии пакета исправлений по аудиту (июль 2026).

Закрепляет: пер-разделные callback напоминаний + экранирование заголовка;
отмену редактирования вакансии без потери контекста поиска; маршрут после
удаления в родной раздел; возврат на ту же страницу поиска (offset);
счётчик подряд идущих сбоев записи FSM.
"""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.fsm.storage.base import StorageKey
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app import admin_ids, lifecycle_worker
from app.fsm_storage import SQLiteFsmStorage
from app.models import FsmState  # noqa: F401 — регистрирует таблицу в metadata
from app.routers import (
    admin_panel, feedback, market_add, market_view, services_view,
    vacancy_edit_overview,
)


class FakeState:
    def __init__(self, data=None, current_state=None):
        self.values = dict(data or {})
        self.current_state = current_state
        self.cleared = False

    async def get_data(self):
        return dict(self.values)

    async def update_data(self, values=None, **kwargs):
        if values:
            self.values.update(values)
        self.values.update(kwargs)

    async def get_state(self):
        return self.current_state

    async def set_state(self, value):
        self.current_state = getattr(value, "state", value)

    async def clear(self):
        self.values.clear()
        self.current_state = None
        self.cleared = True


def fake_callback(data, user_id=17, chat_id=1001):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id),
        bot=SimpleNamespace(),
        answer=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        delete=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, username="tester"),
        message=message,
        bot=message.bot,
        answer=AsyncMock(),
    )


def _kb_callbacks(markup):
    """Все callback_data клавиатуры построчно."""
    return [[btn.callback_data for btn in row] for row in markup.inline_keyboard]


# ─────────────────────────────────────────────────────────────────────────────
# Напоминания о продлении: форматы callback по разделам + экранирование
# ─────────────────────────────────────────────────────────────────────────────
class ExtendCallbackFormatTests(unittest.TestCase):
    @staticmethod
    def _listing(type_):
        return SimpleNamespace(id=7, type=type_, city_id=1, category_id=2)

    def test_market_uses_city_and_category_slugs(self):
        cb = lifecycle_worker._extend_callback(
            self._listing("market"), {1: "beograd"}, {2: "gitary"})
        self.assertEqual(cb, "market_extend:7:beograd:gitary:my")

    def test_service_uses_urlencoded_back_cb(self):
        cb = lifecycle_worker._extend_callback(self._listing("service"), {}, {})
        self.assertEqual(cb, "service_extend:7:my_services")

    def test_vacancy_uses_source_city_catid(self):
        cb = lifecycle_worker._extend_callback(self._listing("vacancy"), {}, {})
        self.assertEqual(cb, "vac_extend:7:my:-:0")

    def test_unknown_type_gives_none(self):
        self.assertIsNone(
            lifecycle_worker._extend_callback(self._listing("release"), {}, {}))


class ReminderEscapingTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_is_html_escaped_and_callback_valid(self):
        listing = SimpleNamespace(
            id=7, type="market", city_id=1, category_id=2, owner_id=42,
            title='Гитара <б/у> & "усилок"',
        )
        bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)))
        with (
            patch.object(lifecycle_worker, "get_text",
                         AsyncMock(return_value="«{title}» — {days} дн.")),
            patch.object(lifecycle_worker, "register_bot_message", AsyncMock()),
            patch.object(lifecycle_worker.lc, "days_left", lambda l: 3),
        ):
            sent = await lifecycle_worker._send_reminder(
                bot, listing, {1: "bg"}, {2: "git"})

        self.assertTrue(sent)
        args, kwargs = bot.send_message.await_args
        text = args[1]
        self.assertNotIn("<б/у>", text)
        self.assertIn("&lt;б/у&gt;", text)
        self.assertIn("&amp;", text)
        kb = kwargs["reply_markup"]
        self.assertEqual(
            kb.inline_keyboard[0][0].callback_data, "market_extend:7:bg:git:my")


# ─────────────────────────────────────────────────────────────────────────────
# Отмена редактирования вакансии + маршрут возврата
# ─────────────────────────────────────────────────────────────────────────────
class VacancyEditCancelTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_cancels_state_keeps_search_ctx_and_back_to_search(self):
        state = FakeState(
            {"vac_search_query": "бас", "vac_search_result_ids": [1, 2]},
            current_state="_MainState:waiting_title",
        )
        cb = fake_callback("vacancy_edit_overview:10:search:-:0")
        with (
            patch.object(vacancy_edit_overview, "clear_bot_messages", AsyncMock()),
            patch.object(vacancy_edit_overview, "_authorize_vacancy_callback",
                         AsyncMock(return_value=True)),
            patch.object(vacancy_edit_overview, "_render_overview", AsyncMock()) as render,
            patch("builtins.print"),
        ):
            await vacancy_edit_overview.vacancy_edit_overview_entry(cb, state)

        self.assertIsNone(state.current_state)          # шаг отменён
        self.assertFalse(state.cleared)                 # данные живы
        self.assertEqual(state.values.get("vac_search_query"), "бас")
        self.assertEqual(render.await_args.kwargs.get("back_cb"),
                         "vac_view:10:search")

    async def test_legacy_callback_without_source_defaults_to_my(self):
        state = FakeState()
        cb = fake_callback("vacancy_edit_overview:10")
        with (
            patch.object(vacancy_edit_overview, "clear_bot_messages", AsyncMock()),
            patch.object(vacancy_edit_overview, "_authorize_vacancy_callback",
                         AsyncMock(return_value=True)),
            patch.object(vacancy_edit_overview, "_render_overview", AsyncMock()) as render,
            patch("builtins.print"),
        ):
            await vacancy_edit_overview.vacancy_edit_overview_entry(cb, state)

        self.assertEqual(render.await_args.kwargs.get("back_cb"),
                         "vac_view:10:::my")


class ClosedVacancyBackRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _render(self, status, back_cb):
        listing = SimpleNamespace(id=10, status=status, is_sold=False)
        bundle = (listing, None, None, [], {})
        send = AsyncMock(return_value=SimpleNamespace(message_id=1))
        with (
            patch.object(vacancy_edit_overview, "_load_listing_bundle",
                         AsyncMock(return_value=bundle)),
            patch.object(vacancy_edit_overview, "_build_overview_text",
                         AsyncMock(return_value="text")),
            patch.object(vacancy_edit_overview, "_build_overview_kb",
                         AsyncMock(return_value=None)) as kb,
            patch.object(vacancy_edit_overview, "register_bot_messages", AsyncMock()),
            patch("builtins.print"),
        ):
            await vacancy_edit_overview._render_overview(
                1001, SimpleNamespace(), send, 10, back_cb=back_cb)
        return kb.await_args.kwargs.get("back_cb")

    async def test_closed_vacancy_forces_my_route(self):
        # Закрытая вакансия из поиска: search-маршрут ответил бы «недоступна»
        back = await self._render("closed", "vac_view:10:search")
        self.assertEqual(back, "vac_view:10:::my")

    async def test_active_vacancy_keeps_search_route(self):
        back = await self._render("active", "vac_view:10:search")
        self.assertEqual(back, "vac_view:10:search")


class BackCbFromCtxTests(unittest.TestCase):
    def test_search(self):
        self.assertEqual(
            vacancy_edit_overview._back_cb_from_ctx(5, {"vef_back_src": "search"}),
            "vac_view:5:search")

    def test_catalog_with_city_and_cat(self):
        data = {"vef_back_src": "catalog", "vef_back_city": "beograd",
                "vef_back_cat": "12"}
        self.assertEqual(vacancy_edit_overview._back_cb_from_ctx(5, data),
                         "vac_view:5:beograd:12")

    def test_catalog_without_city_falls_back_to_my(self):
        self.assertEqual(
            vacancy_edit_overview._back_cb_from_ctx(5, {"vef_back_src": "catalog"}),
            "vac_view:5:::my")

    def test_empty_ctx_defaults_to_my(self):
        self.assertEqual(vacancy_edit_overview._back_cb_from_ctx(5, {}),
                         "vac_view:5:::my")


# ─────────────────────────────────────────────────────────────────────────────
# Маршрут после удаления: услуга → «Мои услуги», барахолка → «Мои объявления»
# ─────────────────────────────────────────────────────────────────────────────
class _DelSession:
    def __init__(self, listing):
        self.listing = listing
        self.deleted = None
        self.committed = False

    async def get(self, model, lid):
        return self.listing

    async def delete(self, obj):
        self.deleted = obj

    async def commit(self):
        self.committed = True


class _Ctx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DeleteRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _run_delete(self, listing_type):
        listing = SimpleNamespace(id=7, owner_id=17, type=listing_type)
        session = _DelSession(listing)
        cb = fake_callback("sell_delete_yes:7")
        state = FakeState(
            {"search_results": [7], "search_query": "усилитель"},
            current_state="ServiceSearch:waiting_for_detail",
        )
        with (
            patch.object(market_add, "SessionLocal", lambda: _Ctx(session)),
            patch.object(market_add, "clear_bot_messages", AsyncMock()),
            patch.object(market_add, "register_bot_messages", AsyncMock()),
            patch.object(market_add, "get_text", AsyncMock(return_value=None)),
            patch.object(market_add, "get_common_menu_button",
                         AsyncMock(return_value=None)),
        ):
            await market_add.delete_yes(cb, state)
        self.assertTrue(session.committed)
        # Удаление снимает активный сценарий (иначе следующий текст ушёл бы
        # в поиск), но данные-контекст не стирает.
        self.assertIsNone(state.current_state)
        self.assertFalse(state.cleared)
        # Второе сообщение — навигация после удаления
        nav_kwargs = cb.message.answer.await_args_list[1].kwargs
        return _kb_callbacks(nav_kwargs["reply_markup"])

    async def test_service_delete_leads_to_my_services(self):
        rows = await self._run_delete("service")
        self.assertEqual(rows[0], ["my_services"])
        for row in rows:
            self.assertNotIn("sell_back", row)

    async def test_market_delete_leads_to_my_listings(self):
        rows = await self._run_delete("market")
        self.assertEqual(rows[0], ["my_listings"])
        for row in rows:
            self.assertNotIn("sell_back", row)


# ─────────────────────────────────────────────────────────────────────────────
# Возврат к результатам поиска: та же страница (offset)
# ─────────────────────────────────────────────────────────────────────────────
def _rows(ids):
    return [SimpleNamespace(id=i, title=f"Объявление {i}", city_id=1,
                            category_id=2, price=None) for i in ids]


class MarketSearchOffsetTests(unittest.IsolatedAsyncioTestCase):
    async def test_back_restores_saved_page(self):
        ids = list(range(1, 31))          # 3 страницы по 10
        state = FakeState({"search_results": ids, "search_query": "гитара",
                           "search_offset": 10})
        cb = fake_callback("market_search_results")
        with (
            patch.object(market_view, "clear_bot_messages", AsyncMock()),
            patch.object(market_view, "register_bot_messages", AsyncMock()),
            patch.object(market_view, "_load_public_market_ids",
                         AsyncMock(return_value=(ids, _rows(ids)))),
            patch.object(market_view, "get_text", AsyncMock(return_value="t")),
            patch.object(market_view, "get_common_menu_button",
                         AsyncMock(return_value=None)),
            patch("builtins.print"),
        ):
            await market_view.back_to_search_results_any(cb, state)

        kwargs = cb.message.answer.await_args.kwargs
        rows = _kb_callbacks(kwargs["reply_markup"])
        # Первая кнопка — 11-е объявление (offset=10), не первое
        self.assertEqual(rows[0], ["search_detail:11"])
        pager = rows[10]
        self.assertEqual(pager, ["market_search_page:0", "stub",
                                 "market_search_page:20"])

    async def test_ram_restore_persists_query_and_offset_to_fsm(self):
        # FSM пуст (например, после clear) — контекст восстанавливается из RAM
        # и должен целиком осесть в FSM, иначе следующий возврат потеряет
        # запрос и страницу.
        ids = list(range(1, 31))
        market_view.last_search_ctx_by_chat[1001] = {
            "ids": ids, "query": "гитара", "offset": 10,
        }
        self.addCleanup(market_view.last_search_ctx_by_chat.pop, 1001, None)
        state = FakeState()
        cb = fake_callback("market_search_results")
        with (
            patch.object(market_view, "clear_bot_messages", AsyncMock()),
            patch.object(market_view, "register_bot_messages", AsyncMock()),
            patch.object(market_view, "_load_public_market_ids",
                         AsyncMock(return_value=(ids, _rows(ids)))),
            patch.object(market_view, "get_text", AsyncMock(return_value="t")),
            patch.object(market_view, "get_common_menu_button",
                         AsyncMock(return_value=None)),
            patch("builtins.print"),
        ):
            await market_view.back_to_search_results_any(cb, state)

        self.assertEqual(state.values.get("search_query"), "гитара")
        self.assertEqual(state.values.get("search_offset"), 10)

    async def test_stale_pager_offset_clamped_to_last_page(self):
        # Старая кнопка «»» с offset=40, а результатов осталось 15 → 2 страницы.
        ids = list(range(1, 16))
        state = FakeState({"search_results": ids, "search_query": "гитара"})
        cb = fake_callback("market_search_page:40")
        with (
            patch.object(market_view, "clear_bot_messages", AsyncMock()),
            patch.object(market_view, "register_bot_messages", AsyncMock()),
            patch.object(market_view, "_load_public_market_ids",
                         AsyncMock(return_value=(ids, _rows(ids)))),
            patch.object(market_view, "get_text", AsyncMock(return_value="t")),
            patch("builtins.print"),
        ):
            await market_view.market_search_page(cb, state)

        self.assertEqual(state.values.get("search_offset"), 10)
        rows = _kb_callbacks(cb.message.answer.await_args.kwargs["reply_markup"])
        self.assertEqual(rows[0], ["search_detail:11"])   # последняя страница
        self.assertIn("stub", rows[5])                    # пагинатор «2/2»

    async def test_pager_click_persists_offset_in_fsm(self):
        ids = list(range(1, 31))
        state = FakeState({"search_results": ids, "search_query": "гитара"})
        cb = fake_callback("market_search_page:20")
        with (
            patch.object(market_view, "clear_bot_messages", AsyncMock()),
            patch.object(market_view, "register_bot_messages", AsyncMock()),
            patch.object(market_view, "_load_public_market_ids",
                         AsyncMock(return_value=(ids, _rows(ids)))),
            patch.object(market_view, "get_text", AsyncMock(return_value="t")),
            patch("builtins.print"),
        ):
            await market_view.market_search_page(cb, state)

        self.assertEqual(state.values.get("search_offset"), 20)
        self.assertEqual(
            market_view.last_search_ctx_by_chat[1001].get("offset"), 20)


class ServicesSearchOffsetTests(unittest.IsolatedAsyncioTestCase):
    async def test_back_keeps_ctx_offset_and_opens_same_page(self):
        ids = list(range(1, 31))
        services_view.services_search_ctx_by_chat[1001] = {
            "ids": ids, "query": "барабаны", "offset": 10,
        }
        state = FakeState({"search_results": ids, "search_query": "барабаны",
                           "search_offset": 10})
        cb = fake_callback("services_search_back")
        with (
            patch.object(services_view, "clear_bot_messages", AsyncMock()),
            patch.object(services_view, "register_bot_messages", AsyncMock()),
            patch.object(services_view, "_load_public_service_ids",
                         AsyncMock(return_value=(ids, _rows(ids)))),
            patch.object(services_view, "get_common_menu_button",
                         AsyncMock(return_value=None)),
            patch("builtins.print"),
        ):
            await services_view.services_search_back(cb, state)

        # offset не затёрт перезаписью контекста
        self.assertEqual(
            services_view.services_search_ctx_by_chat[1001].get("offset"), 10)
        rows = _kb_callbacks(cb.message.answer.await_args.kwargs["reply_markup"])
        self.assertEqual(rows[0], ["sv:item:11:1:2:s"])

    async def test_stale_pager_offset_clamped_to_last_page(self):
        ids = list(range(1, 16))
        services_view.services_search_ctx_by_chat[1001] = {
            "ids": ids, "query": "барабаны", "offset": 0,
        }
        state = FakeState()
        cb = fake_callback("services_search_page:40")
        with (
            patch.object(services_view, "clear_bot_messages", AsyncMock()),
            patch.object(services_view, "register_bot_messages", AsyncMock()),
            patch.object(services_view, "_load_public_service_ids",
                         AsyncMock(return_value=(ids, _rows(ids)))),
            patch.object(services_view, "get_common_menu_button",
                         AsyncMock(return_value=None)),
            patch("builtins.print"),
        ):
            await services_view.services_search_page(cb, state)

        self.assertEqual(state.values.get("search_offset"), 10)
        rows = _kb_callbacks(cb.message.answer.await_args.kwargs["reply_markup"])
        self.assertEqual(rows[0], ["sv:item:11:1:2:s"])

    def tearDown(self):
        services_view.services_search_ctx_by_chat.pop(1001, None)


# ─────────────────────────────────────────────────────────────────────────────
# FSM: счётчик подряд идущих сбоев записи + восстановление
# ─────────────────────────────────────────────────────────────────────────────
class _BrokenFactory:
    def __call__(self):
        raise RuntimeError("db down")


class FsmWriteFailureCounterTests(unittest.IsolatedAsyncioTestCase):
    async def test_consecutive_failures_counted_and_reset_on_success(self):
        storage = SQLiteFsmStorage(session_factory=_BrokenFactory())
        key = StorageKey(bot_id=1, chat_id=1, user_id=1)

        with self.assertLogs("app.fsm", level="ERROR") as logs:
            await storage.set_state(key, "Wizard:step1")
            await storage.set_data(key, {"a": 1})
            await storage.set_state(key, "Wizard:step2")

        self.assertEqual(storage._write_failures, 3)
        self.assertIn("сбой #3", logs.output[-1])
        self.assertIn("НЕ ПЕРЕЖИВУТ РЕСТАРТ", logs.output[-1])
        # Кэш при этом продолжает работать (осознанный компромисс)
        self.assertEqual(await storage.get_state(key), "Wizard:step2")

        # Восстановление: рабочая БД → счётчик сбрасывается с warning
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(tmp.name) / 'fsm_test.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        self.addAsyncCleanup(engine.dispose)
        storage._session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False)

        with self.assertLogs("app.fsm", level="WARNING") as logs:
            await storage.set_state(key, "Wizard:step3")
        self.assertEqual(storage._write_failures, 0)
        self.assertIn("восстановилась после 3", logs.output[0])


# ─────────────────────────────────────────────────────────────────────────────
# ADMIN_IDS: единый источник + уведомление админа из feedback
# ─────────────────────────────────────────────────────────────────────────────
class AdminIdsSingleSourceTests(unittest.TestCase):
    def test_admin_panel_and_feedback_share_same_list_object(self):
        self.assertIs(admin_panel.ADMIN_IDS, admin_ids.ADMIN_IDS)
        self.assertIs(feedback.ADMIN_IDS, admin_ids.ADMIN_IDS)


class FeedbackNotifiesAdminTests(unittest.IsolatedAsyncioTestCase):
    async def test_receive_saves_and_notifies_every_admin(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            from_user=SimpleNamespace(id=17, username="tester"),
            text="Сломалась кнопка",
            delete=AsyncMock(),
            bot=SimpleNamespace(send_message=AsyncMock()),
        )
        state = FakeState()
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with (
            patch.object(feedback, "ADMIN_IDS", [111, 222]),
            patch("app.database.SessionLocal", lambda: _Ctx(session)),
            patch("app.routers.utils.clear_bot_messages", AsyncMock()),
            patch("app.routers.utils.register_bot_messages", AsyncMock()),
            patch("app.keyboards.get_common_menu_button", AsyncMock(return_value=None)),
            patch("builtins.print"),
        ):
            await feedback.feedback_receive(message, state)

        self.assertTrue(session.execute.await_args is not None)  # сохранено в БД
        calls = message.bot.send_message.await_args_list
        # Первые два вызова — уведомления админам, последний — ответ юзеру.
        self.assertEqual([c.args[0] for c in calls[:2]], [111, 222])
        admin_call_text = calls[0].args[1]
        self.assertIn("Сломалась кнопка", admin_call_text)
        self.assertIn("tester", admin_call_text)

    async def test_one_admin_send_failure_does_not_block_the_other(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            from_user=SimpleNamespace(id=17, username=None),
            text="привет",
            delete=AsyncMock(),
            bot=SimpleNamespace(
                send_message=AsyncMock(side_effect=[
                    Exception("blocked"), None, SimpleNamespace(message_id=999),
                ])),
        )
        state = FakeState()
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with (
            patch.object(feedback, "ADMIN_IDS", [111, 222]),
            patch("app.database.SessionLocal", lambda: _Ctx(session)),
            patch("app.routers.utils.clear_bot_messages", AsyncMock()),
            patch("app.routers.utils.register_bot_messages", AsyncMock()),
            patch("app.keyboards.get_common_menu_button", AsyncMock(return_value=None)),
            patch("builtins.print"),
        ):
            await feedback.feedback_receive(message, state)

        # Второй админ всё равно получил уведомление, а пользователю ушёл ответ.
        self.assertEqual(message.bot.send_message.await_count, 3)  # 2 админа + ответ юзеру


# ─────────────────────────────────────────────────────────────────────────────
# init_db: миграции глотают только «колонка уже есть», прочее — наружу
# ─────────────────────────────────────────────────────────────────────────────
class InitDbDuplicateColumnGuardTests(unittest.TestCase):
    def test_duplicate_column_recognised(self):
        from app.database import _is_duplicate_column
        self.assertTrue(_is_duplicate_column(
            Exception("(sqlite3.OperationalError) duplicate column name: first_seen")))

    def test_lock_and_disk_errors_not_swallowed(self):
        from app.database import _is_duplicate_column
        self.assertFalse(_is_duplicate_column(Exception("database is locked")))
        self.assertFalse(_is_duplicate_column(Exception("disk I/O error")))
        self.assertFalse(_is_duplicate_column(Exception("no such table: artist")))


if __name__ == "__main__":
    unittest.main()
