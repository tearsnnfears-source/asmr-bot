import asyncio
import hashlib
import hmac
import logging
import json
import time
import urllib.parse
from datetime import datetime as _dt, timedelta, timezone
from aiohttp import ClientSession, ClientTimeout, web
from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy import select, text as sa_text, func as sa_func, or_ as sa_or

from database import async_session, User, PendingPayment, Artist, get_all_artists, ArtistContent, get_artist_content, Tag, get_all_tags, get_reactions, get_user_reactions, get_user_reaction, set_reaction, get_comments, add_comment, ALLOWED_REACTIONS, Favorite, Playlist, PlaylistItem, ArtistSuggestion, CustomBadge, get_custom_badges, PendingInvite, BundleCheckout, create_pending_invite, consume_pending_invite, get_latest_invite, assign_tier_badge, activate_promo_code, get_active_promo_activation, consume_active_promo_days, claim_tribute_webhook_event, normalize_promo_code, apply_external_grant, prepare_bundle_checkout, get_bundle_checkout_for_tribute, complete_bundle_checkout, get_latest_bundle_access, bundle_access_payload, _auto_thumbnail
from config import TRIBUTE_API_KEY, TRIBUTE_SITE_WEBHOOK_URL, BOT_TOKEN, INVITE_LINK, STARS_PRICES, STARS_TIER_PRICES, GROUP_ID, ADMIN_IDS, TRIBUTE_PLUS_URL, TRIBUTE_PRO_URL, TRIBUTE_ELITE_URL, TRIBUTE_KING_URL, PROJECT_KEY, INTERNAL_GRANT_SECRET, BUNDLE_PEERS
from utils.bundles import activate_bundle_access, bundle_projects_for_tier, get_peer_access_status, get_peer_bundle_checkout_status, refresh_peer_invite, route_peer_bundle_purchase, route_peer_plus_purchase, send_bundle_access_message

logger = logging.getLogger(__name__)
_background_tasks: set[asyncio.Task] = set()

INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60
MINIAPP_STARS_PLANS = {
    "plus": {"days": 31, "stars": STARS_TIER_PRICES["plus"]},
    "pro": {"days": 31, "stars": STARS_TIER_PRICES["pro"]},
    "elite": {"days": 31, "stars": STARS_TIER_PRICES["elite"]},
    "king": {"days": 31, "stars": STARS_TIER_PRICES["king"]},
}


def _schedule_background(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def _tribute_event_key(data: dict) -> str:
    payload = data.get("payload") or {}
    unique_part = (
        payload.get("uuid")
        or payload.get("chargeUuid")
        or payload.get("transactionId")
        or payload.get("paymentToken")
        or payload.get("subscription_id")
        or payload.get("period_id")
        or payload.get("trb_user_id")
        or "unknown"
    )
    return f"{data.get('name') or 'unknown'}:{unique_part}:{data.get('created_at') or ''}"


def validate_telegram_init_data(init_data: str) -> dict | None:
    """Validate Telegram Mini App initData and return trusted user data."""
    if not init_data:
        logger.warning("validate_init_data: empty initData")
        return None
    # Defensive strip — Railway Variables UI is famous for letting a
    # stray '\n' or trailing space sneak in when you paste a bot token.
    # Telegram's HMAC is computed from the clean token, so any
    # invisible whitespace makes the calculated hash drift from
    # received and every request fails with "Cannot parse user".
    bot_token = (BOT_TOKEN or "").strip()
    if not bot_token:
        logger.warning("validate_init_data: BOT_TOKEN env is empty (or only whitespace)!")
        return None

    pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True)
    params = dict(pairs)
    received_hash = params.pop("hash", None)
    if not received_hash:
        logger.warning("validate_init_data: no hash in initData")
        return None
    # IMPORTANT: do NOT pop `signature`. The Telegram docs say
    # "exclude signature from data_check_string", but in practice
    # actual clients compute the hash WITH signature included. Popping
    # it makes HMAC drift for any client on Bot API 7.10+. Confirmed
    # by reproducing on the live miniapp twice; reverting fixes it.
    had_signature = "signature" in params

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(params.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode(),
        hashlib.sha256,
    ).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        # Show the first 6 chars of the bot token so we can verify which
        # bot's secret is sitting in env. Never log the full token.
        bot_prefix = bot_token.split(":", 1)[0] if ":" in bot_token else bot_token[:6]
        # Flag silently-suspicious whitespace too — helps spot the
        # paste-from-BotFather-with-trailing-newline footgun.
        env_len = len(BOT_TOKEN or "")
        clean_len = len(bot_token)
        logger.warning(
            "validate_init_data: HMAC mismatch. bot_token_id=%s had_signature=%s "
            "received=%s... calculated=%s... fields=%s env_len=%d clean_len=%d",
            bot_prefix,
            had_signature,
            received_hash[:8],
            calculated_hash[:8],
            sorted(params.keys()),
            env_len, clean_len,
        )
        return None

    try:
        auth_date = int(params.get("auth_date", "0"))
    except ValueError:
        return None
    if auth_date <= 0 or time.time() - auth_date > INIT_DATA_MAX_AGE_SECONDS:
        logger.warning("Expired Telegram initData")
        return None

    try:
        user = json.loads(params.get("user", "{}"))
        user_id = int(user.get("id"))
    except Exception:
        return None

    return {
        "init_data": init_data,
        "params": params,
        "user": user,
        "user_id": user_id,
    }


def _parse_user_id(init_data: str) -> int | None:
    user_data = validate_telegram_init_data(init_data)
    return user_data["user_id"] if user_data else None


def _content_meta(item: ArtistContent, include_url: bool = False) -> dict:
    thumbnail_url = item.thumbnail_url or _auto_thumbnail(item.url) or ""
    # Photos are their own thumbnails — fall back to the image URL itself
    # so the grid can render without a separate /content/play round-trip.
    if not thumbnail_url and item.content_type == "photo" and item.url:
        thumbnail_url = item.url
    data = {
        "id": item.id,
        "title": item.title or "",
        "thumbnail_url": thumbnail_url,
        "artist_name": item.artist_name,
        "tags": item.tags or "",
        "sort_order": item.sort_order,
        "created_at": item.created_at.isoformat() if item.created_at else "",
        "content_type": item.content_type,
        "views": getattr(item, "views", 0) or 0,
    }
    if include_url:
        data["url"] = item.url
        data["embed_url"] = item.url
    return data


async def _notify_admins(text: str):
    from config import ADMIN_IDS
    bot = Bot(token=BOT_TOKEN)
    try:
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception:
                pass
    finally:
        await bot.session.close()


async def _create_group_invite_for_user(telegram_id: int, store_pending: bool = False) -> str:
    """Create a one-time Telegram group invite, falling back to INVITE_LINK."""
    invite_link = INVITE_LINK or ""
    bot = Bot(token=BOT_TOKEN)
    try:
        if GROUP_ID:
            link_obj = await bot.create_chat_invite_link(
                GROUP_ID,
                member_limit=1,
                expire_date=int((_dt.utcnow() + timedelta(hours=72)).timestamp()),
            )
            invite_link = link_obj.invite_link
        if store_pending and invite_link:
            async with async_session() as session:
                await create_pending_invite(session, telegram_id, invite_link)
    except Exception as e:
        logger.error(f"Failed to create invite link for {telegram_id}: {e}")
    finally:
        await bot.session.close()
    return invite_link


def _internal_grant_authorized(request: web.Request) -> bool:
    if not INTERNAL_GRANT_SECRET:
        return False
    auth = request.headers.get("Authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    header_secret = request.headers.get("X-Internal-Grant-Secret", "").strip()
    return (
        bool(bearer and hmac.compare_digest(bearer, INTERNAL_GRANT_SECRET))
        or bool(header_secret and hmac.compare_digest(header_secret, INTERNAL_GRANT_SECRET))
    )


async def internal_grant_access(request: web.Request) -> web.Response:
    """Receive additive access from another project; bundle tiers map to ASMR PRO."""
    if not INTERNAL_GRANT_SECRET:
        return web.json_response({"error": "Internal grants are not configured"}, status=503)
    if not _internal_grant_authorized(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        data = await request.json()
        telegram_id = int(data.get("telegram_id", 0))
        days = int(data.get("days") or 31)
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    source_project = str(data.get("source_project") or "external").strip()
    order_uuid = str(data.get("order_uuid") or "").strip()
    incoming_tier = str(data.get("tier") or "elite").lower()
    username = (data.get("username") or "").strip() or None
    full_name = (data.get("full_name") or "").strip() or None
    silent = data.get("silent") is True
    payment_method = str(data.get("payment_method") or f"bundle_{incoming_tier}_sync").strip()[:32]

    if not telegram_id or not order_uuid:
        return web.json_response({"error": "telegram_id and order_uuid are required"}, status=400)
    if incoming_tier not in {"plus", "pro", "elite", "king"}:
        return web.json_response({"error": "Unsupported cross-grant tier"}, status=400)

    local_tier = "pro" if incoming_tier in {"elite", "king"} else incoming_tier
    async with async_session() as session:
        applied, user = await apply_external_grant(
            session,
            source_project=source_project,
            order_uuid=order_uuid,
            telegram_id=telegram_id,
            days=days,
            tier=local_tier,
            username=username,
            full_name=full_name,
            payment_method=payment_method,
        )
        total = user.units or 0
        lang = user.lang or "en"

    has_active_access = total > 0
    if applied or (silent and has_active_access):
        invite_link = await _create_group_invite_for_user(telegram_id, store_pending=not silent)
    elif has_active_access:
        async with async_session() as session:
            invite_link = await get_latest_invite(session, telegram_id)
        invite_link = invite_link or INVITE_LINK
    else:
        invite_link = ""

    if applied:
        nick = f"@{username}" if username else f"id{telegram_id}"
        await _notify_admins(
            f"<b>{incoming_tier.upper()} grant received for ASMR</b>\n"
            f"Source: <code>{source_project}</code>\n"
            f"Order: <code>{order_uuid}</code>\n"
            f"User: {nick} | <code>{telegram_id}</code>\n"
            f"ASMR tier: <b>PRO</b> | +{days} days | Total: {total} days"
        )

        if not silent and invite_link:
            bot = Bot(token=BOT_TOKEN)
            try:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                text = (
                    f"<b>{incoming_tier.upper()} access activated for ASMR.LEAKS.</b>\n\n"
                    f"ASMR tier: <b>PRO</b>\n"
                    f"Added: <b>{days} days</b>\n"
                    f"Total: <b>{total} days</b>"
                )
                if lang == "ru":
                    text = (
                        f"<b>{incoming_tier.upper()} доступ ASMR.LEAKS активирован.</b>\n\n"
                        f"Тариф ASMR: <b>PRO</b>\n"
                        f"Добавлено: <b>{days} дней</b>\n"
                        f"Итого: <b>{total} дней</b>"
                    )
                keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="Join ASMR.LEAKS", url=invite_link)
                ]])
                await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=keyboard)
            except Exception as exc:
                logger.info("Cannot DM synced ASMR user %s: %s", telegram_id, exc)
            finally:
                await bot.session.close()

    return web.json_response({
        "ok": True,
        "applied": applied,
        "telegram_id": telegram_id,
        "tier": local_tier,
        "days_left": total,
        "invite_link": invite_link,
    })


