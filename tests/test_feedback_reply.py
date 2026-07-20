"""Обратная связь: ответ администратора через бота (Р-15).

Закрепляет:
- «Нужен ответ»/«Ответ не нужен» редактируют ОДНО уведомление админа
  (без размножения), с fallback-созданием, если сохранённого нет;
- уведомление несёт кнопки «Ответить» и «Убрать»;
- ответ администратора уходит пользователю: сначала его вопрос, потом ответ;
- доставка метит answered_at; недоставка — нет;
- «Убрать» удаляет уведомление.

Все пользовательские тексты — на «Вы».
"""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.routers import feedback
from app.routers.feedback import AdminReplyStates


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


class _Ctx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _MultiPatch:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.__enter__()
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.__exit__(*exc)
        return False


def _fake_cb(data, user_id=17, username="tester", chat_id=1001):
    bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=555)),
        edit_message_text=AsyncMock(),
        delete_message=AsyncMock(),
    )
    message = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id), bot=bot, delete=AsyncMock(),
        edit_reply_markup=AsyncMock())
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, username=username),
        message=message,
        bot=bot,
        answer=AsyncMock(),
    )


def _patches(session):
    return (
        patch.object(feedback, "ADMIN_IDS", [111, 222]),
        patch.object(feedback, "SessionLocal", lambda: _Ctx(session)),
        patch.object(feedback, "clear_bot_messages", AsyncMock()),
        patch.object(feedback, "register_bot_messages", AsyncMock()),
        patch.object(feedback, "get_common_menu_button", AsyncMock(return_value=None)),
        patch("builtins.print"),
    )


def _sent_to(bot):
    return [c.args[0] for c in bot.send_message.await_args_list]


class FbBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        feedback._fb_admin_notifs.clear()
        feedback._admin_reply_target.clear()

    def _prime_notif(self, fb_id, who="@tester", user_id=17, body="текст"):
        feedback._fb_admin_notifs[fb_id] = {
            "who": who, "user_id": user_id, "body": body,
            "msgs": [(111, 900), (222, 901)],
        }


class FbNeedTests(FbBase):
    async def test_edits_existing_notif_no_new_message(self):
        self._prime_notif(42)
        cb = _fake_cb("fb:need:42")
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_need(cb)
        # обращение помечено needs_reply
        self.assertTrue(session.execute.await_args is not None)
        # уведомление отредактировано (2 админских сообщения), новых админам нет
        self.assertEqual(cb.bot.edit_message_text.await_count, 2)
        edited_text = cb.bot.edit_message_text.await_args_list[0].args[0]
        self.assertIn("запросил ответ", edited_text)
        self.assertEqual(_sent_to(cb.bot), [1001])  # только заметка пользователю

    async def test_fallback_creates_when_no_saved_notif(self):
        cb = _fake_cb("fb:need:7")
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_need(cb)
        sent = _sent_to(cb.bot)
        self.assertIn(1001, sent)   # заметка пользователю
        self.assertIn(111, sent)    # компактное уведомление админам
        self.assertIn(222, sent)
        admin_call = next(c for c in cb.bot.send_message.await_args_list if c.args[0] == 111)
        self.assertEqual(admin_call.kwargs["reply_markup"].inline_keyboard[0][0].callback_data, "fb:reply:7")

    async def test_user_note_formal_vy(self):
        cb = _fake_cb("fb:need:5")
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_need(cb)
        note = next(c for c in cb.bot.send_message.await_args_list if c.args[0] == 1001).args[1]
        self.assertIn("Вам", note)


class FbNoNeedTests(FbBase):
    async def test_edits_notif_to_not_required(self):
        self._prime_notif(42)
        cb = _fake_cb("fb:noneed:42")
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_noneed(cb)
        self.assertEqual(cb.bot.edit_message_text.await_count, 2)
        self.assertIn("не требуется", cb.bot.edit_message_text.await_args_list[0].args[0])
        self.assertEqual(_sent_to(cb.bot), [1001])   # только заметка пользователю
        self.assertNotIn(42, feedback._fb_admin_notifs)  # снято с учёта


