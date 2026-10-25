"""Project-local crypto invoices; no website settings or Tribute policy are changed."""
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import BigInteger, DateTime, Integer, String, UniqueConstraint, select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from config import PROJECT_KEY, BOT_TOKEN, GROUP_ID, INVITE_LINK, ADMIN_IDS, MINIAPP_URL
from database import (Base, User, async_session, get_or_create_user, apply_external_grant,
                      complete_bundle_checkout, get_latest_invite, create_pending_invite)
from utils.bundles import activate_bundle_access, send_bundle_access_message
from utils.plisio import CryptoError, METHODS, api_key, payment_url_valid, request, assert_operation
from utils.tribute_checkout import PRODUCTS, PROJECTS

logger = logging.getLogger(__name__)
ORIGINS = {
    'asmrleaks': 'https://asmr-bot-production.up.railway.app',
    'privateleaks': 'https://privateleaksbot-production.up.railway.app',
    'asianleaks': 'https://asianleaksbot-production.up.railway.app',
    'extraleaks': 'https://extraleaksbot-production.up.railway.app',
}


class CryptoSetting(Base):
    __tablename__ = 'bot_crypto_settings'
    project: Mapped[str] = mapped_column(String(32), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default='manual')
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CryptoOrder(Base):
    __tablename__ = 'bot_crypto_orders'
    __table_args__ = (UniqueConstraint('project', 'request_id', name='uq_bot_crypto_request'),)
    order_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    project: Mapped[str] = mapped_column(String(32), index=True)
    request_id: Mapped[str] = mapped_column(String(36))
    telegram_id: Mapped[int] = mapped_column(BigInteger, index=True)
    tier: Mapped[str] = mapped_column(String(16))
    target: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    coin: Mapped[str] = mapped_column(String(24))
    invoice_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    payment_url: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default='creating')
    provider_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


async def mode(session):
    row = await session.get(CryptoSetting, PROJECT_KEY)
    return 'plisio' if row and row.mode == 'plisio' else 'manual'


async def set_mode(session, value, admin_id):
    if value not in {'manual', 'plisio'}:
        raise CryptoError('Use /crypto_pay Plisio | Manual | status')
    if value == 'plisio' and not api_key():
        raise CryptoError('PLISIO_API_KEY is not configured for this bot', 503)
    dialect = session.get_bind().dialect.name
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise CryptoError('Unsupported settings database', 503)
    await session.execute(insert(CryptoSetting).values(project=PROJECT_KEY, mode=value,
        updated_by=admin_id, updated_at=datetime.utcnow()).on_conflict_do_update(
            index_elements=['project'], set_={'mode': value, 'updated_by': admin_id,
                                            'updated_at': datetime.utcnow()}))
    await session.commit()
    return value


def test_scope():
    value = os.getenv('PLISIO_TEST_MODE', 'false').strip().lower()
    if value not in {'true', 'false'}:
        raise CryptoError('Invalid Plisio test configuration', 503)
    if value == 'false':
        return None
    raw = os.getenv('PLISIO_TEST_TELEGRAM_ID', '').strip()
    if not re.fullmatch(r'[1-9][0-9]{0,18}', raw) or int(raw) >= 2**63:
        raise CryptoError('A single valid Plisio test account is required', 503)
    return int(raw)


async def provider_for(session, telegram_id=None, tier=None):
    provider = await mode(session)
    if provider != 'plisio':
        return 'manual'
    tester = test_scope()
    if tester is not None and (PROJECT_KEY != 'asmrleaks' or type(telegram_id) is not int
                               or telegram_id != tester or tier != 'plus'):
        return 'manual'
    return 'plisio'


async def settings(telegram_id=None, tier=None):
    async with async_session() as session:
        provider = await provider_for(session, telegram_id, tier)
    return {'provider': provider, 'ready': provider == 'manual' or bool(api_key()),
            'test_only': provider == 'plisio' and test_scope() is not None,
            'price_cents': 600 if provider == 'plisio' and test_scope() is not None else None,
            'methods': [{'key': key, 'asset': asset, 'network': network}
                        for key, (_, asset, network) in METHODS.items()]}


def plan(tier, target):
    product = PRODUCTS.get(('asmrleaks', tier) if tier in {'elite', 'king'} else (PROJECT_KEY, tier))
    if not product:
        raise CryptoError('Invalid plan')
    target = str(target or '').lower().strip() or None
    if tier == 'elite' and target not in PROJECTS - {PROJECT_KEY}:
        raise CryptoError('Choose the second page for ELITE')
    if tier == 'king':
        if target not in {None, 'all'}:
            raise CryptoError('Invalid KING target')
        target = 'all'
    elif tier != 'elite' and target:
        raise CryptoError('This plan has no second page')
    return product[1], target