async def internal_access_status(request: web.Request) -> web.Response:
    """Return current local access without issuing or extending anything."""
    if not INTERNAL_GRANT_SECRET:
        return web.json_response({"error": "Internal grants are not configured"}, status=503)
    if not _internal_grant_authorized(request):
        return web.json_response({"error": "Forbidden"}, status=403)
    try:
        data = await request.json()
        telegram_id = int(data.get("telegram_id", 0))
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not telegram_id:
        return web.json_response({"error": "telegram_id is required"}, status=400)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
    days_left = max(0, (user.units or 0) if user else 0)
    return web.json_response({
        "ok": True,
        "active": days_left > 0,
        "days_left": days_left,
        "tier": (user.tier or "plus") if user else "plus",
    })


async def internal_refresh_invite(request: web.Request) -> web.Response:
    """Issue a fresh one-time invite only while the local balance is positive."""
    if not INTERNAL_GRANT_SECRET:
        return web.json_response({"error": "Internal grants are not configured"}, status=503)
    if not _internal_grant_authorized(request):
        return web.json_response({"error": "Forbidden"}, status=403)
    try:
        data = await request.json()
        telegram_id = int(data.get("telegram_id", 0))
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not telegram_id:
        return web.json_response({"error": "telegram_id is required"}, status=400)

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
    days_left = max(0, (user.units or 0) if user else 0)
    if days_left <= 0:
        return web.json_response({"error": "No active access", "active": False, "days_left": 0}, status=403)

    invite_link = await _create_group_invite_for_user(telegram_id, store_pending=False)
    if not invite_link:
        return web.json_response({"error": "Invite is unavailable"}, status=503)
    return web.json_response({
        "ok": True,
        "active": True,
        "days_left": days_left,
        "tier": user.tier or "plus",
        "invite_link": invite_link,
    })


async def forward_tribute_webhook(body: bytes, signature: str) -> None:
    if not TRIBUTE_SITE_WEBHOOK_URL:
        return

    headers = {"Content-Type": "application/json"}
    headers["x-asmr-bot-forwarded"] = "1"
    if signature:
        headers["trbt-signature"] = signature

    try:
        timeout = ClientTimeout(total=8)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(TRIBUTE_SITE_WEBHOOK_URL, data=body, headers=headers) as response:
                if response.status >= 400:
                    response_text = await response.text()
                    logger.warning(
                        "Failed to forward Tribute webhook to site: status=%s body=%s",
                        response.status,
                        response_text[:300],
                    )
    except Exception as e:
        logger.warning("Failed to forward Tribute webhook to site: %s", e)


async def _find_bundle_checkout_source(
    session,
    *,
    telegram_id: int,
    tier: str,
    renewal: bool,
) -> tuple[dict | None, list[dict]]:
    """Find the bot that prepared a shared Tribute checkout.

    A failed peer lookup is treated as unsafe: crediting the first available
    database could put a paid subscription in the wrong project.
    """
    candidates: list[dict] = []
    local_checkout = await get_bundle_checkout_for_tribute(
        session,
        telegram_id=telegram_id,
        tier=tier,
        renewal=renewal,
    )
    if local_checkout and (renewal or local_checkout.status == "pending"):
        event_at = (
            local_checkout.completed_at
            if renewal and local_checkout.completed_at
            else local_checkout.created_at
        )
        candidates.append({
            "project": PROJECT_KEY,
            "created_at": event_at.isoformat() if event_at else "",
            "target_project": local_checkout.target_project or "",
            "checkout": local_checkout,
        })

    peer_results = await asyncio.gather(*[
        get_peer_bundle_checkout_status(
            project=project,
            telegram_id=telegram_id,
            tier=tier,
            renewal=renewal,
        )
        for project in BUNDLE_PEERS
    ])
    failures = [result for result in peer_results if not result.get("ok")]
    candidates.extend(
        result for result in peer_results
        if result.get("ok") and result.get("available")
    )
    if failures or not candidates:
        return None, failures
    return max(candidates, key=lambda item: item.get("created_at") or ""), []

@web.middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        response = web.Response()
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

