"""Offline policy/scheduler/command checks: no production DB or Telegram calls."""
import ast
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import Boolean, Column, Integer, String, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

ROOT = Path(__file__).parents[1]
BEFORE = datetime(2026, 10, 25, 20, 59, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 10, 25, 21, 0, tzinfo=timezone.utc)


def load_function(file, name, namespace):
    tree = ast.parse((ROOT / file).read_text(encoding="utf-8-sig"))
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), file, "exec"), namespace)
    return namespace[name]


class GraceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base = declarative_base()
        class User(self.base):
            __tablename__ = "users"
            telegram_id = Column(Integer, primary_key=True)
            units = Column(Integer)
            tier = Column(String, default="plus")
            is_active = Column(Boolean, default=True)
            last_payment_method = Column(String, default="tribute")
            lang = Column(String, default="ru")
            username = Column(String, nullable=True)
            trial_used = Column(Boolean, default=False)
            notify_expiry = Column(Boolean, default=True)
        self.User = User
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.now = BEFORE
        self.ns = dict(Base=self.base, async_session=self.sessions, PROJECT_KEY="fixture-project")
        tree = ast.parse((ROOT / "utils/payment_grace.py").read_text(encoding="utf-8"))
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.ImportFrom) and n.module in {"database", "config"})]
        exec(compile(tree, "utils/payment_grace.py", "exec"), self.ns)
        self.ns["utc_now"] = lambda: self.now
        async with self.engine.begin() as connection:
            await connection.run_sync(self.base.metadata.create_all)
        self.notices = AsyncMock()
        self.bot = SimpleNamespace(ban_chat_member=AsyncMock(), unban_chat_member=AsyncMock(), send_message=AsyncMock())
        self.scheduler_ns = dict(
            Bot=object, async_session=self.sessions, User=User, select=select,
            load_policy=self.ns["load_policy"], send_renewal_notice=self.notices,
            logger=logging.getLogger("grace-test"), GROUP_ID=-100123, ADMIN_IDS=[],
            t=lambda _key, _lang: "fixture", _kick_reason=lambda _user: ("Tribute", "Expired"),
        )
        self.daily = load_function("utils/scheduler.py", "daily_unit_check", self.scheduler_ns)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def schedule(self, enabled):
        async with self.sessions() as session:
            return await self.ns["schedule_policy"](session, enabled, 123, self.now)

    async def policy(self):
        async with self.sessions() as session:
            return await self.ns["load_policy"](session, self.now)

    async def seed(self, **kwargs):
        async with self.sessions() as session:
            session.add(self.User(**kwargs))
            await session.commit()

    async def test_default_keeps_grace_and_status_does_not_create_row(self):
        self.assertTrue((await self.policy()).enabled)
        async with self.sessions() as session:
            self.assertEqual((await session.execute(select(self.ns["PaymentGraceSetting"]))).scalars().all(), [])

    async def test_off_is_persistent_and_effective_exactly_at_next_moscow_midnight(self):
        pending = await self.schedule(False)
        self.assertTrue(pending.enabled)
        self.assertFalse(pending.pending_enabled)
        self.assertEqual(pending.effective_at, MIDNIGHT)
        self.assertTrue((await self.policy()).enabled)
        self.now = MIDNIGHT
        self.assertFalse((await self.policy()).enabled)
        self.now += timedelta(days=10)
        self.assertFalse((await self.policy()).enabled)

    async def test_repeat_off_does_not_push_cutoff_and_on_cancels_pending_off(self):
        first = await self.schedule(False)
        self.now += timedelta(seconds=30)
        self.assertEqual((await self.schedule(False)).effective_at, first.effective_at)
        policy = await self.schedule(True)
        self.assertTrue(policy.enabled)
        self.assertIsNone(policy.effective_at)

    async def test_on_after_off_waits_until_following_midnight(self):
        await self.schedule(False)
        self.now = MIDNIGHT + timedelta(minutes=10)
        policy = await self.schedule(True)
        self.assertFalse(policy.enabled)
        self.assertTrue(policy.pending_enabled)
        self.assertEqual(policy.effective_at, MIDNIGHT + timedelta(days=1))

    async def test_settings_are_scoped_to_project(self):
        await self.schedule(False)
        self.ns["PROJECT_KEY"] = "other-project"
        self.now = MIDNIGHT
        self.assertTrue((await self.policy()).enabled)

    async def test_scheduling_never_touches_user_balances(self):
        await self.seed(telegram_id=101, units=-3)
        await self.schedule(False)
        async with self.sessions() as session:
            user = await session.get(self.User, 101)
            self.assertEqual((user.units, user.is_active), (-3, True))
        self.bot.ban_chat_member.assert_not_awaited()

    async def test_group_access_uses_policy_and_preserves_positive_days_and_lifetime(self):
        access = self.ns["has_paid_access"]
        policy = self.ns["GracePolicy"]
        user = SimpleNamespace(is_active=True, tier="plus", units=-2, last_payment_method="tribute")
        self.assertTrue(access(user, policy(True)))
        self.assertFalse(access(user, policy(False)))
        user.units = 1
        self.assertTrue(access(user, policy(False)))
        user.units = -20
        user.tier = "lifetime"
        self.assertTrue(access(user, policy(False)))
        user.is_active = False
        self.assertFalse(access(user, policy(True)))

    async def test_midnight_removes_expired_including_preexisting_minus_seven_without_erasing_debt(self):
        await self.schedule(False)
        for id_, units, tier in [(101,1,"plus"), (102,0,"plus"), (103,-3,"plus"),
                                 (104,-7,"plus"), (105,-9,"plus"), (106,8,"plus"),
                                 (107,-5,"lifetime")]:
            await self.seed(telegram_id=id_, units=units, tier=tier)
        self.now = MIDNIGHT
        await self.daily(self.bot)
        async with self.sessions() as session:
            users = {u.telegram_id:u for u in (await session.execute(select(self.User))).scalars()}
        self.assertEqual(self.bot.ban_chat_member.await_count, 5)
        for id_ in range(101,106):
            self.assertFalse(users[id_].is_active)
        self.assertEqual([users[id_].units for id_ in [101,102,103,104,105,106,107]], [0,0,-3,-7,-9,7,-5])
        self.assertTrue(users[107].is_active)

    async def test_grace_stays_until_cutoff_and_kick_failure_is_retried(self):
        await self.seed(telegram_id=101, units=-6)
        await self.seed(telegram_id=102, units=-7)
        self.bot.ban_chat_member.side_effect = RuntimeError("fixture unavailable")
        await self.daily(self.bot)
        self.assertEqual(self.bot.ban_chat_member.await_count, 2)
        self.bot.ban_chat_member.side_effect = None
        await self.daily(self.bot)
        async with self.sessions() as session:
            users = (await session.execute(select(self.User))).scalars().all()
        self.assertTrue(all(not user.is_active and user.units == -7 for user in users))

    async def test_renewal_notices_at_three_and_one_days_not_for_lifetime_or_bundle(self):
        for id_, days, method, tier in [
            (101,4,"tribute","plus"), (102,2,"tribute","plus"),
            (103,6,"tribute","plus"), (104,4,"bundle_king_sync","plus"),
            (105,4,"tribute","lifetime")]:
            await self.seed(telegram_id=id_, units=days, last_payment_method=method, tier=tier)
        await self.daily(self.bot)
        self.assertEqual([(c.args[1].telegram_id,c.args[1].units) for c in self.notices.await_args_list], [(101,3),(102,1)])

    async def test_admin_only_private_chat_command_and_case_insensitive_off(self):
        ns = dict(Message=object, AsyncSession=object, ADMIN_IDS=[123],
                  MOSCOW=self.ns["MOSCOW"], load_policy=AsyncMock(),
                  schedule_policy=AsyncMock(return_value=self.ns["GracePolicy"](True,False,MIDNIGHT)))
        handler = load_function("handlers/payment_grace.py", "payment_grace", ns)
        for user, chat, text_ in [(999,"private","/payment_grace off"),
                                  (123,"group","/payment_grace off"),
                                  (123,"private","/payment_grace invalid")]:
            message = SimpleNamespace(from_user=SimpleNamespace(id=user), chat=SimpleNamespace(type=chat),
                                      text=text_, reply=AsyncMock())
            await handler(message, object())
        ns["schedule_policy"].assert_not_awaited()
        msg = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(type="private"),
                              text="/payment_grace Off", reply=AsyncMock())
        await handler(msg, object())
        self.assertEqual(ns["schedule_policy"].await_args.args[1:], (False,123))
        self.assertIn("26.10.2026 00:00", msg.reply.call_args.args[0])
        self.assertIn("только для этого бота", msg.reply.call_args.args[0])

    def test_nightly_job_runs_at_midnight(self):
        source = (ROOT / "utils/scheduler.py").read_text(encoding="utf-8")
        self.assertIn('CronTrigger(hour=0, minute=0, timezone="Europe/Moscow")', source)

    async def test_retired_webhook_never_reads_payload_or_touches_database(self):
        ns = dict(web=SimpleNamespace(Request=object,Response=object,
            json_response=lambda value, status: (value,status)))
        handler = load_function("webhook.py","tribute_webhook",ns)
        request = SimpleNamespace(read=AsyncMock())
        result = await handler(request)
        self.assertEqual(result[1],410)
        request.read.assert_not_awaited()


class RenewalCopyTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_and_button_and_notification_preference(self):
        ns = dict(urlparse=__import__("urllib.parse",fromlist=["urlparse"]).urlparse,
                  MINIAPP_URL="https://example.test/app",
                  InlineKeyboardButton=lambda **kw: kw, InlineKeyboardMarkup=lambda **kw: kw,
                  WebAppInfo=lambda **kw: kw)
        for fn in ["miniapp_keyboard","renewal_notice","send_renewal_notice"]:
            load_function("utils/payment_renewal.py",fn,ns)
        for lang in ["ru","en"]:
            text = ns["renewal_notice"](lang,3)
            self.assertNotIn("tribute/app",text)
            self.assertIn("Mini App",text)
        self.assertIn("не работает",ns["renewal_notice"]("ru",0))
        self.assertNotIn("Старое",ns["renewal_notice"]("ru",0,legacy=False))
        user = SimpleNamespace(telegram_id=101,lang="ru",units=3,
                               last_payment_method="tribute",notify_expiry=False)
        bot = SimpleNamespace(send_message=AsyncMock())
        await ns["send_renewal_notice"](bot,user)
        bot.send_message.assert_not_awaited()
        user.notify_expiry=True
        await ns["send_renewal_notice"](bot,user)
        button = bot.send_message.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button["web_app"]["url"],"https://example.test/app")
        ns["MINIAPP_URL"]="https://t.me/examplebot/app"
        self.assertEqual(ns["miniapp_keyboard"]("en")["inline_keyboard"][0][0]["url"],ns["MINIAPP_URL"])
