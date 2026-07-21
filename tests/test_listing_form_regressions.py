import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models import Category
from app.routers import (
    market_add,
    market_edit_photos,
    services_add,
    services_edit_overview,
    services_edit_photos,
    user_extra_fields,
    vacancy_add,
    vacancy_edit,
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


def fake_callback(data, user_id=17):
    message = SimpleNamespace(
        chat=SimpleNamespace(id=1001),
        bot=SimpleNamespace(),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, username="tester"),
        message=message,
        bot=message.bot,
        answer=AsyncMock(),
    )


class FakeSessionContext:
    async def __aenter__(self):
        return SimpleNamespace()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        if self.value is None:
            raise RuntimeError("expected one result")
        return self.value


class FakeQueuedSession:
    def __init__(self, *results):
        self.results = list(results)
        self.committed = False

    async def execute(self, _statement):
        return FakeResult(self.results.pop(0))

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class ListingFormHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_extra_flex_parser_accepts_only_objects(self):
        self.assertEqual(user_extra_fields._flex_dict('{"skill": "Python"}'), {"skill": "Python"})
        self.assertEqual(user_extra_fields._flex_dict('["not", "an", "object"]'), {})
        self.assertEqual(user_extra_fields._flex_dict('{broken'), {})

    def test_service_extra_category_flag_supports_list_schema(self):
        category = Category(
            id=81,
            slug="service-test",
            name="Service test",
            fields=json.dumps([
                {
                    "type": "__meta",
                    "key": "allow_extra_categories",
                    "value": True,
                }
            ]),
        )
        self.assertTrue(services_edit_overview._allow_extra_for_category(category))

    async def test_vacancy_edit_overview_escapes_user_and_category_text(self):
        text = await vacancy_edit_overview._build_overview_text(
            SimpleNamespace(title="<b>title</b>", descr="A&B", price="<100>"),
            SimpleNamespace(name="Novi <Sad>"),
            SimpleNamespace(name="<Category>"),
            [{"type": "text", "key": "skill", "label": "<Skill>"}],
            {"skill": "C++ & <Python>"},
            "Parent < Child",
        )
        self.assertNotIn("<b>title</b>", text)
        self.assertIn("&lt;b&gt;title&lt;/b&gt;", text)
        self.assertIn("Novi &lt;Sad&gt;", text)
        self.assertIn("Parent &lt; Child", text)
        self.assertIn("&lt;Skill&gt;", text)

    def test_service_video_url_rejects_credentials_and_html_breakout(self):
        self.assertTrue(services_edit_overview._valid_http_url("https://example.com/video"))
        for bad in (
            "javascript:alert(1)",
            "https://",
            "https://user:pass@example.com/video",
            'https://example.com/"onerror=alert(1)',
            "https://example.com/space here",
            "https://example.com/\\path",
        ):
            self.assertFalse(services_edit_overview._valid_http_url(bad), bad)


class ListingFormAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        services_add.media_group_tasks.clear()
        services_add.media_group_wait_msg.clear()
        services_add._service_publish_locks.clear()
        market_add.media_group_tasks.clear()
        market_add.media_group_wait_msg.clear()
        vacancy_add._vacancy_publish_locks.clear()

    async def test_manual_service_price_advances_to_photo_state(self):
        state = FakeState(current_state=services_add.ServiceForm.price.state)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=17),
            text="2500 RSD",
            delete=AsyncMock(),
        )
        with (
            patch.object(services_add, "clear_bot_messages", AsyncMock()),
            patch.object(services_add, "_send_photo_prompt", AsyncMock()) as prompt,
            patch("builtins.print"),
        ):
            await services_add.service_price_set(message, state)

        self.assertEqual(state.current_state, services_add.ServiceForm.photo.state)
        self.assertEqual(state.values["price"], "2500 RSD")
        prompt.assert_awaited_once()

    async def test_extra_wizard_preserves_existing_values_when_started(self):
        listing = SimpleNamespace(
            id=10,
            owner_id=17,
            type="market",
            category_id=81,
            flex=json.dumps({"skill": "Python", "level": "senior"}),
        )
        category = SimpleNamespace(
            id=81,
            fields=json.dumps([{"type": "text", "key": "skill", "label": "Навык"}]),
        )
        session = FakeQueuedSession(listing, category)
        state = FakeState({"listing_id": 10, "edit_listing_id": 10})
        event = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=17),
            answer=AsyncMock(),
        )
        with (
            patch.object(user_extra_fields, "SessionLocal", return_value=session),
            patch.object(user_extra_fields, "clear_bot_messages", AsyncMock()),
            patch.object(user_extra_fields, "_ask_current_field", AsyncMock()) as ask,
            patch("builtins.print"),
        ):
            await user_extra_fields.start_extra_fields_for_category(
                event,
                state,
                cat_id=999,
                resume_data="listing:10:city:category:my",
            )

        self.assertEqual(
            state.values[user_extra_fields.VAL_KEY],
            {"skill": "Python", "level": "senior"},
        )
        self.assertEqual(state.values[user_extra_fields.LISTING_TYPE_KEY], "market")
        ask.assert_awaited_once()

    async def test_video_at_service_photo_step_gets_explicit_error(self):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            from_user=SimpleNamespace(id=17),
            photo=None,
            video=SimpleNamespace(file_id="video"),
            answer=AsyncMock(),
        )
        with (
            patch.object(services_add, "get_common_menu_button", AsyncMock(return_value=None)),
            patch.object(services_add, "get_text", AsyncMock(return_value=None)),
            patch("builtins.print"),
        ):
            await services_add.service_not_photo(message, FakeState())

        message.answer.assert_awaited_once()
        self.assertIn("только фото", message.answer.await_args.args[0])

    async def test_three_single_service_photos_reach_confirmation(self):
        state = FakeState(
            {"photos": []},
            current_state=services_add.ServiceForm.photo.state,
        )
        with (
            patch.object(services_add, "delete_photo_prompts", AsyncMock()),
            patch.object(services_add, "_send_photo_prompt", AsyncMock()),
            patch.object(services_add, "_preview_and_confirm", AsyncMock()) as preview,
            patch("builtins.print"),
        ):
            for index in range(3):
                message = SimpleNamespace(
                    chat=SimpleNamespace(id=1001),
                    bot=SimpleNamespace(),
                    from_user=SimpleNamespace(id=17),
                    message_id=index + 1,
                    media_group_id=None,
                    photo=[SimpleNamespace(file_id=f"photo-{index}")],
                    delete=AsyncMock(),
                )
                await services_add.service_photo(message, state)

        self.assertEqual(state.values["photos"], ["photo-0", "photo-1", "photo-2"])
        self.assertEqual(state.current_state, services_add.ServiceForm.confirm.state)
        preview.assert_awaited_once()

    async def test_service_album_of_three_reaches_confirmation(self):
        key = (1001, "album")
        # Фото альбома уже записаны в FSM по прибытии (см. service_photo) —
        # finalize_album их только читает, не мержит из отдельного кэша.
        state = FakeState(
            {"photos": ["photo-0", "photo-1", "photo-2"]},
            current_state=services_add.ServiceForm.photo.state,
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(delete_message=AsyncMock()),
            from_user=SimpleNamespace(id=17),
        )
        with (
            patch.object(services_add.asyncio, "sleep", AsyncMock()),
            patch.object(services_add, "delete_photo_prompts", AsyncMock()),
            patch.object(services_add, "_preview_and_confirm", AsyncMock()) as preview,
            patch("builtins.print"),
        ):
            await services_add._finalize_album(message, state, key)

        self.assertEqual(state.values["photos"], ["photo-0", "photo-1", "photo-2"])
        self.assertEqual(state.current_state, services_add.ServiceForm.confirm.state)
        preview.assert_awaited_once()

    async def test_service_album_photo_persists_to_fsm_immediately(self):
        """Фото альбома должно попасть в FSM (БД) сразу по прибытии, а не только
        после debounce-финализации — иначе рестарт бота в этом окне теряет
        уже присланные фото (см. CLAUDE.md, «потеря альбома фото»)."""
        key = (1001, "live-service-album")
        state = FakeState(
            {"photos": []},
            current_state=services_add.ServiceForm.photo.state,
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=17),
            message_id=1,
            media_group_id="live-service-album",
            photo=[SimpleNamespace(file_id="photo-0")],
            answer=AsyncMock(return_value=SimpleNamespace(message_id=999)),
            delete=AsyncMock(),
        )
        with (
            patch.object(services_add, "get_text", AsyncMock(return_value=None)),
            patch.object(services_add, "_finalize_album", AsyncMock()),
            patch("builtins.print"),
        ):
            await services_add.service_photo(message, state)
            task = services_add.media_group_tasks.get(key)
            if task:
                await task

        self.assertEqual(state.values["photos"], ["photo-0"])

    async def test_market_album_photo_persists_to_fsm_immediately(self):
        key = (1001, "live-market-album")
        state = FakeState(
            {"photos": []},
            current_state=market_add.Sell.photo.state,
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=17),
            message_id=1,
            media_group_id="live-market-album",
            photo=[SimpleNamespace(file_id="photo-0")],
            answer=AsyncMock(return_value=SimpleNamespace(message_id=999)),
            delete=AsyncMock(),
        )
        with (
            patch.object(market_add, "get_text", AsyncMock(return_value=None)),
            patch.object(market_add, "finalize_album", AsyncMock()),
            patch("builtins.print"),
        ):
            await market_add.sell_photo(message, state)
            task = market_add.media_group_tasks.get(key)
            if task:
                await task

        self.assertEqual(state.values["photos"], ["photo-0"])

    async def test_album_cleanup_is_isolated_by_chat(self):
        services_add.media_group_wait_msg[(1, "album-a")] = 11
        services_add.media_group_wait_msg[(2, "album-b")] = 22

        await services_add._clear_album_cache(1)

        self.assertNotIn((1, "album-a"), services_add.media_group_wait_msg)
        self.assertEqual(services_add.media_group_wait_msg[(2, "album-b")], 22)

    async def test_service_album_cannot_resurrect_cleared_form(self):
        key = (1001, "stale-service-album")
        services_add.media_group_wait_msg[key] = 55
        state = FakeState(current_state=None)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(delete_message=AsyncMock()),
            from_user=SimpleNamespace(id=17),
        )
        with (
            patch.object(services_add.asyncio, "sleep", AsyncMock()),
            patch.object(services_add, "_preview_and_confirm", AsyncMock()) as preview,
            patch.object(services_add, "_send_photo_prompt", AsyncMock()) as prompt,
        ):
            await services_add._finalize_album(message, state, key)

        self.assertEqual(state.values, {})
        self.assertNotIn(key, services_add.media_group_wait_msg)
        preview.assert_not_awaited()
        prompt.assert_not_awaited()

    async def test_market_album_cleanup_is_per_chat_and_stale_safe(self):
        stale = (1001, "stale-market-album")
        other = (2002, "other-market-album")
        market_add.media_group_wait_msg[stale] = 77
        market_add.media_group_wait_msg[other] = 88
        state = FakeState(current_state=None)
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(delete_message=AsyncMock()),
            from_user=SimpleNamespace(id=17),
        )
        with (
            patch.object(market_add.asyncio, "sleep", AsyncMock()),
            patch.object(market_add, "preview_and_confirm", AsyncMock()) as preview,
            patch.object(market_add, "send_photo_prompt", AsyncMock()) as prompt,
        ):
            await market_add.finalize_album(message, state, stale)

        self.assertEqual(state.values, {})
        self.assertNotIn(stale, market_add.media_group_wait_msg)
        self.assertEqual(market_add.media_group_wait_msg[other], 88)
        preview.assert_not_awaited()
        prompt.assert_not_awaited()

    async def test_service_publish_lock_rejects_parallel_callback(self):
        first = fake_callback("sell_ok")
        second = fake_callback("sell_ok")
        state = FakeState(current_state=services_add.ServiceForm.confirm.state)
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_publish(*args):
            started.set()
            await finish.wait()

        with patch.object(services_add, "_service_ok_locked", AsyncMock(side_effect=slow_publish)) as publish:
            task = asyncio.create_task(services_add.service_ok(first, state))
            await started.wait()
            await services_add.service_ok(second, state)
            finish.set()
            await task

        publish.assert_awaited_once()
        self.assertEqual(second.answer.await_args.args[0], "Публикуем, пожалуйста, подождите.")

    async def test_service_edit_back_opens_owner_card(self):
        listing = SimpleNamespace(
            id=10,
            city_id=1,
            category_id=81,
            title="Услуга",
            descr="Описание",
            price="100",
            photo_file_id=None,
            flex=None,
            extra_category_id1=None,
            extra_category_id2=None,
        )
        answer = AsyncMock(return_value=SimpleNamespace(message_id=501))
        with (
            patch.object(services_edit_overview, "clear_bot_messages", AsyncMock()),
            patch.object(services_edit_overview, "_clear_user_inputs", AsyncMock()),
            patch.object(
                services_edit_overview,
                "_load_listing_bundle",
                AsyncMock(return_value=(
                    listing,
                    SimpleNamespace(name="Белград"),
                    SimpleNamespace(name="Категория", fields="[]"),
                    [],
                    {},
                )),
            ),
            patch.object(services_edit_overview, "register_bot_messages", AsyncMock()),
            patch("builtins.print"),
        ):
            await services_edit_overview._render_overview(
                1001,
                SimpleNamespace(),
                answer,
                listing.id,
            )

        keyboard = answer.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("sv:item:10:1:81:m", callbacks)

    async def test_stale_service_photo_apply_cannot_use_other_draft(self):
        callback = fake_callback("sphoto:apply:10")
        state = FakeState({
            "sphoto_listing_id": 20,
            "sphoto_pending_action": "delete",
            "sphoto_pending_index": 0,
        })
        with patch.object(services_edit_photos, "_get_listing", AsyncMock()) as get_listing:
            await services_edit_photos.sphoto_apply(callback, state)

        get_listing.assert_not_awaited()
        self.assertIn("устарел", callback.answer.await_args.args[0])

    async def test_stale_market_photo_cancel_does_not_render_other_draft(self):
        callback = fake_callback("mphoto:cancel:10")
        state = FakeState({"mphoto_listing_id": 20, "mphoto_draft_ids": ["photo-b"]})
        with (
            patch.object(market_edit_photos, "_authorize_photo_edit", AsyncMock(return_value=object())),
            patch.object(market_edit_photos, "_render_photo_editor", AsyncMock()) as render,
        ):
            await market_edit_photos.mphoto_cancel(callback, state)

        render.assert_not_awaited()
        self.assertIn("устарел", callback.answer.await_args.args[0])

    async def test_stale_vacancy_price_callback_does_not_revive_form(self):
        callback = fake_callback("vac_price_choice:free")
        state = FakeState(current_state=None)

        await vacancy_add.vacancy_price_choice(callback, state)

        callback.message.delete.assert_not_awaited()
        self.assertFalse(state.values)
        self.assertIn("завершён", callback.answer.await_args.args[0])

    async def test_vacancy_publish_lock_rejects_parallel_price_message(self):
        state = FakeState(current_state=vacancy_add.VacForm.price.state)
        first = SimpleNamespace(
            from_user=SimpleNamespace(id=17),
            answer=AsyncMock(),
        )
        second = SimpleNamespace(
            from_user=SimpleNamespace(id=17),
            answer=AsyncMock(),
        )
        started = asyncio.Event()
        finish = asyncio.Event()

        async def slow_publish(*args):
            started.set()
            await finish.wait()

        with patch.object(
            vacancy_add,
            "_vacancy_input_price_locked",
            AsyncMock(side_effect=slow_publish),
        ) as publish:
            task = asyncio.create_task(vacancy_add.vacancy_input_price(first, state))
            await started.wait()
            await vacancy_add.vacancy_input_price(second, state)
            finish.set()
            await task

        publish.assert_awaited_once()
        self.assertEqual(second.answer.await_args.args[0], "Публикуем, пожалуйста, подождите.")

    async def test_legacy_vacancy_entry_checks_owner_and_type_before_render(self):
        callback = fake_callback("edit_vacancy_overview:10")
        state = FakeState()
        with (
            patch.object(vacancy_edit, "clear_bot_messages", AsyncMock()),
            patch.object(vacancy_edit, "_authorize_vacancy_callback", AsyncMock(return_value=False)),
            patch.object(vacancy_edit, "_render_overview", AsyncMock()) as render,
        ):
            await vacancy_edit.vacancy_edit_overview_entry(callback, state)

        render.assert_not_awaited()

    async def test_vacancy_fsm_save_rejects_owned_non_vacancy_listing(self):
        state = FakeState(
            {"vef_listing_id": 10, "vac_search_query": "гитарист"},
            current_state="_MainState:waiting_title",
        )
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1001),
            bot=SimpleNamespace(),
            from_user=SimpleNamespace(id=17),
            text="Новый заголовок",
            delete=AsyncMock(),
            answer=AsyncMock(),
        )
        with (
            patch.object(vacancy_edit_overview, "clear_bot_messages", AsyncMock()),
            patch.object(vacancy_edit_overview, "SessionLocal", return_value=FakeSessionContext()),
            patch.object(vacancy_edit_overview, "_owned_vacancy_in_session", AsyncMock(return_value=None)),
            patch.object(vacancy_edit_overview, "_render_overview", AsyncMock()) as render,
            patch("builtins.print"),
        ):
            await vacancy_edit_overview.vef_main_title_save(message, state)

        # Активный шаг снят, но данные (контекст поиска) не стёрты:
        # это контракт «отмены» редактирования вакансии.
        self.assertIsNone(state.current_state)
        self.assertFalse(state.cleared)
        self.assertEqual(state.values.get("vac_search_query"), "гитарист")
        render.assert_not_awaited()
        self.assertIn("Недостаточно прав", message.answer.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