async def tribute_webhook(request: web.Request) -> web.Response:
    body = await request.read()
    signature = request.headers.get("trbt-signature", "")
    if TRIBUTE_API_KEY and not signature:
        logger.warning("Missing Tribute signature")
        return web.Response(status=403)
    if TRIBUTE_API_KEY:
        expected = hmac.new(TRIBUTE_API_KEY.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            logger.warning("Invalid Tribute signature")
            return web.Response(status=403)

    try:
        data = json.loads(body)
    except Exception:
        return web.Response(status=400)

    event = data.get("name")
    payload = data.get("payload", {})
    logger.info(f"Tribute webhook event: {event}")

    if event in ("new_subscription", "renewed_subscription"):
        telegram_id = int(payload.get("telegram_user_id", 0))
        if not telegram_id:
            return web.json_response({"status": "ok"})

        event_key = _tribute_event_key(data)
        order_uuid = payload.get("uuid") or None

        async with async_session() as session:
            claimed = await claim_tribute_webhook_event(
                session,
                event_key=event_key,
                name=event,
                order_uuid=order_uuid,
                payload=payload,
            )
            if not claimed:
                logger.info("Duplicate Tribute event ignored: %s", event_key)
                return web.json_response({"status": "ok", "duplicate": True})

            result = await session.execute(select(User).where(User.telegram_id == telegram_id))
            user = result.scalar_one_or_none()

            # Tribute шлёт сумму в МИНОРНЫХ единицах (центах), e.g. 540 для 5.40 EUR.
            # Раньше код трактовал это как евро → 540 >= 8.5 → ELITE для всех плательщиков.
            # Sanity: подписки €6–€8, поэтому любое значение ≥100 — это точно центы.
            try:
                raw_amount = payload.get("amount", 0) or payload.get("price", 0) or 0
                amount_eur = float(raw_amount)
                if amount_eur >= 100:
                    amount_eur = amount_eur / 100.0
            except Exception:
                amount_eur = 0

            TRIBUTE_DAYS = 31
            event_hash = hashlib.sha256(event_key.encode()).hexdigest()[:24]
            tribute_order_uuid = f"tribute:{event_hash}"
            username = payload.get("telegram_username", "")
            full_name = (
                payload.get("telegram_full_name")
                or payload.get("telegram_name")
                or payload.get("name")
                or ""
            )

            # Every local PLUS product has a unique Tribute price. Route it
            # directly to the owning bot and never touch the ASMR balance.
            plus_project = next((
                project
                for price, project in (
                    (5.5, "privateleaks"),
                    (5.25, "asianleaks"),
                    (3.0, "extraleaks"),
                )
                if abs(amount_eur - price) < 0.001
            ), None)
            if plus_project:
                routed = await route_peer_plus_purchase(
                    project=plus_project,
                    telegram_id=telegram_id,
                    order_uuid=f"tribute-{plus_project}:{event_hash}",
                    username=username,
                    full_name=full_name,
                    days=TRIBUTE_DAYS,
                )
                logger.info(
                    "Tribute PLUS routed away from ASMR: project=%s amount_eur=%.2f telegram_id=%s ok=%s",
                    plus_project,
                    amount_eur,
                    telegram_id,
                    routed.get("ok"),
                )
                if routed.get("ok"):
                    await session.commit()
                    return web.json_response({"status": "ok", "routed": plus_project})
                await session.rollback()
                await _notify_admins(
                    f"<b>Tribute PLUS routing failed</b>\n"
                    f"Project: <code>{plus_project}</code>\n"
                    f"User: <code>{telegram_id}</code> | EUR {amount_eur:.2f}\n"
                    f"Error: <code>{routed.get('error') or 'unknown'}</code>"
                )
                return web.json_response(
                    {"status": "retry", "routed": plus_project, "error": routed.get("error")},
                    status=503,
                )

            if abs(amount_eur - 13.0) < 0.001:
                purchased_tier = "king"
            elif abs(amount_eur - 10.0) < 0.001:
                purchased_tier = "elite"
            elif amount_eur >= 7.0:
                purchased_tier = "pro"
            else:
                purchased_tier = "plus"

            is_bundle = purchased_tier in {"elite", "king"}
            bundle_checkout = None
            bundle_target = None
            if is_bundle:
                renewal = event == "renewed_subscription"
                bundle_source, lookup_failures = await _find_bundle_checkout_source(
                    session,
                    telegram_id=telegram_id,
                    tier=purchased_tier,
                    renewal=renewal,
                )
                if lookup_failures:
                    failed_projects = ", ".join(
                        item.get("project", "unknown") for item in lookup_failures
                    )
                    logger.warning(
                        "Bundle source lookup failed for user %s: %s",
                        telegram_id,
                        failed_projects,
                    )
                    await session.rollback()
                    return web.json_response(
                        {"status": "retry", "error": "Bundle source lookup failed"},
                        status=503,
                    )
                if not bundle_source:
                    logger.warning(
                        "No prepared %s checkout found for Telegram user %s",
                        purchased_tier,
                        telegram_id,
                    )
                    await session.rollback()
                    return web.json_response(
                        {"status": "retry", "error": "Bundle checkout is not prepared"},
                        status=409,
                    )

                source_project = bundle_source.get("project")
                if source_project != PROJECT_KEY:
                    routed = await route_peer_bundle_purchase(
                        project=source_project,
                        telegram_id=telegram_id,
                        tier=purchased_tier,
                        order_uuid=tribute_order_uuid,
                        username=username,
                        full_name=full_name,
                        days=TRIBUTE_DAYS,
                        renewal=renewal,
                    )
                    if routed.get("ok"):
                        await session.commit()
                        logger.info(
                            "Tribute %s routed to %s for user %s",
                            purchased_tier,
                            source_project,
                            telegram_id,
                        )
                        return web.json_response({
                            "status": "ok",
                            "tier": purchased_tier,
                            "routed": source_project,
                        })
                    await session.rollback()
                    await _notify_admins(
                        f"<b>{purchased_tier.upper()} routing failed</b>\n"
                        f"Project: <code>{source_project}</code>\n"
                        f"User: <code>{telegram_id}</code>\n"
                        f"Error: <code>{routed.get('error') or 'unknown'}</code>"
                    )
                    return web.json_response(
                        {
                            "status": "retry",
                            "routed": source_project,
                            "error": routed.get("error"),
                        },
                        status=503,
                    )

                bundle_checkout = bundle_source.get("checkout")
                bundle_target = "all" if purchased_tier == "king" else (
                    bundle_checkout.target_project if bundle_checkout else None
                )
                if purchased_tier == "elite" and not bundle_target:
                    logger.warning("ELITE Tribute payment has no saved target: telegram_id=%s", telegram_id)
                    await session.rollback()
                    return web.json_response(
                        {"status": "retry", "error": "ELITE target is not prepared"},
                        status=409,
                    )

            # Bundle purchases unlock the ASMR app at PRO level. The bundle
            # name is stored in bundle_checkouts, not in users.tier.
            local_tier = "pro" if is_bundle else purchased_tier

            logger.info(
                "Tribute payment: raw_amount=%s -> amount_eur=%.2f -> purchased_tier=%s local_tier=%s target=%s (telegram_id=%s)",
                payload.get("amount") or payload.get("price"),
                amount_eur, purchased_tier, local_tier, bundle_target, telegram_id,
            )

            # Никогда не понижаем admin-выданный тир (ELITE → PLUS/PRO) и не сбрасываем PRO.
            TIER_RANK = {'plus': 1, 'pro': 2, 'elite': 3}

            TRIBUTE_DAYS, promo_code = await consume_active_promo_days(
                session,
                telegram_id=telegram_id,
                default_days=31,
                payment_method="tribute",
                order_uuid=tribute_order_uuid,
            )
            if not user:
                user = User(
                    telegram_id=telegram_id,
                    username=payload.get("telegram_username"),
                    units=TRIBUTE_DAYS,
                    is_active=True,
                    last_payment_method="tribute",
                    tier=local_tier,
                )
                session.add(user)
            else:
                # Grace debt не переносится: при оплате юзер получает полные 31 день,
                # активный остаток (>0) суммируется как при обычном продлении.
                base = max(0, user.units)
                user.units = base + TRIBUTE_DAYS
                user.is_active = True
                user.last_payment_method = "tribute"
                # Tier: только апгрейд. ELITE/PRO нельзя автоматически понизить через Tribute.
                current_rank = TIER_RANK.get((user.tier or 'plus').lower(), 1)
                new_rank     = TIER_RANK.get(local_tier, 1)
                if new_rank > current_rank:
                    user.tier = local_tier
                # else: оставляем существующий тир как есть.
            assign_tier_badge(user)

            await session.commit()
            total = user.units
            lang = user.lang or "en"
            bundle_checkout_id = (
                bundle_checkout.id
                if bundle_checkout and bundle_checkout.status == "pending"
                else None
            )

        _schedule_background(forward_tribute_webhook(body, signature))

        if is_bundle:
            bundle_order_uuid = tribute_order_uuid
            bundle_bot = Bot(token=BOT_TOKEN)
            try:
                access_links, failures = await activate_bundle_access(
                    bundle_bot,
                    telegram_id=telegram_id,
                    order_uuid=bundle_order_uuid,
                    username=user.username or payload.get("telegram_username"),
                    full_name=user.full_name,
                    days=TRIBUTE_DAYS,
                    local_days_left=total,
                    tier=purchased_tier,
                    target_project=bundle_target,
                )
                async with async_session() as bundle_session:
                    await complete_bundle_checkout(
                        bundle_session,
                        telegram_id=telegram_id,
                        tier=purchased_tier,
                        target_project=bundle_target,
                        payment_method="tribute",
                        order_uuid=bundle_order_uuid,
                        access_links=access_links,
                        checkout_id=bundle_checkout_id,
                        partial=bool(failures),
                    )

                failed_projects = ", ".join(item.get("project", "unknown") for item in failures)
                username = payload.get("telegram_username", "")
                nick = f"@{username}" if username else f"id{telegram_id}"
                promo_line = f"\n🎟 Промокод: <code>{promo_code}</code>" if promo_code else ""
                failure_line = f"\n⚠️ Не выданы ссылки: <code>{failed_projects}</code>" if failures else ""
                await _notify_admins(
                    f"💳 <b>Новая {purchased_tier.upper()} оплата — Tribute</b>\n"
                    f"👤 {nick} | <code>{telegram_id}</code>\n"
                    f"📅 +{TRIBUTE_DAYS} | Итого ASMR: {total} дн. | tier=PRO\n"
                    f"🔗 Ссылок: {len(access_links)}"
                    f"{promo_line}{failure_line}"
                )
                await send_bundle_access_message(
                    bundle_bot,
                    telegram_id=telegram_id,
                    tier=purchased_tier,
                    days=TRIBUTE_DAYS,
                    local_days_left=total,
                    links=access_links,
                    lang=lang,
                )
            except Exception as exc:
                logger.error("Bundle Tribute activation failed for %s: %s", telegram_id, exc, exc_info=True)
                await _notify_admins(
                    f"⚠️ <b>{purchased_tier.upper()} bundle activation failed</b>\n"
                    f"User: <code>{telegram_id}</code>\nOrder: <code>{bundle_order_uuid}</code>"
                )
            finally:
                await bundle_bot.session.close()

            return web.json_response({
                "status": "ok",
                "tier": purchased_tier,
                "target_project": bundle_target,
            })

        # Уведомление админу о Tribute оплате
        username = payload.get("telegram_username", "")
        nick = f"@{username}" if username else f"id{telegram_id}"
        promo_line = f"\n🎟 Промокод: <code>{promo_code}</code>" if promo_code else ""
        await _notify_admins(
            f"💳 <b>Новая оплата — Tribute</b>\n"
            f"👤 {nick} | <code>{telegram_id}</code>\n"
            f"📅 +{TRIBUTE_DAYS} | Итого: {total} дн."
            f"{promo_line}"
        )

        bot = Bot(token=BOT_TOKEN)
        invite_link = INVITE_LINK
        try:
            from datetime import timedelta
            if GROUP_ID:
                link_obj = await bot.create_chat_invite_link(
                    GROUP_ID,
                    member_limit=1,
                    expire_date=int((_dt.utcnow() + timedelta(hours=72)).timestamp()),
                )
                invite_link = link_obj.invite_link
                async with async_session() as inv_session:
                    await create_pending_invite(inv_session, telegram_id, invite_link)
        except Exception as e:
            logger.error(f"Failed to create invite link for {telegram_id}: {e}")

        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            if lang == "ru":
                text = (f"✅ <b>Оплата через Tribute прошла успешно!</b>\n\n"
                        f"📅 Добавлено: <b>{TRIBUTE_DAYS} дней</b>\n"
                        f"📅 Итого: <b>{total} дней</b>\n\n"
                        f"👇 Нажми кнопку ниже чтобы вступить в группу.\n"
                        f"<i>Ссылка одноразовая — после вступления истекает.</i>")
            else:
                text = (f"✅ <b>Tribute payment successful!</b>\n\n"
                        f"📅 Added: <b>{TRIBUTE_DAYS} days</b>\n"
                        f"📅 Total: <b>{total} days</b>\n\n"
                        f"👇 Tap the button below to join the group.\n"
                        f"<i>One-time link — expires after use.</i>")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(
                    text="🔗 Вступить в группу" if lang == "ru" else "🔗 Join the group",
                    url=invite_link
                )
            ]])
            await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=kb)
        except Exception as e:
            logger.error(f"Cannot notify user {telegram_id}: {e}")
        finally:
            await bot.session.close()

    return web.json_response({"status": "ok"})


async def parse_init_data(request: web.Request) -> dict:
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        return validate_telegram_init_data(init_data)
    except:
        return None


async def api_prepare_bundle_checkout(request: web.Request) -> web.Response:
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data = await request.json()
        tier = str(data.get("tier") or "").lower()
        target_project = str(data.get("bundle_target") or "").lower() or None
        async with async_session() as session:
            checkout = await prepare_bundle_checkout(
                session,
                telegram_id=user_data["user_id"],
                tier=tier,
                target_project=target_project,
            )
        return web.json_response({
            "ok": True,
            "checkout_id": checkout.id,
            "tier": checkout.tier,
            "target_project": checkout.target_project,
        })
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:
        logger.error("prepare_bundle_checkout error: %s", exc)
        return web.json_response({"error": "Could not prepare checkout"}, status=500)


ACCESS_PROJECT_LABELS = {
    "asmrleaks": "ASMR.LEAKS",
    "privateleaks": "PrivateLeaks",
    "asianleaks": "AsianLeaks",
    "extraleaks": "ExtraLeaks",
}


def _bundle_entitlement_projects(bundle) -> list[str]:
    if not bundle:
        return []
    try:
        return ["asmrleaks", *bundle_projects_for_tier(bundle.tier, bundle.target_project)]
    except ValueError:
        return ["asmrleaks"]


async def api_access_links(request: web.Request) -> web.Response:
    """Return the pages represented by the user's latest active access record."""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    telegram_id = int(user_data["user_id"])

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        bundle = await get_latest_bundle_access(session, telegram_id, consume=False)
        latest_local_invite = await get_latest_invite(session, telegram_id)

    projects = _bundle_entitlement_projects(bundle)
    local_days = max(0, (user.units or 0) if user else 0)
    if not projects and local_days > 0:
        projects = ["asmrleaks"]

    peer_projects = [project for project in projects if project != "asmrleaks"]
    peer_statuses = await asyncio.gather(*[
        get_peer_access_status(project=project, telegram_id=telegram_id)
        for project in peer_projects
    ])
    status_by_project = {
        item.get("project"): item for item in peer_statuses if item.get("project")
    }

    stored = {
        item.get("project"): item
        for item in ((bundle.access_links or []) if bundle else [])
        if isinstance(item, dict) and item.get("project")
    }
    links = []
    for project in projects:
        saved = stored.get(project) or {}
        if project == "asmrleaks":
            active = local_days > 0
            status_available = True
            days_left = local_days
            url = (saved.get("url") or latest_local_invite or INVITE_LINK or "") if active else ""
        else:
            status = status_by_project.get(project) or {}
            status_available = bool(status.get("ok"))
            active = bool(status.get("active")) if status_available else None
            days_left = int(status.get("days_left") or 0) if status_available else None
            url = (saved.get("url") or "") if active else ""
        links.append({
            "project": project,
            "label": ACCESS_PROJECT_LABELS.get(project, project),
            "url": url,
            "days_left": days_left,
            "active": active,
            "status_available": status_available,
            "refreshable": True,
        })

    return web.json_response({
        "ok": True,
        "tier": bundle.tier if bundle else ((user.tier or "plus") if user else "free"),
        "access_links": links,
    })


