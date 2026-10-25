"""Public seven-product fulfillment on temporary SQLite; all Telegram/peer IO mocked."""
import ast
from datetime import datetime
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import web
from sqlalchemy import Boolean, Column, Integer, String, JSON, DateTime, Text, UniqueConstraint, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base

from utils.tribute_v2 import parse_paid_event
from test_tribute_v2 import settings, event, signed, PAYER

ROOT = Path(__file__).parents[1]


def load_function(file, name, ns):
    tree = ast.parse((ROOT/file).read_text(encoding='utf-8-sig'))
    node = next(n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    node.decorator_list=[]
    exec(compile(ast.Module(body=[node],type_ignores=[]),file,'exec'),ns)
    return ns[name]


class PublicPayments(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.base=declarative_base()
        class User(self.base):
            __tablename__='users'
            telegram_id=Column(Integer,primary_key=True)
            units=Column(Integer,default=0)
            tier=Column(String,default='free')
            username=Column(String)
            full_name=Column(String)
            lang=Column(String,default='ru')
            is_active=Column(Boolean,default=False)
            last_payment_method=Column(String)
            badge=Column(String)
        class Grant(self.base):
            __tablename__='external_grants'
            __table_args__=(UniqueConstraint('source_project','order_uuid'),)
            id=Column(Integer,primary_key=True)
            source_project=Column(String)
            order_uuid=Column(String)
            telegram_id=Column(Integer)
            days=Column(Integer)
            tier=Column(String)
        class Receipt(self.base):
            __tablename__='tribute_webhook_events'
            id=Column(Integer,primary_key=True)
            event_key=Column(Text,unique=True)
            name=Column(String)
            order_uuid=Column(Text)
            payload=Column(JSON)
            received_at=Column(DateTime,default=datetime.utcnow)
        class Checkout(self.base):
            __tablename__='bundle_checkouts'
            id=Column(Integer,primary_key=True)
            telegram_id=Column(Integer)
            tier=Column(String)
            target_project=Column(String)
            status=Column(String,default='pending')
            payment_method=Column(String)
            order_uuid=Column(String,unique=True)
            access_links=Column(JSON)
            consumed=Column(Boolean,default=False)
            created_at=Column(DateTime,default=datetime.utcnow)
            completed_at=Column(DateTime)
        self.User,self.Grant,self.Receipt,self.Checkout=User,Grant,Receipt,Checkout
        self.temp=tempfile.TemporaryDirectory()
        self.engine=create_async_engine('sqlite+aiosqlite:///'+str(Path(self.temp.name)/'fixture.db'))
        self.sessions=async_sessionmaker(self.engine,expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.exec_driver_sql('PRAGMA journal_mode=WAL')
            await conn.run_sync(self.base.metadata.create_all)
        async with self.sessions() as session:
            session.add(User(telegram_id=PAYER,units=7,tier='free',lang='ru'))
            session.add(Checkout(id=1,telegram_id=PAYER,tier='elite',target_project='asianleaks'))
            await session.commit()
        async def get_user(session,telegram_id,**kwargs):
            user=await session.get(User,telegram_id)
            if user is None:
                user=User(telegram_id=telegram_id,units=0,tier='free')
                session.add(user);await session.commit()
            return user
        self.bot=SimpleNamespace(create_chat_invite_link=AsyncMock(return_value=SimpleNamespace(invite_link='https://example.test/fixture')),
            send_message=AsyncMock(),session=SimpleNamespace(close=AsyncMock()))
        self.ns=dict(User=User,ExternalGrant=Grant,TributeWebhookEvent=Receipt,BundleCheckout=Checkout,
            async_session=self.sessions,select=select,IntegrityError=IntegrityError,AsyncSession=object,
            datetime=datetime,get_or_create_user=get_user,assign_tier_badge=lambda u:None,
            consume_active_promo_days=AsyncMock(return_value=(31,None)),
            create_pending_invite=AsyncMock(),get_latest_invite=AsyncMock(return_value='https://example.test/saved'),
            Bot=lambda **kwargs:self.bot,BOT_TOKEN='fixture',GROUP_ID=-100123,INVITE_LINK='',PROJECT_KEY='asmrleaks',
            InlineKeyboardButton=lambda **kw:kw,InlineKeyboardMarkup=lambda **kw:kw,
            route_peer_plus_purchase=AsyncMock(return_value={'ok':True}),
            route_peer_bundle_purchase=AsyncMock(return_value={'ok':True}),
            activate_bundle_access=AsyncMock(return_value=([{'project':'asmrleaks','url':'https://example.test/a'},{'project':'asianleaks','url':'https://example.test/b'}],[])),
            send_bundle_access_message=AsyncMock(),notify_admin_error=AsyncMock(),
            logger=logging.getLogger('public-tribute-test'))
        load_function('database.py','claim_tribute_webhook_event',self.ns)
        load_function('database.py','apply_external_grant',self.ns)
        load_function('database.py','complete_bundle_checkout',self.ns)
        tree=ast.parse((ROOT/'utils/tribute_fulfillment.py').read_text(encoding='utf-8'))
        tree.body=[n for n in tree.body if not (isinstance(n,ast.ImportFrom) and (
            n.module in {'config','database','aiogram','aiogram.types'} or (n.module or '').startswith('utils.')))]
        exec(compile(tree,'utils/tribute_fulfillment.py','exec'),self.ns)
        self.find=AsyncMock(return_value=({'project':'asmrleaks','checkout':SimpleNamespace(id=1),'target_project':'asianleaks'},[]))
        self.notify=AsyncMock()

    async def asyncTearDown(self):
        await self.engine.dispose();self.temp.cleanup()

    async def pay(self,code='s1ajK',**changes):
        data=event(code,**changes)
        body,signature=signed(data)
        payment=parse_paid_event(body,signature,settings(TRIBUTE_V2_MODE='public'))
        result=await self.ns['fulfill_public_payment'](payment,data,find_bundle_source=self.find,notify_admins=self.notify)
        return result.status,json.loads(result.body)

    async def state(self):
        async with self.sessions() as session:
            user=await session.get(self.User,PAYER)
            grants=(await session.execute(select(func.count()).select_from(self.Grant))).scalar()
            receipts=(await session.execute(select(self.Receipt))).scalars().all()
            return user.units,user.tier,user.last_payment_method,grants,[r.payload for r in receipts]

    async def test_normal_six_euro_plus_and_duplicate_add_only_once(self):
        self.assertEqual((await self.pay())[0],200)
        self.assertEqual((await self.state())[:4],(38,'plus','tribute_v2',1))
        self.assertTrue((await self.pay())[1]['duplicate'])
        self.assertEqual((await self.state())[:4],(38,'plus','tribute_v2',1))
        self.bot.create_chat_invite_link.assert_awaited_once()
        self.bot.send_message.assert_awaited_once()

    async def test_pro_is_eight_euro_and_does_not_depend_on_net_amount(self):
        self.assertEqual((await self.pay('s1ajR',amount=650))[0],200)
        self.assertEqual((await self.state())[:4],(38,'pro','tribute_v2',1))

    async def test_all_three_other_plus_products_route_without_asmr_credit(self):
        for code,project in [('s1ajL','privateleaks'),('s1ajN','asianleaks'),('s1ajP','extraleaks')]:
            self.assertEqual((await self.pay(code))[0],200)
            self.assertEqual(self.ns['route_peer_plus_purchase'].await_args.kwargs['project'],project)
        self.assertEqual((await self.state())[:2],(7,'free'))
        self.bot.create_chat_invite_link.assert_not_awaited()

    async def test_peer_timeout_retry_keeps_the_same_id_and_never_credits_asmr(self):
        route=self.ns['route_peer_plus_purchase']
        route.side_effect=[TimeoutError('fixture'),{'ok':True}]
        self.assertEqual((await self.pay('s1ajL'))[0],503)
        self.assertEqual((await self.pay('s1ajL'))[0],200)
        self.assertEqual(route.await_args_list[0].kwargs['order_uuid'],route.await_args_list[1].kwargs['order_uuid'])
        self.assertEqual((await self.state())[:4],(7,'free',None,0))

    async def test_elite_partial_retries_without_losing_target_or_adding_days_twice(self):
        self.ns['activate_bundle_access'].side_effect=[([{'url':'https://example.test/a'}],[{'project':'asianleaks'}]),
            ([{'url':'https://example.test/a'},{'url':'https://example.test/b'}],[])]
        self.assertEqual((await self.pay('s1ajS'))[0],503)
        self.find.return_value=({'project':'privateleaks','checkout_id':55,'target_project':'extraleaks'},[])
        self.assertEqual((await self.pay('s1ajS'))[0],200)
        self.assertEqual((await self.state())[:4],(38,'pro','tribute_v2',1))
        self.find.assert_awaited_once()
        self.assertTrue(all(call.kwargs['target_project']=='asianleaks' for call in self.ns['activate_bundle_access'].await_args_list))
        async with self.sessions() as session:
            checkout=await session.get(self.Checkout,1)
            self.assertEqual(checkout.status,'completed')
            self.assertEqual(len(checkout.access_links),2)

    async def test_bundle_renewal_reuses_its_paid_source_instead_of_unrelated_recent_checkouts(self):
        self.assertEqual((await self.pay('s1ajS'))[0],200)
        self.find.side_effect=AssertionError('Renewal must use the saved paid context')
        data=event('s1ajS',expires_at='2026-12-25T12:00:00Z');data['name']='renewed_subscription'
        body,sig=signed(data);payment=parse_paid_event(body,sig,settings(TRIBUTE_V2_MODE='public'))
        response=await self.ns['fulfill_public_payment'](payment,data,find_bundle_source=self.find,notify_admins=self.notify)
        self.assertEqual(response.status,200)
        self.assertEqual((await self.state())[0],69)

    async def test_peer_elite_is_bound_to_its_original_checkout_on_retry(self):
        self.find.return_value=({'project':'privateleaks','checkout_id':42,'target_project':'extraleaks'},[])
        route=self.ns['route_peer_bundle_purchase'];route.side_effect=[{'ok':False},{'ok':True}]
        self.assertEqual((await self.pay('s1ajS'))[0],503)
        self.find.side_effect=AssertionError('Retry must not re-select another checkout')
        self.assertEqual((await self.pay('s1ajS'))[0],200)
        for call in route.await_args_list:
            self.assertEqual((call.kwargs['checkout_id'],call.kwargs['target_project']),(42,'extraleaks'))
        self.assertEqual((await self.state())[0],7)

    async def test_king_uses_all_pages_and_retries_partial_delivery(self):
        self.find.return_value=({'project':'privateleaks','checkout_id':9,'target_project':'all'},[])
        self.ns['route_peer_bundle_purchase'].return_value={'ok':True,'partial':True}
        self.assertEqual((await self.pay('s1ajU'))[0],503)
        self.ns['route_peer_bundle_purchase'].return_value={'ok':True,'partial':False}
        self.assertEqual((await self.pay('s1ajU'))[0],200)
        self.assertEqual(self.ns['route_peer_bundle_purchase'].await_args.kwargs['target_project'],'all')

    async def test_missing_bundle_selection_never_grants(self):
        self.find.return_value=(None,[])
        self.assertEqual((await self.pay('s1ajS'))[0],503)
        self.assertEqual((await self.state())[:4],(7,'free',None,0))

    async def test_blocked_dm_does_not_retry_or_recredit_a_paid_subscription(self):
        self.bot.send_message.side_effect=PermissionError('fixture blocked')
        self.assertEqual((await self.pay())[0],200)
        self.assertEqual((await self.pay())[0],200)
        self.assertEqual((await self.state())[0],38)
        self.ns['notify_admin_error'].assert_awaited_once()

    async def test_promo_is_consumed_once_and_kept_across_delivery_retry(self):
        self.ns['consume_active_promo_days'].return_value=(62,'FIXTURE')
        self.bot.create_chat_invite_link.side_effect=[TimeoutError('fixture'),SimpleNamespace(invite_link='https://example.test/a')]
        self.ns['get_latest_invite'].return_value=None
        self.assertEqual((await self.pay())[0],503)
        self.assertEqual((await self.pay())[0],200)
        self.assertEqual((await self.state())[0],69)
        self.ns['consume_active_promo_days'].assert_awaited_once()

    async def test_lifetime_is_never_downgraded_by_plus_or_pro(self):
        async with self.sessions() as session:
            user=await session.get(self.User,PAYER);user.tier='lifetime';await session.commit()
        self.assertEqual((await self.pay('s1ajR'))[0],200)
        self.assertEqual((await self.state())[1],'lifetime')

    async def test_all_peer_bundle_handlers_resume_bound_partial_purchases_once(self):
        paths=[(ROOT.parents[1],"privateleaks"),(ROOT.parent/"asianleaks-bot","asianleaks"),
               (ROOT.parent/"extraleaks-bot","extraleaks")]
        for index,(folder,project) in enumerate(paths):
            with self.subTest(project=project):
                uid=PAYER+index+10
                target="asianleaks" if project=="privateleaks" else "asmrleaks"
                checkout_id=10+index
                async with self.sessions() as session:
                    session.add(self.User(telegram_id=uid,units=7,tier='free'))
                    session.add(self.Checkout(id=checkout_id,telegram_id=uid,tier='elite',target_project=target))
                    await session.commit()
                ns={**self.ns, '_internal_grant_authorized':lambda request:True,
                    'BUNDLE_TARGETS':{'asmrleaks','privateleaks','asianleaks','extraleaks'}-{project},
                    'PROJECT_KEY':project,'PROJECT_LABEL':project,'_notify_admins':AsyncMock(),
                    'get_bundle_checkout_for_tribute':AsyncMock(side_effect=AssertionError('Bound checkout required'))}
                handler=load_function(str(folder/'webhook.py'),'internal_bundle_purchase',ns)
                ns['activate_bundle_access'].side_effect=[([{'url':'https://example.test/a'}],[{'project':target}]),
                    ([{'url':'https://example.test/a'},{'url':'https://example.test/b'}],[])]
                request=SimpleNamespace(json=AsyncMock(return_value={'telegram_id':uid,'tier':'elite',
                    'order_uuid':'tribute-v2:'+project,'days':31,'checkout_id':checkout_id,'target_project':target}))
                self.assertEqual((await handler(request)).status,503)
                self.assertEqual((await handler(request)).status,200)
                async with self.sessions() as session:
                    user=await session.get(self.User,uid)
                    checkout=await session.get(self.Checkout,checkout_id)
                    self.assertEqual((user.units,user.last_payment_method),(38,'tribute_v2'))
                    self.assertEqual((checkout.status,len(checkout.access_links)),('completed',2))
                ns['get_bundle_checkout_for_tribute'].assert_not_awaited()

    async def test_peer_rejects_a_checkout_bound_to_a_different_payer(self):
        folder=ROOT.parents[1]
        ns={**self.ns,'_internal_grant_authorized':lambda request:True,
            'BUNDLE_TARGETS':{'asmrleaks','asianleaks','extraleaks'},
            'PROJECT_KEY':'privateleaks','PROJECT_LABEL':'PrivateLeaks'}
        handler=load_function(str(folder/'webhook.py'),'internal_bundle_purchase',ns)
        request=SimpleNamespace(json=AsyncMock(return_value={'telegram_id':PAYER+99,'tier':'elite',
            'order_uuid':'tribute-v2:fixture','days':31,'checkout_id':1,'target_project':'asianleaks'}))
        self.assertEqual((await handler(request)).status,409)
        self.assertEqual((await self.state())[0],7)
