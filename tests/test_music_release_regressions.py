import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.routers import artists, releases


def _release_objects(*, meta_status="published", artist_status="active", listing_status="active"):
    listing = SimpleNamespace(
        id=101,
        type="release",
        status=listing_status,
        owner_id=10,
        title="A < B",
        descr="описание & детали",
        photo_file_id="cover",
    )
    artist = SimpleNamespace(
        id=201,
        status=artist_status,
        owner_user_id=10,
        name="<Группа>",
    )
    meta = SimpleNamespace(
        artist_id=artist.id,
        status=meta_status,
        release_type="single",
        release_date="17.07.2026",
        genre="rock < pop",
        recorded_at="A&B",
        links='[{"label":"Сайт","url":"https://example.com/music"}]',
        video_file_id="video-id",
    )
    tracks = [SimpleNamespace(id=301, position=1, title="<Трек>", file_id="audio-id")]
    return listing, meta, artist, tracks


class FakeMessage:
    def __init__(self):
        self.chat = SimpleNamespace(id=777)
        self.deleted = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def edit_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeUserMessage(FakeMessage):
    def __init__(self, text):
        super().__init__()
        self.text = text
        self.from_user = SimpleNamespace(id=55, username="reporter")
        self.bot = SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=9001))
        )
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))


class FakeCallback:
    def __init__(self, data):
        self.data = data
        self.from_user = SimpleNamespace(id=55, username="reporter")
        self.message = FakeMessage()
        self.bot = SimpleNamespace()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


class FakeState:
    def __init__(self, data=None):
        self.cleared = False
        self.values = dict(data or {})

    async def get_data(self):
        return dict(self.values)

    async def clear(self):
        self.cleared = True

    async def set_state(self, value):
        self.values["state"] = value

    async def update_data(self, **kwargs):
        self.values.update(kwargs)


class MusicHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_feature_gate_is_inner_and_cannot_swallow_unrelated_updates(self):
        self.assertEqual(len(releases.router.callback_query.outer_middleware), 0)
        self.assertGreaterEqual(len(releases.router.callback_query.middleware), 1)
        self.assertEqual(len(artists.router.callback_query.outer_middleware), 0)
        self.assertGreaterEqual(len(artists.router.callback_query.middleware), 1)

    def test_urls_require_real_http_or_https_url(self):
        self.assertEqual(
            releases._normalize_http_url("https://example.com/a?b=1"),
            "https://example.com/a?b=1",
        )
        for bad in (
            "httpNOPE",
            "javascript:alert(1)",
            "https://",
            "https://user:pass@example.com",
            "https://example.com/space here",
            'https://example.com/"onfocus=alert(1)',
            "https://example.com/\\windows-path",
            "https://example.com/\x00control",
            "ftp://example.com/file",
        ):
            self.assertIsNone(releases._normalize_http_url(bad), bad)

    def test_link_parser_deduplicates_and_limits(self):
        text = " ".join(["https://example.com"] * 2 + [f"https://e{i}.com" for i in range(20)])
        links = releases._parse_link_text(text)
        self.assertEqual(len(links), releases.MAX_LINKS)
        self.assertEqual(len({link["url"] for link in links}), len(links))

    async def test_caption_escapes_user_content_without_cutting_tags(self):
        listing, meta, artist, tracks = _release_objects()
        listing.descr = "<b>не тег</b> " * 200
        caption = await releases._release_caption(listing, meta, artist, tracks)
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("&lt;Группа&gt;", caption)
        self.assertIn("A &lt; B", caption)
        self.assertNotIn("<b>не тег</b>", caption)
        self.assertEqual(caption.count("<b>"), caption.count("</b>"))

    async def test_hidden_release_has_no_playback_or_external_links(self):
        listing, meta, artist, tracks = _release_objects(meta_status="hidden")
        keyboard = await releases._release_kb(
            listing,
            meta,
            tracks,
            viewer_id=listing.owner_id,
            is_admin_user=False,
            artist=artist,
        )
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        callbacks = {button.callback_data for button in buttons if button.callback_data}
        self.assertFalse(any(value.startswith("rel:listen:") for value in callbacks))
        self.assertFalse(any(value.startswith("rel:video:") for value in callbacks))
        self.assertFalse(any(value.startswith("rel:report:") for value in callbacks))
        self.assertFalse(any(button.url for button in buttons))

    def test_public_release_must_have_media(self):
        listing, meta, artist, tracks = _release_objects()
        self.assertTrue(releases._release_is_public(listing, meta, artist, tracks))
        meta.video_file_id = None
        meta.links = None
        self.assertFalse(releases._release_is_public(listing, meta, artist, []))

    async def test_artist_search_source_survives_release_round_trip(self):
        self.assertEqual(releases._clean_release_source("a201.s"), "a201.s")
        self.assertEqual(
            await releases._release_back("a201.s"),
            ("art:view:201:search", "⬅️ К исполнителю"),
        )


class ReleaseReportRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_screen_uses_only_first_cover_from_legacy_csv(self):
        bot = SimpleNamespace(
            send_photo=AsyncMock(return_value=SimpleNamespace(message_id=42)),
            send_message=AsyncMock(),
        )
        with (
            patch.object(releases, "clear_bot_messages", AsyncMock()),
            patch.object(releases, "register_bot_messages", AsyncMock()),
        ):
            await releases._send_screen(
                bot,
                777,
                "card",
                photo="cover-1,cover-2",
            )

        self.assertEqual(bot.send_photo.await_args.args[1], "cover-1")
        bot.send_message.assert_not_awaited()

    async def test_feature_middleware_blocks_stale_direct_callback(self):
        callback = FakeCallback("rel:view:101")
        handler = AsyncMock()
        middleware = releases._MusicEnabledMiddleware()
        with (
            patch.object(releases, "is_enabled", AsyncMock(return_value=False)),
            patch.object(releases, "CallbackQuery", FakeCallback),
        ):
            await middleware(handler, callback, {})

        handler.assert_not_awaited()
        self.assertEqual(callback.answers[-1][0], "Раздел временно недоступен.")

    async def test_publish_db_failure_resets_double_click_guard(self):
        callback = FakeCallback("rel:pub")
        state = FakeState({
            "title": "Релиз",
            "cover": "cover-id",
            "artist_id": 201,
            "rel_type": "single",
            "links": [{"label": "Сайт", "url": "https://example.com"}],
            "tracks": [],
            "video": None,
        })
        with (
            patch.object(releases, "_release_city_id", AsyncMock(return_value=1)),
            patch.object(
                releases,
                "_owned_active_artist",
                AsyncMock(return_value=SimpleNamespace(id=201)),
            ),
            patch.object(releases, "_ensure_release_category", AsyncMock(return_value=2)),
            patch.object(releases, "_persist_release", AsyncMock(side_effect=RuntimeError("db"))),
            patch.object(releases, "_replace_prompt", AsyncMock()) as prompt,
            patch("builtins.print"),
        ):
            await releases.publish(callback, state)

        self.assertFalse(state.values["rel_publishing"])
        prompt.assert_awaited_once()
        self.assertFalse(state.cleared)

    async def test_parallel_publish_callbacks_persist_only_once(self):
        first = FakeCallback("rel:pub")
        second = FakeCallback("rel:pub")
        state = FakeState({
            "title": "Релиз",
            "cover": "cover-id",
            "artist_id": 201,
            "rel_type": "single",
            "links": [{"label": "Сайт", "url": "https://example.com"}],
            "tracks": [],
            "video": None,
        })
        persist_started = asyncio.Event()
        finish_persist = asyncio.Event()

        async def slow_failed_persist(*args, **kwargs):
            persist_started.set()
            await finish_persist.wait()
            raise RuntimeError("db")

        persist = AsyncMock(side_effect=slow_failed_persist)
        with (
            patch.object(releases, "_release_city_id", AsyncMock(return_value=1)),
            patch.object(
                releases,
                "_owned_active_artist",
                AsyncMock(return_value=SimpleNamespace(id=201)),
            ),
            patch.object(releases, "_ensure_release_category", AsyncMock(return_value=2)),
            patch.object(releases, "_persist_release", persist),
            patch.object(releases, "_replace_prompt", AsyncMock()),
            patch("builtins.print"),
        ):
            first_task = asyncio.create_task(releases.publish(first, state))
            await persist_started.wait()
            await releases.publish(second, state)
            finish_persist.set()
            await first_task

        persist.assert_awaited_once()
        self.assertEqual(second.answers[-1][0], "Публикуем, пожалуйста, подождите.")

    async def test_preset_reason_notifies_and_finishes(self):
        callback = FakeCallback("rel:repdo:101:spam")
        state = FakeState()
        objects = _release_objects()
        notify = AsyncMock()
        with (
            patch.object(releases, "_load_release", AsyncMock(return_value=objects)),
            patch.object(releases, "_notify_report", notify),
        ):
            await releases.release_report_send(callback, state)

        notify.assert_awaited_once()
        self.assertTrue(state.cleared)
        self.assertTrue(callback.message.deleted)
        self.assertEqual(callback.answers[-1][0], "Жалоба отправлена. Спасибо!")

    async def test_other_reason_prompt_and_text_submission(self):
        callback = FakeCallback("rel:repdo:101:other")
        state = FakeState()
        objects = _release_objects()
        notify = AsyncMock()
        with (
            patch.object(releases, "_load_release", AsyncMock(return_value=objects)),
            patch.object(releases, "_notify_report", notify),
        ):
            await releases.release_report_send(callback, state)

        self.assertEqual(state.values["report_listing_id"], 101)
        self.assertEqual(len(callback.message.edits), 1)
        notify.assert_not_awaited()

        message = FakeUserMessage("причина <script>")
        register = AsyncMock()
        with (
            patch.object(releases, "_load_release", AsyncMock(return_value=objects)),
            patch.object(releases, "_notify_report", notify),
            patch.object(releases, "register_bot_messages", register),
        ):
            await releases.release_report_other(message, state)

        notify.assert_awaited_once()
        self.assertEqual(notify.await_args.args[-1], "Другое: причина <script>")
        self.assertTrue(state.cleared)
        register.assert_awaited_once()

    async def test_back_from_other_only_restores_reason_menu(self):
        callback = FakeCallback("rel:repback:101")
        state = FakeState()
        await releases.release_report_back(callback, state)

        self.assertTrue(state.cleared)
        self.assertEqual(len(callback.message.edits), 1)
        self.assertIn("Что не так", callback.message.edits[0][0])
        self.assertFalse(callback.message.deleted)


if __name__ == "__main__":
    unittest.main()