async def api_refresh_access_link(request: web.Request) -> web.Response:
    """Verify a project's own balance, issue a new invite, and persist it."""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data = await request.json()
        project = str(data.get("project") or "").lower()
    except Exception:
        return web.json_response({"error": "Invalid request"}, status=400)
    if project not in ACCESS_PROJECT_LABELS:
        return web.json_response({"error": "Unknown project"}, status=400)

    telegram_id = int(user_data["user_id"])
    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        bundle = await get_latest_bundle_access(session, telegram_id, consume=False)

    entitled_projects = _bundle_entitlement_projects(bundle)
    if project == "asmrleaks":
        days_left = max(0, (user.units or 0) if user else 0)
        if days_left <= 0:
            return web.json_response({"error": "No active ASMR access", "active": False}, status=403)
        invite_link = await _create_group_invite_for_user(
            telegram_id,
            store_pending=bundle is None,
        )
        tier = user.tier or "plus"
    else:
        if not bundle or project not in entitled_projects:
            return web.json_response({"error": "This page is not included in your subscription"}, status=403)
        refreshed = await refresh_peer_invite(project=project, telegram_id=telegram_id)
        if not refreshed.get("ok"):
            status = 403 if refreshed.get("active") is False and refreshed.get("days_left") == 0 else 502
            return web.json_response({
                "error": refreshed.get("error") or "Could not refresh invite",
                "active": refreshed.get("active"),
                "days_left": refreshed.get("days_left"),
            }, status=status)
        invite_link = refreshed.get("url") or ""
        days_left = int(refreshed.get("days_left") or 0)
        tier = refreshed.get("tier") or bundle.tier

    if not invite_link:
        return web.json_response({"error": "Invite is unavailable"}, status=503)

    if bundle:
        async with async_session() as session:
            checkout = await session.get(BundleCheckout, bundle.id)
            current_links = list(checkout.access_links or [])
            replacement = {
                "project": project,
                "label": ACCESS_PROJECT_LABELS[project],
                "url": invite_link,
                "days_left": days_left,
            }
            checkout.access_links = [
                replacement if item.get("project") == project else item
                for item in current_links
                if isinstance(item, dict)
            ]
            if not any(item.get("project") == project for item in current_links if isinstance(item, dict)):
                checkout.access_links = [*checkout.access_links, replacement]
            await session.commit()

    return web.json_response({
        "ok": True,
        "project": project,
        "label": ACCESS_PROJECT_LABELS[project],
        "invite_link": invite_link,
        "days_left": days_left,
        "tier": tier,
        "active": True,
    })

async def api_create_stars_invoice(request: web.Request) -> web.Response:
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    
    try:
        data = await request.json()
        tier = str(data.get("tier", "plus")).lower()
        bundle_target = str(data.get("bundle_target") or "").lower()
        promo_code = normalize_promo_code(data.get("promo_code") or data.get("promocode") or "")
    except:
        tier = "plus"
        bundle_target = ""
        promo_code = ""

    plan = MINIAPP_STARS_PLANS.get(tier)
    if not plan:
        return web.json_response({"error": "Invalid tier"}, status=400)
    if tier == "elite" and bundle_target not in {"privateleaks", "asianleaks", "extraleaks"}:
        return web.json_response({"error": "Choose a valid ELITE second page"}, status=400)
    if tier == "king":
        bundle_target = "all"

    user_id = user_data["user_id"]
    days = plan["days"]
    stars = plan["stars"]
    applied_promo_code = None

    async with async_session() as session:
        if promo_code:
            try:
                activation = await activate_promo_code(
                    session,
                    telegram_id=user_id,
                    code=promo_code,
                )
            except ValueError as e:
                status = 409 if "limit" in str(e).lower() else 404
                return web.json_response({"error": str(e)}, status=status)
        else:
            activation = await get_active_promo_activation(session, user_id)
        if activation:
            days = activation.days
            applied_promo_code = activation.code

    payload = (
        f"stars_bundle_{tier}_{bundle_target}_{user_id}"
        if tier in {"elite", "king"}
        else f"stars_tier_{tier}_{user_id}"
    )
    
    bot = Bot(token=BOT_TOKEN)
    try:
        # Генерируем реальную строку инвойса от Telegram
        invoice_link = await bot.create_invoice_link(
            title=f"{tier.upper()} на {days} дней",
            description=(
                "ASMR PRO и доступ ко всем четырём приватным страницам"
                if tier == "king"
                else "ASMR PRO и вторая приватная страница"
                if tier == "elite"
                else "Оплата премиум доступа к ASMR.LEAKS"
            ),
            payload=payload,
            provider_token="", # Для Telegram Звезд это поле ОБЯЗАТЕЛЬНО должно быть пустым
            currency="XTR",
            prices=[LabeledPrice(label=f"{days} Days VIP", amount=stars)]
        )
        if invoice_link.startswith("https://telegram.me/"):
            invoice_link = "https://t.me/" + invoice_link[len("https://telegram.me/"):]
        return web.json_response({
            "invoice_link": invoice_link,
            "stars": stars,
            "days": days,
            "tier": tier,
            "bundle_target": bundle_target or None,
            "promo_code": applied_promo_code,
            "promo_days": days if applied_promo_code else None,
        })
    except Exception as e:
        logger.error(f"Error creating invoice: {e}")
        return web.json_response({"error": str(e)}, status=500)
    finally:
        await bot.session.close()


async def api_promo_activate(request: web.Request) -> web.Response:
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)

    try:
        data = await request.json()
        promo_code = data.get("promo_code") or data.get("promocode") or data.get("code") or ""
    except Exception:
        return web.json_response({"error": "Bad body"}, status=400)

    async with async_session() as session:
        try:
            activation = await activate_promo_code(
                session,
                telegram_id=user_data["user_id"],
                code=promo_code,
            )
        except ValueError as e:
            status = 409 if "limit" in str(e).lower() else 404
            return web.json_response({"ok": False, "error": str(e)}, status=status)

    return web.json_response({
        "ok": True,
        "code": activation.code,
        "days": activation.days,
        "message": f"Promo {activation.code} activated for {activation.days} days",
    })


async def api_check_invite(request: web.Request) -> web.Response:
    """POST /miniapp/check_invite — returns and consumes pending invite link for the user."""
    try:
        data = await request.json()
        user_data = validate_telegram_init_data(data.get("initData", ""))
        if not user_data:
            return web.json_response({"invite_link": None})
        user_id = user_data["user_id"]
        async with async_session() as session:
            bundle = await get_latest_bundle_access(session, int(user_id), consume=True)
            if bundle:
                payload = bundle_access_payload(bundle)
                payload["invite_link"] = (payload.get("access_links") or [{}])[0].get("url")
                return web.json_response(payload)
            link = await consume_pending_invite(session, int(user_id))
        return web.json_response({"invite_link": link})
    except Exception as e:
        logger.error(f"check_invite error: {e}")
        return web.json_response({"invite_link": None})


async def api_my_invite(request: web.Request) -> web.Response:
    """POST /miniapp/my_invite — newest invite link without consuming.

    The redesign's AppHeader bell taps into this so the link stays
    available even after the auto-opened modal has been dismissed.
    Read-only: rows are never marked used here.
    """
    try:
        data = await request.json()
        user_data = validate_telegram_init_data(data.get("initData", ""))
        if not user_data:
            return web.json_response({"invite_link": None})
        user_id = user_data["user_id"]
        async with async_session() as session:
            bundle = await get_latest_bundle_access(session, int(user_id), consume=False)
            if bundle:
                payload = bundle_access_payload(bundle)
                payload["invite_link"] = (payload.get("access_links") or [{}])[0].get("url")
                return web.json_response(payload)
            link = await get_latest_invite(session, int(user_id))
        return web.json_response({"invite_link": link})
    except Exception as e:
        logger.error(f"my_invite error: {e}")
        return web.json_response({"invite_link": None})


async def api_get_artists(request: web.Request) -> web.Response:
    async with async_session() as session:
        result = await session.execute(select(Artist).order_by(Artist.name))
        artists = result.scalars().all()
        from sqlalchemy import func
        content_result = await session.execute(
            select(ArtistContent.artist_name, func.count(ArtistContent.id))
            .group_by(ArtistContent.artist_name)
        )
        artists_with_content = {row[0] for row in content_result.all() if row[1] > 0}
        # Per-artist short count — Artist row only stores photos/videos, but
        # the frontend wants to render a real "Shorts N" header before any
        # paginated /artist_content fetch lands. Group ArtistContent by type.
        short_result = await session.execute(
            select(ArtistContent.artist_name, func.count(ArtistContent.id))
            .where(ArtistContent.content_type == "short")
            .group_by(ArtistContent.artist_name)
        )
        shorts_by_name = {row[0]: row[1] for row in short_result.all()}
        artists_data = [{
            "name": a.name, "photo_url": a.photo_url, "profile_photo_url": a.profile_photo_url,
            "topic_url": getattr(a, 'topic_url', None),
            "has_profile": a.name in artists_with_content,
            "photos": a.photos, "videos": a.videos,
            "shorts": shorts_by_name.get(a.name, 0),  # additive; old clients ignore.
            "tag_hot": getattr(a, 'tag_hot', False),
            "tag_new": getattr(a, 'tag_new', False), "tag_prom": getattr(a, 'tag_prom', False),
            "tag_ready": getattr(a, 'tag_ready', False)
        } for a in artists]
        return web.json_response({"artists": artists_data, "total": len(artists_data)})