def public_order(order):
    return {'order_id': order.order_id, 'provider': 'plisio', 'status': order.status,
            'tier': order.tier, 'coin': order.coin, 'price_cents': order.price_cents,
            'currency': 'EUR', 'days': order.days or 31,
            'payment_url': order.payment_url if payment_url_valid(order.payment_url) else None}


async def owned_order(session, order_id, telegram_id=None, lock=False):
    if not isinstance(order_id, str) or not re.fullmatch(r'mc_[a-z]+_[a-f0-9-]{36}', order_id):
        raise CryptoError('Invoice not found', 404)
    query = select(CryptoOrder).where(CryptoOrder.order_id == order_id, CryptoOrder.project == PROJECT_KEY)
    if telegram_id is not None:
        query = query.where(CryptoOrder.telegram_id == telegram_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    order = (await session.execute(query)).scalar_one_or_none()
    if not order:
        raise CryptoError('Invoice not found', 404)
    return order


async def create_order(telegram_id, body):
    tier = str(body.get('tier') or '').lower()
    cents, target = plan(tier, body.get('bundle_target'))
    coin = str(body.get('coin') or '')
    if coin not in METHODS:
        raise CryptoError('Choose a supported cryptocurrency and network')
    try:
        request_id = str(uuid.UUID(str(body.get('request_id') or '')))
    except ValueError:
        raise CryptoError('A valid request_id is required') from None
    async with async_session() as session:
        # A retry reuses its original invoice even if the administrator changed mode.
        existing = (await session.execute(select(CryptoOrder).where(
            CryptoOrder.project == PROJECT_KEY, CryptoOrder.request_id == request_id))).scalar_one_or_none()
        if existing:
            if (existing.telegram_id, existing.tier, existing.target, existing.coin) != (telegram_id, tier, target, coin):
                raise CryptoError('Checkout request does not match', 409)
            return public_order(existing)
        if await provider_for(session, telegram_id, tier) != 'plisio':
            return {'provider': 'manual'}
        if not api_key():
            raise CryptoError('Plisio is temporarily unavailable. Contact support.', 503)
        await get_or_create_user(session, telegram_id)
        await session.execute(select(User).where(User.telegram_id == telegram_id).with_for_update())
        recent = await session.scalar(select(func.count()).select_from(CryptoOrder).where(
            CryptoOrder.project == PROJECT_KEY, CryptoOrder.telegram_id == telegram_id,
            CryptoOrder.created_at > datetime.utcnow() - timedelta(minutes=10)))
        if recent >= 5:
            raise CryptoError('Too many invoices. Reopen your existing invoice or wait a few minutes.', 429)
        order = CryptoOrder(order_id=f'mc_{PROJECT_KEY}_{uuid.uuid4()}', project=PROJECT_KEY,
            request_id=request_id, telegram_id=telegram_id, tier=tier, target=target,
            price_cents=cents, coin=coin, status='creating')
        session.add(order)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return await create_order(telegram_id, body)
    try:
        data = await request('invoices/new', {
            'order_number': order.order_id, 'order_name': f'{PROJECT_KEY} {tier.upper()} - 31 days',
            'source_currency': 'EUR', 'source_amount': f'{cents / 100:.2f}',
            'currency': METHODS[coin][0], 'allowed_psys_cids': METHODS[coin][0],
            'callback_url': ORIGINS[PROJECT_KEY] + '/payments/plisio/webhook?json=true',
            'success_invoice_url': MINIAPP_URL, 'fail_invoice_url': MINIAPP_URL, 'expire_min': 30,
        })
        if not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', str(data.get('txn_id', ''))) or not payment_url_valid(data.get('invoice_url')):
            raise CryptoError('Plisio returned an invalid invoice', 503)
        async with async_session() as session:
            row = await owned_order(session, order.order_id, lock=True)
            row.invoice_id, row.payment_url = data['txn_id'], data['invoice_url']
            row.status = 'pending'
            await session.commit()
            return public_order(row)
    except Exception:
        # The provider might have received a timed-out request. Never retry invoice
        # creation automatically with the same request, or silently claim it was paid.
        async with async_session() as session:
            row = await owned_order(session, order.order_id, lock=True)
            row.status = 'creation_unknown'
            await session.commit()
        raise CryptoError('Could not prepare the invoice. Try again later or contact support.', 503) from None


async def notify(bot, text):
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.warning('Crypto admin notification could not be delivered')


async def deliver(order):
    is_bundle = order.tier in {'elite', 'king'}
    local_tier = 'pro' if is_bundle and PROJECT_KEY == 'asmrleaks' else order.tier
    async with async_session() as session:
        applied, user = await apply_external_grant(session, source_project='plisio',
            order_uuid=order.order_id, telegram_id=order.telegram_id, days=order.days,
            tier=local_tier, payment_method='plisio_crypto')
    bot = Bot(token=BOT_TOKEN)
    try:
        if is_bundle:
            links, failures = await activate_bundle_access(bot, telegram_id=order.telegram_id,
                order_uuid=order.order_id, username=user.username, full_name=user.full_name,
                days=order.days, local_days_left=user.units, tier=order.tier, target_project=order.target)
            async with async_session() as session:
                await complete_bundle_checkout(session, telegram_id=order.telegram_id, tier=order.tier,
                    target_project=order.target, payment_method='plisio_crypto', order_uuid=order.order_id,
                    access_links=links, partial=bool(failures))
            if failures:
                raise CryptoError('Payment received; access delivery is being retried', 503)
            try:
                await send_bundle_access_message(bot, telegram_id=order.telegram_id, tier=order.tier,
                    days=order.days, local_days_left=user.units, links=links, lang=user.lang or 'en')
            except Exception:
                await notify(bot, 'Plisio: access is active and links are saved, but the private message could not be delivered.')
            return
        async with async_session() as session:
            invite = await get_latest_invite(session, order.telegram_id) if not applied else None
        if not invite and GROUP_ID:
            try:
                result = await bot.create_chat_invite_link(GROUP_ID, member_limit=1,
                    expire_date=int((datetime.now(timezone.utc) + timedelta(hours=72)).timestamp()))
                invite = result.invite_link
                async with async_session() as session:
                    await create_pending_invite(session, order.telegram_id, invite)
            except Exception:
                if not INVITE_LINK:
                    raise CryptoError('Payment received; invitation delivery is being retried', 503) from None
        invite = invite or INVITE_LINK
        if not invite:
            raise CryptoError('Payment received; invitation is not available yet', 503)
        text = (f'Plisio payment confirmed. Added: {order.days} days. Total: {user.units} days.'
                if (user.lang or 'en') != 'ru' else
                f'Оплата Plisio подтверждена. Добавлено: {order.days} дней. Итого: {user.units} дней.')
        try:
            await bot.send_message(order.telegram_id, text, reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text='Join group', url=invite)]]))
        except Exception:
            await notify(bot, 'Plisio: access is active, but the private message could not be delivered. The bot may be blocked.')
    finally:
        await bot.session.close()


