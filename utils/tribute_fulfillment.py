"""Retryable V2 fulfillment. Receipt intent and grants have separate stable identities."""
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from config import BOT_TOKEN, GROUP_ID, INVITE_LINK, PROJECT_KEY
from database import (async_session, TributeWebhookEvent, BundleCheckout,
                      claim_tribute_webhook_event, apply_external_grant,
                      consume_active_promo_days, complete_bundle_checkout,
                      create_pending_invite, get_latest_invite)
from utils.bundles import (activate_bundle_access, route_peer_bundle_purchase,
                           route_peer_plus_purchase, send_bundle_access_message)
from utils.error_reporting import notify_admin_error

logger = logging.getLogger(__name__)
STATE = '_v2_fulfillment'
PROJECTS = {'asmrleaks', 'privateleaks', 'asianleaks', 'extraleaks'}


class FulfillmentPending(RuntimeError):
    pass


async def _prepare(payment, data, find_bundle_source):
    async with async_session() as session:
        claimed = await claim_tribute_webhook_event(
            session, event_key=payment.event_key, name=data['name'],
            order_uuid=payment.order_uuid, payload=data['payload'],
        )
        row = (await session.execute(select(TributeWebhookEvent).where(
            TributeWebhookEvent.event_key == payment.event_key,
        ))).scalar_one()
        if not claimed:
            # The already-completed private test predates fulfillment metadata.
            return (row.payload or {}).get(STATE)
        source, target, checkout_id = payment.product.project, None, None
        if payment.product.tier in {'elite', 'king'}:
            context = None
            if payment.renewal:
                previous = (await session.execute(select(TributeWebhookEvent).where(
                    TributeWebhookEvent.event_key != payment.event_key,
                    TributeWebhookEvent.payload['subscription_id'].as_integer() == data['payload']['subscription_id'],
                    TributeWebhookEvent.payload['telegram_user_id'].as_integer() == payment.telegram_id,
                ).order_by(TributeWebhookEvent.received_at.desc()))).scalars().all()
                context = next((r.payload[STATE] for r in previous
                                if isinstance(r.payload, dict) and STATE in r.payload), None)
            if context:
                source, target, checkout_id = context['source'], context['target'], context['checkout_id']
            else:
                prepared, failures = await find_bundle_source(
                    session, telegram_id=payment.telegram_id, tier=payment.product.tier,
                    renewal=payment.renewal,
                )
                if failures or not prepared:
                    raise FulfillmentPending('bundle_checkout_unavailable')
                source, target = prepared['project'], prepared.get('target_project')
                checkout = prepared.get('checkout')
                checkout_id = checkout.id if checkout else prepared.get('checkout_id')
            if source not in PROJECTS or not checkout_id:
                raise FulfillmentPending('bundle_source_unavailable')
            if payment.product.tier == 'king':
                target = 'all'
            elif target not in PROJECTS - {source}:
                raise FulfillmentPending('elite_target_unavailable')
        days, promo = payment.product.days, None
        if source == PROJECT_KEY:
            days, promo = await consume_active_promo_days(
                session, telegram_id=payment.telegram_id, default_days=days,
                payment_method='tribute_v2', order_uuid=payment.order_uuid,
            )
        context = {'state':'pending', 'source':source, 'target':target,
                   'checkout_id':checkout_id, 'days':days, 'promo':promo}
        row.payload = {**data['payload'], STATE:context}
        await session.commit()
        return context