async def api_get_profile(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=400)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            
            if not user:
                return web.json_response({"days_left": 0, "show_subscribe_button": True, "trial_used": False, "lang": ""})
            
            days_left = user.units or 0
            show_subscribe = True if days_left <= 3 else False
            
            return web.json_response({
                "days_left": days_left, "show_subscribe_button": show_subscribe,
                "trial_used": user.trial_used, "full_name": user.full_name or "",
                "username": user.username or "", "telegram_id": user.telegram_id,
                "is_active": user.is_active, "units": user.units, "lang": user.lang or "",
                "notify_expiry": getattr(user, 'notify_expiry', True),
                "badge": getattr(user, 'badge', None),
                "badges": [b.strip() for b in (getattr(user, 'badge', None) or "").split(",") if b.strip()],
                "tier": getattr(user, 'tier', 'plus') or 'plus',
                "tribute_plus_url": TRIBUTE_PLUS_URL,
                "tribute_pro_url": TRIBUTE_PRO_URL,
                "tribute_elite_url": TRIBUTE_ELITE_URL,
                "tribute_king_url": TRIBUTE_KING_URL,
            })
    except Exception as e:
        logger.error(f"Error in api_get_profile: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def api_set_language(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, lang = data.get("initData", ""), data.get("lang", "en")
        if not init_data: return web.json_response({"error": "No initData"}, status=400)
        if lang not in ("en", "ru", "es"): return web.json_response({"error": "Invalid language"}, status=400)
        
        user_id = _parse_user_id(init_data)
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                user = User(telegram_id=user_id, lang=lang)
                session.add(user)
            else:
                user.lang = lang
            await session.commit()
            return web.json_response({"ok": True, "lang": lang})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_set_notify_expiry(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, notify = data.get("initData", ""), data.get("notify_expiry", True)
        if not init_data: return web.json_response({"error": "No initData"}, status=400)
        
        user_id = _parse_user_id(init_data)
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                user = User(telegram_id=user_id, notify_expiry=bool(notify))
                session.add(user)
            else:
                user.notify_expiry = bool(notify)
            await session.commit()
            return web.json_response({"ok": True, "notify_expiry": bool(notify)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_get_favorites(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        if not init_data: return web.json_response({"error": "No initData"}, status=403)
        
        user_id = _parse_user_id(init_data)
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0: return web.json_response({"error": "No subscription"}, status=403)
            
            from database import Favorite
            result = await session.execute(select(Favorite).where(Favorite.telegram_id == user_id))
            favorites = result.scalars().all()
            items = [{"id": f.id, "title": f.title, "content_id": f.content_id} for f in favorites]
            return web.json_response({"items": items, "count": len(items), "limit": 100})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_add_favorite(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, url, title = data.get("initData", ""), data.get("url", ""), data.get("title", "")
        if not init_data or not url or not title: return web.json_response({"error": "Missing data"}, status=400)
        
        user_id = _parse_user_id(init_data)
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0: return web.json_response({"error": "No subscription"}, status=403)
            
            from database import Favorite
            result = await session.execute(select(Favorite).where(Favorite.telegram_id == user_id))
            if len(result.scalars().all()) >= 100: return web.json_response({"error": "Limit reached"}, status=409)
            
            fav = Favorite(telegram_id=user_id, title=title, url=url)
            session.add(fav)
            await session.commit()
            return web.json_response({"ok": True, "id": fav.id})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_delete_favorite(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data, fav_id = data.get("initData", ""), data.get("id", 0)
        if not init_data or not fav_id: return web.json_response({"error": "Missing data"}, status=400)
        
        user_id = _parse_user_id(init_data)
        if not user_id: return web.json_response({"error": "Cannot parse user"}, status=403)
        
        async with async_session() as session:
            from database import Favorite
            result = await session.execute(select(Favorite).where(Favorite.id == fav_id, Favorite.telegram_id == user_id))
            fav = result.scalar_one_or_none()
            if fav:
                await session.delete(fav)
                await session.commit()
                return web.json_response({"ok": True})
            return web.json_response({"error": "Not found"}, status=404)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_free_trial(request: web.Request) -> web.Response:
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        if not init_data:
            return web.json_response({"error": "No initData"}, status=400)

        user_data = validate_telegram_init_data(init_data)
        if not user_data:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        user_id = int(user_data["user_id"])
        tg_user = user_data.get("user") or {}

        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()

            if not user:
                user = User(
                    telegram_id=user_id,
                    username=tg_user.get("username"),
                    full_name=" ".join(filter(None, [tg_user.get("first_name"), tg_user.get("last_name")])) or None,
                    units=5,
                    trial_used=True,
                    is_active=True,
                )
                session.add(user)
                await session.commit()
                await session.refresh(user)
            else:
                if user.trial_used:
                    return web.json_response({"error": "Trial already used"}, status=409)
                if tg_user.get("username"):
                    user.username = tg_user.get("username")
                full_name = " ".join(filter(None, [tg_user.get("first_name"), tg_user.get("last_name")]))
                if full_name:
                    user.full_name = full_name
                user.units += 5
                user.trial_used = True
                user.is_active = True
                await session.commit()
                await session.refresh(user)

            invite_link = await _create_group_invite_for_user(user_id, store_pending=True)

            # Уведомление админу
            nick = f"@{user.username}" if getattr(user, 'username', None) else f"id{user_id}"
            await _notify_admins(
                f"🎁 <b>Новый триал (Mini App)</b>\n"
                f"👤 {nick} | <code>{user_id}</code>\n"
                f"📅 Активировал 5 бесплатных дней"
            )

            if invite_link:
                bot = Bot(token=BOT_TOKEN)
                try:
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    lang = user.lang or "en"
                    if lang == "ru":
                        text = (
                            "✅ <b>Бесплатный период активирован!</b>\n\n"
                            "📅 Добавлено: <b>5 дней</b>\n\n"
                            "👇 Нажмите кнопку ниже, чтобы вступить в закрытую группу.\n"
                            "<i>Ссылка одноразовая и действует 72 часа.</i>"
                        )
                        button_text = "🔗 Вступить в группу"
                    else:
                        text = (
                            "✅ <b>Free trial activated!</b>\n\n"
                            "📅 Added: <b>5 days</b>\n\n"
                            "👇 Tap the button below to join the private group.\n"
                            "<i>The link is one-time and expires in 72 hours.</i>"
                        )
                        button_text = "🔗 Join the group"
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text=button_text, url=invite_link)
                    ]])
                    await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
                except Exception as e:
                    logger.error(f"Cannot send trial invite to user {user_id}: {e}")
                finally:
                    await bot.session.close()

            return web.json_response({"ok": True, "days_left": user.units, "invite_link": invite_link})
    except Exception as e:
        logger.error(f"Error in api_free_trial: {e}")
        return web.json_response({"error": str(e)}, status=500)

async def api_get_videos(request: web.Request) -> web.Response:
    """GET /miniapp/videos?limit=N — full video catalog, newest first.

    The CSV `tags` column can carry a 'shorts' label even on a
    content_type='video' row (legacy ingest, repeats of shorts on the
    long-form catalog). We hide those here so the redesign's Home feed
    matches what /miniapp/tags already does for the Browse rail.
    Supports paging via ?offset=N&limit=M so the redesign Home can
    stream the catalog in 30-row chunks instead of blocking on a
    multi-second response. The two miniapps stay compatible: omitting
    offset defaults to 0, matching the old behaviour.
    """
    try:
        limit  = int(request.query.get('limit', 500))
        offset = max(0, int(request.query.get('offset', 0)))
        # New: ?order=random&seed=<int> gives a pseudo-shuffled view of
        # the whole catalog (not just newest), stable across pages for
        # one seed so offset paging still works. Used by the redesign
        # Home "For you" feed to surface old artists, not just today's
        # uploads.
        order  = request.query.get('order', 'newest').lower()
        try:
            seed = int(request.query.get('seed', 0))
        except (TypeError, ValueError):
            seed = 0
        async with async_session() as session:
            q = (
                select(ArtistContent)
                .where(ArtistContent.content_type == "video")
                # Exclude rows tagged with 'shorts' anywhere in the CSV.
                # tags can be NULL, 'shorts', 'shorts,whisper', 'sleep, shorts'
                # — the case-insensitive LIKE catches all three. NULL rows
                # pass through unaffected.
                .where(
                    sa_or(
                        ArtistContent.tags.is_(None),
                        ~ArtistContent.tags.ilike('%shorts%'),
                    )
                )
            )
            if order == 'random':
                # md5(id::text || seed) gives a deterministic shuffle per
                # seed — pagination over the same seed yields disjoint
                # pages, so the user never sees a duplicate while
                # scrolling. A new seed (fresh app open) gets a brand
                # new order. seed is a server-supplied int — no
                # injection risk — but we still bind it as a parameter
                # for safety.
                from sqlalchemy import bindparam
                q = q.order_by(
                    sa_text("md5(id::text || :__shuffle_seed)").bindparams(
                        __shuffle_seed=str(seed)
                    )
                )
            else:
                q = q.order_by(ArtistContent.created_at.desc())
            q = q.offset(offset).limit(limit)
            result = await session.execute(q)
            videos = result.scalars().all()
            videos_data = [_content_meta(v) for v in videos]
            return web.json_response({
                "videos": videos_data,
                "total":  len(videos_data),
                "offset": offset,
                "limit":  limit,
                "order":  order,
                "seed":   seed if order == 'random' else 0,
            })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_get_custom_badges(request: web.Request) -> web.Response:
    try:
        async with async_session() as session:
            badges = await get_custom_badges(session)
            return web.json_response({"badges": [{"name": b.name, "color": b.color} for b in badges]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def api_get_artist_content(request: web.Request) -> web.Response:
    """GET /miniapp/artist_content?name=X&type=video|photo|short&offset=0&limit=10&tag=TAG
    Without type param: returns first page of all three types (initial load).
    With type param: returns paginated list of that type."""
    q           = request.rel_url.query
    artist_name = q.get("name", "").strip()
    content_type= q.get("type", "").strip()      # video | photo | short | "" (all)
    offset      = max(0, int(q.get("offset", 0)))
    tag_filter  = q.get("tag", "").strip()

    LIMITS = {"video": 10, "short": 9, "photo": 15}

    if not artist_name:
        return web.json_response({"error": "name required"}, status=400)

    from sqlalchemy import select, and_
    async with async_session() as session:

        async def _fetch(ctype: str, lim: int, off: int, tag: str = ""):
            q2 = select(ArtistContent).where(
                and_(ArtistContent.artist_name == artist_name,
                     ArtistContent.content_type == ctype)
            )
            if tag:
                q2 = q2.where(ArtistContent.tags.ilike(f"%{tag}%"))
            q2 = q2.order_by(ArtistContent.created_at.desc(), ArtistContent.id.desc())
            # Fetch lim+1 to know if there are more
            q2 = q2.offset(off).limit(lim + 1)
            result = await session.execute(q2)
            rows = list(result.scalars().all())
            has_more = len(rows) > lim
            return rows[:lim], has_more

        if content_type in ("video", "short", "photo"):
            # Paginated single-type request
            lim  = LIMITS.get(content_type, 10)
            rows, has_more = await _fetch(content_type, lim, offset, tag_filter)

            def _fmt_item(v):
                return _content_meta(v)

            return web.json_response({
                "type": content_type, "offset": offset,
                "has_more": has_more, "items": [_fmt_item(v) for v in rows]
            })

        else:
            # Initial load — first page of all three
            videos, v_more = await _fetch("video", LIMITS["video"], 0)
            photos, p_more = await _fetch("photo", LIMITS["photo"], 0)
            shorts, s_more = await _fetch("short", LIMITS["short"], 0)
            return web.json_response({
                "artist": artist_name,
                "videos": [_content_meta(v) for v in videos],
                "videos_more": v_more,
                "photos": [_content_meta(p) for p in photos],
                "photos_more": p_more,
                "shorts": [_content_meta(s) for s in shorts],
                "shorts_more": s_more,
            })


async def api_content_play(request: web.Request) -> web.Response:
    """POST /miniapp/content/play — return a playable URL only for active users."""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        content_id = int(data.get("content_id", 0))
        if not user_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)

        async with async_session() as session:
            user_result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = user_result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)

            content_result = await session.execute(
                select(ArtistContent).where(ArtistContent.id == content_id)
            )
            content = content_result.scalar_one_or_none()
            if not content:
                return web.json_response({"error": "Not found"}, status=404)

            payload = _content_meta(content, include_url=True)
            return web.json_response(payload)
    except Exception as e:
        logger.error("content play error: %s", e)
        return web.json_response({"error": str(e)}, status=500)


async def api_get_recommended(request: web.Request) -> web.Response:
    """GET /miniapp/recommended?content_id=N&limit=8 — list of videos to play
    next. Strategy: 2 latest from the same artist + the rest are videos that
    share at least one tag with the source, excluding the source itself.
    Falls back to latest videos overall if nothing else matches."""
    q = request.rel_url.query
    try:
        cid = int(q.get('content_id', 0))
    except Exception:
        cid = 0
    limit = max(1, min(20, int(q.get('limit', 8) or 8)))

    async with async_session() as session:
        # Load the source row to read its artist + tags.
        source = None
        if cid:
            res = await session.execute(select(ArtistContent).where(ArtistContent.id == cid))
            source = res.scalar_one_or_none()

        picks = []
        seen_ids = {cid} if cid else set()

        # 2 latest from the same artist (excluding the source).
        if source and source.artist_name:
            same_artist = await session.execute(
                select(ArtistContent)
                .where(ArtistContent.artist_name == source.artist_name)
                .where(ArtistContent.content_type == 'video')
                .where(ArtistContent.id != cid)
                .order_by(ArtistContent.created_at.desc())
                .limit(2)
            )
            for v in same_artist.scalars().all():
                if v.id in seen_ids: continue
                seen_ids.add(v.id); picks.append(v)

        # Tag-similar (any tag overlap). Cheap LIKE since tags are stored as
        # a comma-separated string in this schema.
        if source and source.tags:
            from sqlalchemy import or_, func as _sf
            tag_terms = [t.strip() for t in (source.tags or '').split(',') if t.strip()]
            if tag_terms:
                clauses = [ArtistContent.tags.ilike(f"%{t}%") for t in tag_terms[:4]]
                sim = await session.execute(
                    select(ArtistContent)
                    .where(or_(*clauses))
                    .where(ArtistContent.content_type == 'video')
                    .where(ArtistContent.id != cid)
                    .order_by(_sf.random())
                    .limit(limit * 2)
                )
                for v in sim.scalars().all():
                    if v.id in seen_ids: continue
                    seen_ids.add(v.id); picks.append(v)
                    if len(picks) >= limit: break

        # Fallback: latest videos overall (excluding already-picked + source).
        if len(picks) < limit:
            q_latest = select(ArtistContent).where(ArtistContent.content_type == 'video')
            if seen_ids:
                q_latest = q_latest.where(~ArtistContent.id.in_(list(seen_ids)))
            q_latest = q_latest.order_by(ArtistContent.created_at.desc()).limit(limit * 2)
            latest = await session.execute(q_latest)
            for v in latest.scalars().all():
                if v.id in seen_ids: continue
                seen_ids.add(v.id); picks.append(v)
                if len(picks) >= limit: break

        return web.json_response({"items": [_content_meta(v) for v in picks[:limit]]})


async def api_get_video_by_id(request: web.Request) -> web.Response:
    """GET /miniapp/video/{id} — single content row by id. Lets the frontend
    open any video/short/photo without depending on whether it's in the
    /miniapp/videos?limit=500 page — important once the catalog passes a few
    thousand items."""
    try:
        cid = int(request.match_info.get("id", 0))
    except Exception:
        return web.json_response({"error": "Bad id"}, status=400)
    if not cid:
        return web.json_response({"error": "Bad id"}, status=400)
    async with async_session() as session:
        res = await session.execute(select(ArtistContent).where(ArtistContent.id == cid))
        item = res.scalar_one_or_none()
        if not item:
            return web.json_response({"error": "Not found"}, status=404)
        return web.json_response(_content_meta(item))


async def api_post_view(request: web.Request) -> web.Response:
    """POST /miniapp/view {initData, content_id} — register one view.
    Counted at most once per (user, content) thanks to the content_views
    composite primary key + ON CONFLICT DO NOTHING; we only bump the
    aggregate ArtistContent.views counter when the insert actually wrote
    a new row."""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        content_id = data.get("content_id")
        try: content_id = int(content_id)
        except Exception: content_id = 0
        if not user_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)
        from sqlalchemy import text as _t
        async with async_session() as session:
            # Insert; rowcount tells us whether this user had already viewed.
            res = await session.execute(
                _t("INSERT INTO content_views (telegram_id, content_id) VALUES (:uid, :cid) "
                   "ON CONFLICT DO NOTHING"),
                {"uid": user_id, "cid": content_id}
            )
            counted = (res.rowcount or 0) > 0
            if counted:
                await session.execute(
                    _t("UPDATE artist_content SET views = COALESCE(views, 0) + 1 WHERE id = :cid"),
                    {"cid": content_id}
                )
            await session.commit()
            v = await session.execute(
                _t("SELECT views FROM artist_content WHERE id = :cid"),
                {"cid": content_id}
            )
            views = v.scalar() or 0
        return web.json_response({"ok": True, "counted": counted, "views": int(views)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_get_shorts(request: web.Request) -> web.Response:
    """GET /miniapp/shorts?limit=20 — latest shorts for home page scroll"""
    try:
        limit = int(request.query.get('limit', 20))
        async with async_session() as session:
            result = await session.execute(
                select(ArtistContent)
                .where(ArtistContent.content_type == "short")
                .order_by(sa_func.random())
                .limit(limit)
            )
            shorts = result.scalars().all()
        return web.json_response({"shorts": [_content_meta(s) for s in shorts]})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_watch_progress(request: web.Request) -> web.Response:
    """POST /miniapp/watch_progress — save/update watch position"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data      = await request.json()
        content_id = int(data.get("content_id", 0))
        progress   = int(data.get("progress_seconds", 0))
        duration   = int(data.get("duration_seconds", 0))
    except Exception:
        return web.json_response({"error": "Bad body"}, status=400)

    user_id = user_data["user_id"]
    if not user_id or not content_id:
        return web.json_response({"ok": False})

    from sqlalchemy import text as _t
    async with async_session() as session:
        await session.execute(_t("""
            INSERT INTO watch_history (telegram_id, content_id, progress_seconds, duration_seconds, updated_at)
            VALUES (:uid, :cid, :prog, :dur, NOW())
            ON CONFLICT (telegram_id, content_id)
            DO UPDATE SET progress_seconds=:prog, duration_seconds=:dur, updated_at=NOW()
        """), {"uid": user_id, "cid": content_id, "prog": progress, "dur": duration})
        await session.commit()
    return web.json_response({"ok": True})


async def api_continue_watching(request: web.Request) -> web.Response:
    """POST /miniapp/continue_watching — get user's in-progress videos"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)

    user_id = user_data["user_id"]

    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(_t("""
            SELECT wh.content_id, wh.progress_seconds, wh.duration_seconds,
                   ac.title, ac.url, ac.thumbnail_url, ac.artist_name, ac.tags
            FROM watch_history wh
            JOIN artist_content ac ON ac.id = wh.content_id
            WHERE wh.telegram_id = :uid
              AND wh.progress_seconds > 0
              AND wh.duration_seconds > 30
              AND CAST(wh.progress_seconds AS FLOAT) / NULLIF(wh.duration_seconds,0) BETWEEN 0.03 AND 0.92
            ORDER BY wh.updated_at DESC
            LIMIT 10
        """), {"uid": user_id})
        rows = result.mappings().all()

    return web.json_response({"items": [
        {"id": r["content_id"], "title": r["title"] or "",
         "artist_name": r["artist_name"] or "",
         "thumbnail_url": r["thumbnail_url"] or "",
         "tags": r["tags"] or "",
         "progress": r["progress_seconds"], "duration": r["duration_seconds"]}
        for r in rows
    ]})


async def api_search(request: web.Request) -> web.Response:
    """GET /miniapp/search?q=TEXT&limit=20&order=newest|random&seed=N

    `order=random` returns a deterministic shuffle of all rows matching
    the query (md5(id::text || seed)) — same pattern as /miniapp/videos.
    Used by the Home → Browse category page so tapping a tag surfaces a
    fresh sample each time the user taps Refresh.
    Default order is newest-first, so the free-text search path stays
    unchanged.
    """
    q     = request.query.get("q", "").strip()
    limit = min(int(request.query.get("limit", 20)), 50)
    order = request.query.get("order", "newest").lower()
    try:
        seed = int(request.query.get("seed", 0))
    except (TypeError, ValueError):
        seed = 0
    if len(q) < 2:
        return web.json_response({"results": []})

    from sqlalchemy import or_
    async with async_session() as session:
        stmt = (
            select(ArtistContent)
            .where(ArtistContent.content_type == "video")
            .where(or_(
                ArtistContent.title.ilike(f"%{q}%"),
                ArtistContent.artist_name.ilike(f"%{q}%"),
                ArtistContent.tags.ilike(f"%{q}%"),
            ))
        )
        if order == "random":
            stmt = stmt.order_by(
                sa_text("md5(id::text || :__shuffle_seed)").bindparams(
                    __shuffle_seed=str(seed)
                )
            )
        else:
            stmt = stmt.order_by(ArtistContent.created_at.desc())
        result = await session.execute(stmt.limit(limit))
        items = result.scalars().all()

    return web.json_response({"results": [
        _content_meta(v)
        for v in items
    ]})


async def api_toggle_follow(request: web.Request) -> web.Response:
    """POST /miniapp/follow — toggle artist follow, returns {following: bool, count: int}"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    try:
        data        = await request.json()
        artist_name = data.get("artist_name", "").strip()
    except Exception:
        return web.json_response({"error": "Bad body"}, status=400)
    if not artist_name:
        return web.json_response({"error": "artist_name required"}, status=400)

    user_id = user_data["user_id"]

    from sqlalchemy import text as _t
    async with async_session() as session:
        existing = await session.execute(
            _t("SELECT id FROM artist_follows WHERE telegram_id=:uid AND artist_name=:name"),
            {"uid": user_id, "name": artist_name}
        )
        row = existing.scalar_one_or_none()
        if row:
            await session.execute(
                _t("DELETE FROM artist_follows WHERE telegram_id=:uid AND artist_name=:name"),
                {"uid": user_id, "name": artist_name}
            )
            following = False
        else:
            await session.execute(
                _t("INSERT INTO artist_follows (telegram_id, artist_name) VALUES (:uid, :name) ON CONFLICT DO NOTHING"),
                {"uid": user_id, "name": artist_name}
            )
            following = True
        await session.commit()
        count_row = await session.execute(
            _t("SELECT COUNT(*) FROM artist_follows WHERE telegram_id=:uid"), {"uid": user_id}
        )
        count = count_row.scalar() or 0
    return web.json_response({"following": following, "count": int(count)})


async def api_get_follows(request: web.Request) -> web.Response:
    """POST /miniapp/follows — list of followed artist names + their recent video"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    user_id = user_data["user_id"]

    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(
            _t("SELECT af.artist_name, a.photo_url, a.profile_photo_url FROM artist_follows af "
               "LEFT JOIN artists a ON a.name = af.artist_name "
               "WHERE af.telegram_id=:uid ORDER BY af.created_at DESC"),
            {"uid": user_id}
        )
        rows = result.mappings().all()
    return web.json_response({"artists": [
        {"name": r["artist_name"], "photo_url": r["photo_url"] or "", "profile_photo_url": r["profile_photo_url"] or ""}
        for r in rows
    ]})


async def api_followed_feed(request: web.Request) -> web.Response:
    """POST /miniapp/followed_feed — random videos from followed artists.

    The redesign's Home rail wanted a varied sample (so the same drop
    doesn't sit on top every refresh), not strictly the newest 20.
    ORDER BY RANDOM() shuffles per-request — cheap enough at the
    artist_content scale we're running.

    Filters out shorts-tagged rows like /miniapp/videos does, so the
    rail mirrors what the user sees elsewhere in the app.
    """
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    user_id = user_data["user_id"]

    from sqlalchemy import text as _t
    async with async_session() as session:
        result = await session.execute(_t("""
            SELECT ac.id, ac.title, ac.url, ac.thumbnail_url, ac.artist_name, ac.tags, ac.created_at
            FROM artist_content ac
            WHERE ac.content_type = 'video'
              AND ac.artist_name IN (
                SELECT artist_name FROM artist_follows WHERE telegram_id = :uid
              )
              AND (ac.tags IS NULL OR ac.tags NOT ILIKE '%shorts%')
            ORDER BY RANDOM()
            LIMIT 30
        """), {"uid": user_id})
        rows = result.mappings().all()
    return web.json_response({"videos": [
        {"id": r["id"], "title": r["title"] or "",
         "thumbnail_url": r["thumbnail_url"] or "",
         "artist_name": r["artist_name"] or "", "tags": r["tags"] or "",
         "created_at": r["created_at"].isoformat() if r["created_at"] else ""}
        for r in rows
    ]})


async def api_user_stats(request: web.Request) -> web.Response:
    """POST /miniapp/user_stats — user activity stats"""
    user_data = await parse_init_data(request)
    if not user_data:
        return web.json_response({"error": "Invalid request"}, status=400)
    user_id = user_data["user_id"]

    from sqlalchemy import text as _t
    async with async_session() as session:
        follows   = (await session.execute(_t("SELECT COUNT(*) FROM artist_follows WHERE telegram_id=:uid"),   {"uid": user_id})).scalar() or 0
        favorites = (await session.execute(_t("SELECT COUNT(*) FROM favorites WHERE telegram_id=:uid"),         {"uid": user_id})).scalar() or 0
        reactions = (await session.execute(_t("SELECT COUNT(*) FROM video_reactions WHERE telegram_id=:uid"),   {"uid": user_id})).scalar() or 0
        comments  = (await session.execute(_t("SELECT COUNT(*) FROM video_comments WHERE telegram_id=:uid"),    {"uid": user_id})).scalar() or 0
        user_row  = (await session.execute(_t("SELECT created_at FROM users WHERE telegram_id=:uid"),           {"uid": user_id})).mappings().one_or_none()
        # Top artist from follows
        top_artist_row = (await session.execute(_t(
            "SELECT artist_name FROM artist_follows WHERE telegram_id=:uid ORDER BY created_at ASC LIMIT 1"
        ), {"uid": user_id})).scalar_one_or_none()
    member_since = user_row["created_at"].strftime("%b %Y") if user_row and user_row["created_at"] else ""
    return web.json_response({"stats": {
        "follows": int(follows), "favorites": int(favorites),
        "reactions": int(reactions), "comments": int(comments),
        "top_artist": top_artist_row or "",
        "member_since": member_since,
    }})


async def api_get_tags(request: web.Request) -> web.Response:
    """GET /miniapp/tags — все теги с цветами и счётчиком видео.

    Каждый тег теперь возвращается с полем `count` = сколько единиц
    контента (видео + шортсы) ссылаются на этот тег. Список отсортирован
    по count DESC, потом по name ASC — самый популярный тег (Licking
    и т.п.) первым. Старые клиенты, которые читают только name/color,
    дополнительное поле просто игнорируют — обратная совместимость
    сохранена.
    """
    from sqlalchemy import text as _t
    async with async_session() as session:
        tags = await get_all_tags(session)
        # Парсим CSV из artist_content.tags для видео и шортсов и
        # собираем словарь tag → count. Один и тот же паттерн
        # используется в handlers/admin.py для статистики.
        tag_rows = (await session.execute(_t(
            "SELECT tags FROM artist_content "
            "WHERE content_type IN ('video', 'short') "
            "AND tags IS NOT NULL AND tags != ''"
        ))).scalars().all()
        counts: dict[str, int] = {}
        for tags_str in tag_rows:
            for t in tags_str.split(','):
                t = t.strip()
                if t:
                    counts[t] = counts.get(t, 0) + 1
        # Технический тег "shorts" не нужен в Browse — он не описывает
        # содержимое, а лишь дублирует content_type. Прячем его из ответа.
        out = [{
            "name":  t.name,
            "color": t.color,
            "count": counts.get(t.name, 0),
        } for t in tags if t.name.strip().lower() != 'shorts']
        out.sort(key=lambda x: (-x["count"], x["name"]))
        return web.json_response({"tags": out})


async def api_get_video_reactions(request: web.Request) -> web.Response:
    """GET /miniapp/video/{id}/reactions?initData=..."""
    content_id = int(request.match_info.get("id", 0))
    init_data = request.rel_url.query.get("initData", "")
    user_id = _parse_user_id(init_data) if init_data else None

    async with async_session() as session:
        counts = await get_reactions(session, content_id)
        user_reactions = []
        if user_id:
            user_reactions = await get_user_reactions(session, content_id, user_id)
        return web.json_response({
            "content_id": content_id,
            "counts": counts,
            "user_reactions": user_reactions,   # list of emojis
            "allowed": ALLOWED_REACTIONS,
            "max_per_user": 3,
        })


async def api_post_reaction(request: web.Request) -> web.Response:
    """POST /miniapp/video/{id}/react  body: {initData, emoji}"""
    content_id = int(request.match_info.get("id", 0))
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        emoji = data.get("emoji", "")
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        if emoji not in ALLOWED_REACTIONS:
            return web.json_response({"error": "Invalid emoji"}, status=400)

        async with async_session() as session:
            # verify subscription
            from sqlalchemy import select as sa_select
            result = await session.execute(sa_select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)
            counts = await set_reaction(session, content_id, user_id, emoji)
            user_reactions = await get_user_reactions(session, content_id, user_id)
            return web.json_response({"counts": counts, "user_reactions": user_reactions})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_get_comments(request: web.Request) -> web.Response:
    """GET /miniapp/video/{id}/comments"""
    content_id = int(request.match_info.get("id", 0))
    async with async_session() as session:
        comments = await get_comments(session, content_id)

        # Batch-load badges for all commenters
        tg_ids = list({c.telegram_id for c in comments})
        badge_map: dict[int, list[str]] = {}
        if tg_ids:
            result = await session.execute(
                select(User.telegram_id, User.badge).where(User.telegram_id.in_(tg_ids))
            )
            for tid, raw in result.all():
                badge_map[tid] = [b.strip() for b in (raw or "").split(",") if b.strip()]

        return web.json_response({
            "comments": [
                {
                    "id": c.id,
                    "username": c.username or "Anonymous",
                    "photo_url": c.photo_url or "",
                    "text": c.text,
                    "created_at": c.created_at.strftime("%d.%m %H:%M"),
                    "badges": badge_map.get(c.telegram_id, [])[:3],
                }
                for c in reversed(comments)
            ]
        })


async def api_post_comment(request: web.Request) -> web.Response:
    """POST /miniapp/video/{id}/comment  body: {initData, text}"""
    content_id = int(request.match_info.get("id", 0))
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        text = data.get("text", "").strip()
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        if not text:
            return web.json_response({"error": "Empty comment"}, status=400)

        # Extract photo_url from initData user object
        photo_url = None
        try:
            params = dict(urllib.parse.parse_qsl(init_data))
            user_obj = json.loads(params.get('user', '{}'))
            photo_url = user_obj.get('photo_url') or None
        except Exception:
            pass

        async with async_session() as session:
            from sqlalchemy import select as sa_select
            result = await session.execute(sa_select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)
            comment = await add_comment(session, content_id, user_id, user.username, text, photo_url)
            return web.json_response({
                "ok": True,
                "comment": {
                    "id": comment.id,
                    "username": comment.username or "Anonymous",
                    "photo_url": comment.photo_url or "",
                    "text": comment.text,
                    "created_at": comment.created_at.strftime("%d.%m %H:%M"),
                }
            })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_suggest_artist(request: web.Request) -> web.Response:
    """POST /miniapp/suggest_artist {initData, artist_name}"""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        artist_name = data.get("artist_name", "").strip()
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        if not artist_name or len(artist_name) > 256:
            return web.json_response({"error": "Invalid artist name"}, status=400)

        # Get username
        params = dict(urllib.parse.parse_qsl(init_data))
        username = None
        try:
            username = json.loads(params.get('user', '{}')).get('username')
        except Exception:
            pass

        async with async_session() as session:
            sug = ArtistSuggestion(telegram_id=user_id, username=username, artist_name=artist_name)
            session.add(sug)
            await session.commit()

        # Notify admins via bot
        nick = f"@{username}" if username else f"id{user_id}"
        await _notify_admins(
            f"💡 <b>Предложение артиста</b>\n"
            f"👤 {nick} | <code>{user_id}</code>\n"
            f"🎤 <b>{artist_name}</b>"
        )
        return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_toggle_favorite(request: web.Request) -> web.Response:
    """POST /miniapp/favorites/toggle {initData, content_id, title, url}"""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        content_id = data.get("content_id")
        user_id = _parse_user_id(init_data)
        if not user_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)

        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)

            # Check if already favorited
            result = await session.execute(
                select(Favorite).where(Favorite.telegram_id == user_id, Favorite.content_id == content_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                await session.delete(existing)
                await session.commit()
                return web.json_response({"ok": True, "favorited": False})
            else:
                content_result = await session.execute(
                    select(ArtistContent).where(ArtistContent.id == content_id)
                )
                content = content_result.scalar_one_or_none()
                if not content:
                    return web.json_response({"error": "Content not found"}, status=404)
                # Check limit
                from sqlalchemy import func as sa_func
                count_result = await session.execute(
                    select(sa_func.count(Favorite.id)).where(Favorite.telegram_id == user_id)
                )
                if (count_result.scalar() or 0) >= 200:
                    return web.json_response({"error": "Limit reached"}, status=409)
                fav = Favorite(
                    telegram_id=user_id,
                    content_id=content_id,
                    title=(content.title or "Video")[:128],
                    url=content.url,
                )
                session.add(fav)
                await session.commit()
                return web.json_response({"ok": True, "favorited": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_check_favorite(request: web.Request) -> web.Response:
    """GET /miniapp/favorites/check?initData=...&content_id=..."""
    init_data = request.rel_url.query.get("initData", "")
    content_id = request.rel_url.query.get("content_id", "")
    user_id = _parse_user_id(init_data) if init_data else None
    if not user_id or not content_id:
        return web.json_response({"favorited": False})
    async with async_session() as session:
        result = await session.execute(
            select(Favorite).where(Favorite.telegram_id == user_id, Favorite.content_id == int(content_id))
        )
        return web.json_response({"favorited": result.scalar_one_or_none() is not None})


async def api_get_favorites_v2(request: web.Request) -> web.Response:
    """POST /miniapp/favorites  — returns favorites with content details"""
    try:
        data = await request.json()
        init_data = data.get("initData", "")
        user_id = _parse_user_id(init_data)
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)

        async with async_session() as session:
            result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = result.scalar_one_or_none()
            if not user or user.units <= 0:
                return web.json_response({"error": "No subscription"}, status=403)

            result = await session.execute(
                select(Favorite).where(Favorite.telegram_id == user_id).order_by(Favorite.created_at.desc())
            )
            favorites = result.scalars().all()

            # Enrich with content details
            items = []
            content_ids = [f.content_id for f in favorites if f.content_id]
            content_map = {}
            if content_ids:
                cr = await session.execute(
                    select(ArtistContent).where(ArtistContent.id.in_(content_ids))
                )
                for c in cr.scalars().all():
                    content_map[c.id] = c

            for f in favorites:
                item = {"id": f.id, "title": f.title, "content_id": f.content_id}
                if f.content_id and f.content_id in content_map:
                    c = content_map[f.content_id]
                    item.update({
                        "artist_name": c.artist_name,
                        "thumbnail_url": c.thumbnail_url or "",
                        "tags": c.tags or "",
                        # New field — additive, safe for old clients (they
                        # ignore it). New redesign uses this to split the
                        # Saved tab into "Liked videos" vs "Liked shorts".
                        "content_type": c.content_type or "",
                    })
                items.append(item)
            return web.json_response({"items": items, "count": len(items), "limit": 200})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── Playlists API ────────────────────────────────────────────────────────────

async def api_get_playlists(request: web.Request) -> web.Response:
    """POST /miniapp/playlists {initData}

    Returns playlists with item_count + up to 4 preview thumbnail URLs from
    the most-recently-added items. The thumbs are used by the Saved →
    Playlists 2x2 tile grid so the cards look like real collections rather
    than colored gradients.
    """
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        if not user_id:
            return web.json_response({"error": "Cannot parse user"}, status=403)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.telegram_id == user_id).order_by(Playlist.created_at.desc())
            )
            playlists = result.scalars().all()
            from sqlalchemy import func as sa_func
            items_out = []
            for pl in playlists:
                count_r = await session.execute(
                    select(sa_func.count(PlaylistItem.id)).where(PlaylistItem.playlist_id == pl.id)
                )
                # Newest 4 items in this playlist, then look up their
                # thumbnail URLs from ArtistContent.
                pi_r = await session.execute(
                    select(PlaylistItem.content_id)
                    .where(PlaylistItem.playlist_id == pl.id)
                    .order_by(PlaylistItem.created_at.desc())
                    .limit(4)
                )
                cids = [row[0] for row in pi_r.all() if row[0]]
                thumbs = []
                if cids:
                    cr = await session.execute(
                        select(ArtistContent.id, ArtistContent.thumbnail_url, ArtistContent.url)
                        .where(ArtistContent.id.in_(cids))
                    )
                    thumb_by_id = {row[0]: (row[1] or row[2] or '') for row in cr.all()}
                    # Preserve newest-first order from the PlaylistItem query.
                    thumbs = [thumb_by_id.get(c, '') for c in cids]
                items_out.append({
                    "id": pl.id, "name": pl.name,
                    "item_count": count_r.scalar() or 0,
                    "created_at": pl.created_at.strftime("%d.%m.%Y"),
                    "thumbs": thumbs,
                })
            return web.json_response({"playlists": items_out})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_create_playlist(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/create {initData, name}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        name = data.get("name", "").strip()
        if not user_id or not name:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            # Limit 20 playlists
            from sqlalchemy import func as sa_func
            count_r = await session.execute(
                select(sa_func.count(Playlist.id)).where(Playlist.telegram_id == user_id)
            )
            if (count_r.scalar() or 0) >= 20:
                return web.json_response({"error": "Max 20 playlists"}, status=409)
            pl = Playlist(telegram_id=user_id, name=name[:128])
            session.add(pl)
            await session.commit()
            await session.refresh(pl)
            return web.json_response({"ok": True, "id": pl.id, "name": pl.name})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_delete_playlist(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/delete {initData, playlist_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        if not user_id or not playlist_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            pl = result.scalar_one_or_none()
            if not pl:
                return web.json_response({"error": "Not found"}, status=404)
            from sqlalchemy import delete as sa_delete
            await session.execute(sa_delete(PlaylistItem).where(PlaylistItem.playlist_id == pl.id))
            await session.delete(pl)
            await session.commit()
            return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_playlist_add_item(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/add_item {initData, playlist_id, content_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        content_id = data.get("content_id")
        if not user_id or not playlist_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            if not result.scalar_one_or_none():
                return web.json_response({"error": "Not found"}, status=404)
            # Check duplicate
            result = await session.execute(
                select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.content_id == content_id)
            )
            if result.scalar_one_or_none():
                return web.json_response({"error": "Already in playlist"}, status=409)
            item = PlaylistItem(playlist_id=playlist_id, content_id=content_id)
            session.add(item)
            await session.commit()
            return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_playlist_remove_item(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/remove_item {initData, playlist_id, content_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        content_id = data.get("content_id")
        if not user_id or not playlist_id or not content_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            if not result.scalar_one_or_none():
                return web.json_response({"error": "Not found"}, status=404)
            from sqlalchemy import delete as sa_delete
            await session.execute(
                sa_delete(PlaylistItem).where(
                    PlaylistItem.playlist_id == playlist_id,
                    PlaylistItem.content_id == content_id,
                )
            )
            await session.commit()
            return web.json_response({"ok": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_playlist_items(request: web.Request) -> web.Response:
    """POST /miniapp/playlists/items {initData, playlist_id}"""
    try:
        data = await request.json()
        user_id = _parse_user_id(data.get("initData", ""))
        playlist_id = data.get("playlist_id")
        if not user_id or not playlist_id:
            return web.json_response({"error": "Missing data"}, status=400)
        async with async_session() as session:
            result = await session.execute(
                select(Playlist).where(Playlist.id == playlist_id, Playlist.telegram_id == user_id)
            )
            pl = result.scalar_one_or_none()
            if not pl:
                return web.json_response({"error": "Not found"}, status=404)
            result = await session.execute(
                select(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id).order_by(PlaylistItem.created_at.desc())
            )
            p_items = result.scalars().all()
            content_ids = [i.content_id for i in p_items]
            content_map = {}
            if content_ids:
                cr = await session.execute(select(ArtistContent).where(ArtistContent.id.in_(content_ids)))
                for c in cr.scalars().all():
                    content_map[c.id] = c
            items_out = []
            for pi in p_items:
                c = content_map.get(pi.content_id)
                if c:
                    items_out.append({
                        "id": c.id, "title": c.title or "",
                        "artist_name": c.artist_name, "tags": c.tags or "",
                        "thumbnail_url": c.thumbnail_url or "",
                    })
            return web.json_response({"playlist": {"id": pl.id, "name": pl.name}, "items": items_out})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def api_health(request: web.Request) -> web.Response:
    """GET /health — lightweight DB liveness probe for the miniapp/site.
    Returns 200 {status:'ok'} when the DB answers SELECT 1, 503 otherwise.
    Never raises — frontend uses it as a fast yes/no check during boot.
    """
    try:
        async with async_session() as session:
            await session.execute(sa_text("SELECT 1"))
        return web.json_response({"status": "ok"})
    except Exception as e:
        logger.warning(f"/health DB probe failed: {e}")
        return web.json_response({"status": "db_error"}, status=503)


def create_app() -> web.Application:
    app = web.Application()
    app.middlewares.append(cors_middleware)
    app.router.add_get("/health", api_health)
    app.router.add_post("/tribute-webhook", tribute_webhook)
    app.router.add_post("/internal/grant_access", internal_grant_access)
    app.router.add_post("/internal/access_status", internal_access_status)
    app.router.add_post("/internal/refresh_invite", internal_refresh_invite)
    app.router.add_post("/miniapp/create_stars_invoice", api_create_stars_invoice)
    app.router.add_post("/miniapp/prepare_bundle_checkout", api_prepare_bundle_checkout)
    app.router.add_post("/miniapp/access_links", api_access_links)
    app.router.add_post("/miniapp/access_links/refresh", api_refresh_access_link)
    app.router.add_post("/miniapp/promo/activate", api_promo_activate)
    app.router.add_post("/miniapp/promo/apply", api_promo_activate)
    app.router.add_post("/miniapp/promo/validate", api_promo_activate)
    app.router.add_post("/miniapp/check_invite", api_check_invite)
    app.router.add_post("/miniapp/my_invite", api_my_invite)
    app.router.add_get("/miniapp/artists", api_get_artists)
    app.router.add_get("/miniapp/videos", api_get_videos)
    app.router.add_get("/miniapp/custom_badges", api_get_custom_badges)
    app.router.add_post("/miniapp/profile", api_get_profile)
    app.router.add_post("/miniapp/set_language", api_set_language)
    app.router.add_post("/miniapp/set_notify_expiry", api_set_notify_expiry)
    app.router.add_post("/miniapp/favorites", api_get_favorites_v2)
    app.router.add_post("/miniapp/favorites/add", api_add_favorite)
    app.router.add_post("/miniapp/favorites/delete", api_delete_favorite)
    app.router.add_post("/miniapp/favorites/toggle", api_toggle_favorite)
    app.router.add_get("/miniapp/favorites/check", api_check_favorite)
    app.router.add_post("/miniapp/free_trial", api_free_trial)
    app.router.add_post("/miniapp/suggest_artist", api_suggest_artist)
    app.router.add_post("/miniapp/playlists", api_get_playlists)
    app.router.add_post("/miniapp/playlists/create", api_create_playlist)
    app.router.add_post("/miniapp/playlists/delete", api_delete_playlist)
    app.router.add_post("/miniapp/playlists/add_item", api_playlist_add_item)
    app.router.add_post("/miniapp/playlists/remove_item", api_playlist_remove_item)
    app.router.add_post("/miniapp/playlists/items", api_playlist_items)
    app.router.add_get("/miniapp/artist_content", api_get_artist_content)
    app.router.add_post("/miniapp/content/play", api_content_play)
    app.router.add_post("/miniapp/view", api_post_view)
    app.router.add_get("/miniapp/video/{id:[0-9]+}", api_get_video_by_id)
    app.router.add_get("/miniapp/recommended", api_get_recommended)
    app.router.add_post("/miniapp/watch_progress", api_watch_progress)
    app.router.add_post("/miniapp/continue_watching", api_continue_watching)
    app.router.add_get("/miniapp/search", api_search)
    app.router.add_post("/miniapp/follow", api_toggle_follow)
    app.router.add_post("/miniapp/follows", api_get_follows)
    app.router.add_post("/miniapp/followed_feed", api_followed_feed)
    app.router.add_post("/miniapp/user_stats", api_user_stats)
    app.router.add_get("/miniapp/shorts", api_get_shorts)
    app.router.add_get("/miniapp/tags", api_get_tags)
    app.router.add_get("/miniapp/video/{id}/reactions", api_get_video_reactions)
    app.router.add_post("/miniapp/video/{id}/react", api_post_reaction)
    app.router.add_get("/miniapp/video/{id}/comments", api_get_comments)
    app.router.add_post("/miniapp/video/{id}/comment", api_post_comment)
    return app