async def reconcile(order_id, telegram_id=None, force=False):
    async with async_session() as session:
        order = await owned_order(session, order_id, telegram_id, lock=True)
        if order.status == 'confirmed' or not order.invoice_id:
            return public_order(order)
        if not force and order.checked_at and order.checked_at > datetime.utcnow() - timedelta(seconds=15):
            return public_order(order)
        order.checked_at = datetime.utcnow()
        await session.commit()
    data = await request('operations/' + order.invoice_id)
    provider_status = assert_operation(data, order)
    async with async_session() as session:
        row = await owned_order(session, order_id, telegram_id, lock=True)
        if row.status == 'confirmed':
            return public_order(row)
        row.provider_status = provider_status
        if provider_status == 'completed':
            if row.days is None:
                row.days = 31
                if PROJECT_KEY == 'asmrleaks':
                    from database import consume_active_promo_days
                    row.days, _ = await consume_active_promo_days(session, telegram_id=row.telegram_id,
                        default_days=31, payment_method='plisio_crypto', order_uuid=row.order_id)
                row.status = 'delivery_pending'
                await session.commit()
                row = await owned_order(session, order_id, telegram_id, lock=True)
                if row.status == 'confirmed':
                    return public_order(row)
            await deliver(row)
            row.status = 'confirmed'
        elif row.status != 'delivery_pending':
            row.status = {'new': 'pending', 'pending': 'pending', 'pending internal': 'pending',
                          'expired': 'expired', 'cancelled': 'expired', 'cancelled duplicate': 'expired',
                          'error': 'failed', 'mismatch': 'attention'}.get(provider_status, row.status)
        await session.commit()
        if row.status == 'confirmed':
            bot = Bot(token=BOT_TOKEN)
            try:
                await notify(bot, f'Plisio: {PROJECT_KEY} {row.tier.upper()} payment confirmed. '
                    f'User: {row.telegram_id}. EUR {row.price_cents / 100:.2f}. Added: {row.days} days.')
            finally:
                await bot.session.close()
        return public_order(row)