async def _deliver_local(payment, context, payload):
    tier = payment.product.tier
    is_bundle = tier in {'elite', 'king'}
    async with async_session() as session:
        applied, user = await apply_external_grant(
            session, source_project='tribute_v2', order_uuid=payment.order_uuid,
            telegram_id=payment.telegram_id, days=context['days'],
            tier='pro' if is_bundle else tier,
            username=payload.get('telegram_username'),
            full_name=payload.get('telegram_full_name') or payload.get('telegram_name'),
            payment_method='tribute_v2',
        )
    bot = Bot(token=BOT_TOKEN)
    try:
        if is_bundle:
            links, failures = await activate_bundle_access(
                bot, telegram_id=payment.telegram_id, order_uuid=payment.order_uuid,
                username=user.username, full_name=user.full_name, days=context['days'],
                local_days_left=user.units, tier=tier, target_project=context['target'],
            )
            async with async_session() as session:
                await complete_bundle_checkout(
                    session, telegram_id=payment.telegram_id, tier=tier,
                    target_project=context['target'], payment_method='tribute_v2',
                    order_uuid=payment.order_uuid, access_links=links,
                    checkout_id=context['checkout_id'], partial=bool(failures),
                )
            if failures:
                raise FulfillmentPending('bundle_delivery_pending')
            await send_bundle_access_message(
                bot, telegram_id=payment.telegram_id, tier=tier, days=context['days'],
                local_days_left=user.units, links=links, lang=user.lang or 'en',
                order_uuid=payment.order_uuid,
            )
            return
        async with async_session() as session:
            invite = await get_latest_invite(session, payment.telegram_id) if not applied else None
        if not invite and GROUP_ID:
            created = await bot.create_chat_invite_link(
                GROUP_ID, member_limit=1,
                expire_date=int((datetime.now(timezone.utc)+timedelta(hours=72)).timestamp()),
            )
            invite = created.invite_link
            async with async_session() as session:
                await create_pending_invite(session, payment.telegram_id, invite)
        invite = invite or INVITE_LINK
        if not invite:
            raise FulfillmentPending('invite_delivery_pending')
        if (user.lang or 'en') == 'ru':
            text = (f'Оплата через Tribute прошла успешно!\n\n'
                    f'Добавлено: <b>{context["days"]} дней</b>\n'
                    f'Итого: <b>{user.units} дней</b>')
            label = 'Вступить в группу'
        else:
            text = (f'Tribute payment successful!\n\n'
                    f'Added: <b>{context["days"]} days</b>\n'
                    f'Total: <b>{user.units} days</b>')
            label = 'Join the group'
        try:
            await bot.send_message(payment.telegram_id, text, parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text=label, url=invite),
                ]]))
        except Exception as error:
            # A blocked private chat never makes an already credited payment retry.
            await notify_admin_error(
                bot, title='Tribute: не удалось отправить сообщение после оплаты',
                stage='Отправка сообщения; доступ уже активирован', error=error,
                telegram_id=payment.telegram_id, access_state='active',
                order_uuid=payment.order_uuid,
            )
    finally:
        await bot.session.close()


async def fulfill_public_payment(payment, data, *, find_bundle_source, notify_admins):
    if PROJECT_KEY != 'asmrleaks':
        return web.json_response({'error':'invalid_payment_router'}, status=503)
    try:
        context = await _prepare(payment, data, find_bundle_source)
        if context is None:
            return web.json_response({'status':'ok','duplicate':True})
        async with async_session() as session:
            # Hold only the receipt lock, never a user-balance lock, across peer calls.
            # Peer grants may call back into ASMR and must be able to commit there.
            row = (await session.execute(select(TributeWebhookEvent).where(
                TributeWebhookEvent.event_key == payment.event_key,
            ).with_for_update())).scalar_one()
            context = dict(row.payload[STATE])
            if context['state'] == 'completed':
                return web.json_response({'status':'ok','duplicate':True})
            payload = data['payload']
            if context['source'] == PROJECT_KEY:
                await _deliver_local(payment, context, payload)
            elif payment.product.tier == 'plus':
                result = await route_peer_plus_purchase(
                    project=context['source'], telegram_id=payment.telegram_id,
                    order_uuid=payment.order_uuid, days=context['days'],
                    username=payload.get('telegram_username'), full_name=payload.get('telegram_full_name'),
                )
                if not result.get('ok'):
                    raise FulfillmentPending('plus_delivery_pending')
            else:
                result = await route_peer_bundle_purchase(
                    project=context['source'], telegram_id=payment.telegram_id,
                    order_uuid=payment.order_uuid, days=context['days'], tier=payment.product.tier,
                    username=payload.get('telegram_username'), full_name=payload.get('telegram_full_name'),
                    renewal=payment.renewal, checkout_id=context['checkout_id'],
                    target_project=context['target'],
                )
                if not result.get('ok') or result.get('partial'):
                    raise FulfillmentPending('bundle_delivery_pending')
            row.payload = {**row.payload, STATE:{**context, 'state':'completed'}}
            await session.commit()
        await notify_admins(
            f'<b>Tribute V2: {payment.product.tier.upper()} активирован</b>\n'
            f'User: <code>{payment.telegram_id}</code>\n'
            f'Проект: <code>{context["source"]}</code> | +{context["days"]} дней\n'
            f'EUR {payment.product.price_cents / 100:.2f}'
        )
        return web.json_response({'status':'ok','tier':payment.product.tier})
    except Exception as error:
        code = str(error) if isinstance(error, FulfillmentPending) else 'fulfillment_pending'
        logger.warning('Tribute V2 fulfillment pending: %s (%s)', code, type(error).__name__)
        return web.json_response({'status':'retry','error':code}, status=503)