class FbReplyStartTests(FbBase):
    def _session_with_feedback(self):
        row = (2002, "bob", "у меня вопрос")
        return SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: row)),
            commit=AsyncMock(),
        )

    async def test_admin_enters_reply_state(self):
        cb = _fake_cb("fb:reply:42", user_id=111, chat_id=111)
        state = FakeState()
        with _MultiPatch(_patches(self._session_with_feedback())):
            await feedback.fb_reply_start(cb, state)
        self.assertEqual(state.current_state, AdminReplyStates.waiting_reply.state)
        self.assertEqual(state.values["fb_user_id"], 2002)
        self.assertEqual(state.values["fb_id"], 42)

    async def test_non_admin_rejected(self):
        cb = _fake_cb("fb:reply:42", user_id=999, chat_id=999)
        state = FakeState()
        with _MultiPatch(_patches(self._session_with_feedback())):
            await feedback.fb_reply_start(cb, state)
        self.assertIsNone(state.current_state)
        cb.answer.assert_awaited()
        self.assertEqual(cb.bot.send_message.await_count, 0)


def _admin_msg(text_="Вот Вам ответ", deliver=True, admin_id=111):
    async def _send(chat_id, *a, **k):
        if not deliver and chat_id == 2002:
            raise Exception("blocked")
        return SimpleNamespace(message_id=1)
    return SimpleNamespace(
        chat=SimpleNamespace(id=admin_id),
        from_user=SimpleNamespace(id=admin_id, username="admin"),
        text=text_,
        answer=AsyncMock(return_value=SimpleNamespace(message_id=2)),
        delete=AsyncMock(),
        bot=SimpleNamespace(
            send_message=AsyncMock(side_effect=_send),
            delete_message=AsyncMock(),
            edit_message_text=AsyncMock(),
        ),
    )


def _calls_to(bot, chat_id):
    return [c for c in bot.send_message.await_args_list if c.args[0] == chat_id]


class FbReplySendTests(FbBase):
    async def test_question_before_answer_and_marks_answered(self):
        msg = _admin_msg("Вот Вам ответ", deliver=True)
        state = FakeState(data={"fb_id": 42, "fb_user_id": 2002, "fb_original": "мой вопрос"})
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_reply_send(msg, state)
        user_call = _calls_to(msg.bot, 2002)[0]
        body = user_call.args[1]
        self.assertLess(body.index("мой вопрос"), body.index("Вот Вам ответ"))
        self.assertTrue(session.execute.await_args is not None)   # answered_at
        # текст ответа сохранён в БД (для «Мои обращения»)
        self.assertEqual(session.execute.await_args.args[1]["ans"], "Вот Вам ответ")
        # подтверждение админу (111) с «✅»
        self.assertIn("✅", _calls_to(msg.bot, 111)[0].args[1])
        self.assertTrue(state.cleared)

    async def test_delivery_failure_does_not_mark_answered(self):
        msg = _admin_msg("ответ", deliver=False)
        state = FakeState(data={"fb_id": 42, "fb_user_id": 2002, "fb_original": "вопрос"})
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_reply_send(msg, state)
        self.assertIsNone(session.execute.await_args)             # answered_at не ставим
        self.assertIn("Не удалось", _calls_to(msg.bot, 111)[0].args[1])
        self.assertTrue(state.cleared)


class FbQuickReplyTests(FbBase):
    def test_filter_true_only_when_armed(self):
        feedback._admin_reply_target[111] = {"fb_id": 42, "user_id": 2002, "original": "в"}
        with _MultiPatch(_patches(SimpleNamespace(execute=AsyncMock(), commit=AsyncMock()))):
            armed = _admin_msg("привет", admin_id=111)
            self.assertTrue(feedback._admin_can_quick_reply(armed))
            # не-админ
            self.assertFalse(feedback._admin_can_quick_reply(_admin_msg("x", admin_id=999)))
            # команда — не перехватываем
            self.assertFalse(feedback._admin_can_quick_reply(_admin_msg("/start", admin_id=111)))
            # не «вооружён»
            feedback._admin_reply_target.clear()
            self.assertFalse(feedback._admin_can_quick_reply(_admin_msg("привет", admin_id=111)))

    async def test_quick_reply_delivers_and_disarms(self):
        feedback._admin_reply_target[111] = {"fb_id": 42, "user_id": 2002, "original": "мой вопрос"}
        msg = _admin_msg("быстрый ответ", deliver=True, admin_id=111)
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_quick_reply(msg, FakeState())
        user_call = _calls_to(msg.bot, 2002)[0]
        self.assertIn("быстрый ответ", user_call.args[1])
        self.assertTrue(session.execute.await_args is not None)   # answered_at
        self.assertNotIn(111, feedback._admin_reply_target)        # разоружились


class FbDismissTests(FbBase):
    async def test_dismiss_deletes_message(self):
        self._prime_notif(42)
        cb = _fake_cb("fb:dismiss:42", user_id=111, chat_id=111)
        with _MultiPatch(_patches(SimpleNamespace(execute=AsyncMock(), commit=AsyncMock()))):
            await feedback.fb_dismiss(cb)
        cb.message.delete.assert_awaited()
        self.assertNotIn(42, feedback._fb_admin_notifs)


class FbMineTests(FbBase):
    async def test_view_shows_question_and_answer(self):
        cb = _fake_cb("fb:mineview:42", user_id=17)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(
                first=lambda: ("мой вопрос", "ответ админа", "2026-07-20", 1))),
            commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_mine_view(cb)
        sent = cb.bot.send_message.await_args.args[1]
        self.assertIn("мой вопрос", sent)
        self.assertIn("ответ админа", sent)

    async def test_view_pending_when_no_answer(self):
        cb = _fake_cb("fb:mineview:42", user_id=17)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(
                first=lambda: ("вопрос", None, None, 1))),
            commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_mine_view(cb)
        sent = cb.bot.send_message.await_args.args[1]
        self.assertIn("ещё не ответил", sent)

    async def test_view_offers_delete_only_when_found(self):
        cb = _fake_cb("fb:mineview:42", user_id=17)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(
                first=lambda: ("вопрос", None, None, 0))),
            commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_mine_view(cb)
        kb = cb.bot.send_message.await_args.kwargs["reply_markup"]
        self.assertEqual(kb.inline_keyboard[0][0].callback_data, "fb:minedel:42")

    async def test_view_no_delete_button_when_not_found(self):
        cb = _fake_cb("fb:mineview:42", user_id=17)
        session = SimpleNamespace(
            execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)),
            commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_mine_view(cb)
        kb = cb.bot.send_message.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertNotIn("fb:minedel:42", callbacks)


class FbMineDeleteTests(FbBase):
    async def test_confirm_shows_cancel_and_confirm(self):
        cb = _fake_cb("fb:minedel:42", user_id=17)
        await feedback.fb_mine_delete_confirm(cb)
        kb = cb.message.edit_reply_markup.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("fb:mineview:42", callbacks)      # отмена — назад к карточке
        self.assertIn("fb:minedel_yes:42", callbacks)    # подтверждение

    async def test_delete_yes_removes_own_row_only(self):
        cb = _fake_cb("fb:minedel_yes:42", user_id=17)
        session = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
        # _render_fb_mine делает ещё один SELECT COUNT после удаления — вернём «пусто»
        session.execute = AsyncMock(side_effect=[
            None,  # DELETE
            SimpleNamespace(scalar_one=lambda: 0),   # COUNT total в _render_fb_mine
            SimpleNamespace(fetchall=lambda: []),     # SELECT rows (тот же блок)
        ])
        with _MultiPatch(_patches(session)):
            await feedback.fb_mine_delete_yes(cb)
        delete_call = session.execute.await_args_list[0]
        self.assertIn("DELETE FROM feedback", delete_call.args[0].text)
        self.assertEqual(delete_call.args[1], {"id": 42, "uid": 17})

    async def test_delete_clears_stale_admin_refs(self):
        feedback._fb_admin_notifs[42] = {"who": "@t", "user_id": 17, "body": "x", "msgs": []}
        feedback._admin_reply_target[111] = {"fb_id": 42, "user_id": 17, "original": "x"}
        cb = _fake_cb("fb:minedel_yes:42", user_id=17)
        session = SimpleNamespace(execute=AsyncMock(side_effect=[
            None,
            SimpleNamespace(scalar_one=lambda: 0),
            SimpleNamespace(fetchall=lambda: []),
        ]), commit=AsyncMock())
        with _MultiPatch(_patches(session)):
            await feedback.fb_mine_delete_yes(cb)
        self.assertNotIn(42, feedback._fb_admin_notifs)
        self.assertNotIn(111, feedback._admin_reply_target)


if __name__ == "__main__":
    unittest.main()
