import asyncio
import logging
from datetime import datetime, timedelta

from aiohttp import ClientSession, ClientTimeout
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BUNDLE_PEERS, BUNDLE_TARGETS, GROUP_ID, INVITE_LINK, PROJECT_KEY

logger = logging.getLogger(__name__)

SOURCE_PROJECT = "asmrleaks"
SOURCE_LABEL = "ASMR.LEAKS"


def bundle_projects_for_tier(tier: str, target_project: str | None = None) -> list[str]:
    safe_tier = (tier or "").lower()
    safe_target = (target_project or "").lower()
    if safe_tier == "elite":
        if safe_target not in BUNDLE_TARGETS:
            raise ValueError("ELITE requires a valid second page")
        return [safe_target]
    if safe_tier == "king":
        return list(BUNDLE_TARGETS)
    raise ValueError("Only ELITE and KING are bundle tiers")


async def _send_peer_grant(
    session: ClientSession,
    *,
    project: str,
    telegram_id: int,
    order_uuid: str,
    username: str | None,
    full_name: str | None,
    days: int,
    tier: str,
) -> dict:
    peer = BUNDLE_PEERS.get(project) or {}
    grant_url = (peer.get("grant_url") or "").strip()
    grant_secret = (peer.get("grant_secret") or "").strip()
    label = peer.get("label") or project
    if not grant_url or not grant_secret:
        return {"ok": False, "project": project, "label": label, "error": "not configured"}

    payload = {
        "source_project": PROJECT_KEY or SOURCE_PROJECT,
        "order_uuid": order_uuid,
        "telegram_id": int(telegram_id),
        "days": int(days),
        "tier": tier,
        "username": username or "",
        "full_name": full_name or "",
        "payment_method": f"bundle_{tier}_sync",
        "silent": True,
    }
    headers = {
        "Authorization": f"Bearer {grant_secret}",
        "Content-Type": "application/json",
    }
    try:
        async with session.post(grant_url, json=payload, headers=headers) as response:
            try:
                data = await response.json(content_type=None)
            except Exception:
                data = {}
            if response.status >= 400:
                logger.warning("Bundle grant failed for %s: status=%s", project, response.status)
                return {"ok": False, "project": project, "label": label, "error": f"HTTP {response.status}"}
            return {
                "ok": True,
                "project": project,
                "label": label,
                "url": data.get("invite_link") or "",
                "days_left": data.get("days_left"),
                "applied": bool(data.get("applied", True)),
            }
    except Exception as exc:
        logger.warning("Bundle grant error for %s: %s", project, exc)
        return {"ok": False, "project": project, "label": label, "error": str(exc)}


async def refresh_peer_invite(*, project: str, telegram_id: int) -> dict:
    """Ask a peer to verify its own balance and issue a fresh invite."""
    peer = BUNDLE_PEERS.get(project) or {}
    grant_url = (peer.get("grant_url") or "").strip()
    grant_secret = (peer.get("grant_secret") or "").strip()
    label = peer.get("label") or project
    if not grant_url or not grant_secret:
        return {"ok": False, "project": project, "label": label, "error": "not configured"}

    endpoint = grant_url.rstrip("/").rsplit("/", 1)[0] + "/refresh_invite"
    headers = {
        "Authorization": f"Bearer {grant_secret}",
        "Content-Type": "application/json",
    }
    try:
        timeout = ClientTimeout(total=12)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                json={"telegram_id": int(telegram_id)},
                headers=headers,
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
                if response.status >= 400:
                    return {
                        "ok": False,
                        "project": project,
                        "label": label,
                        "active": bool(data.get("active")),
                        "days_left": int(data.get("days_left") or 0),
                        "error": data.get("error") or f"HTTP {response.status}",
                    }
                return {
                    "ok": True,
                    "project": project,
                    "label": label,
                    "active": True,
                    "days_left": int(data.get("days_left") or 0),
                    "tier": data.get("tier") or "plus",
                    "url": data.get("invite_link") or "",
                }
    except Exception as exc:
        logger.warning("Bundle invite refresh error for %s: %s", project, exc)
        return {"ok": False, "project": project, "label": label, "error": str(exc)}


async def get_peer_access_status(*, project: str, telegram_id: int) -> dict:
    """Read a peer's current balance without creating an invite or adding days."""
    peer = BUNDLE_PEERS.get(project) or {}
    grant_url = (peer.get("grant_url") or "").strip()
    grant_secret = (peer.get("grant_secret") or "").strip()
    label = peer.get("label") or project
    if not grant_url or not grant_secret:
        return {"ok": False, "project": project, "label": label, "error": "not configured"}

    endpoint = grant_url.rstrip("/").rsplit("/", 1)[0] + "/access_status"
    headers = {
        "Authorization": f"Bearer {grant_secret}",
        "Content-Type": "application/json",
    }
    try:
        timeout = ClientTimeout(total=8)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                json={"telegram_id": int(telegram_id)},
                headers=headers,
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {}
                if response.status >= 400:
                    return {
                        "ok": False,
                        "project": project,
                        "label": label,
                        "error": data.get("error") or f"HTTP {response.status}",
                    }
                return {
                    "ok": True,
                    "project": project,
                    "label": label,
                    "active": bool(data.get("active")),
                    "days_left": int(data.get("days_left") or 0),
                    "tier": data.get("tier") or "plus",
                }
    except Exception as exc:
        logger.warning("Bundle access status error for %s: %s", project, exc)
        return {"ok": False, "project": project, "label": label, "error": str(exc)}


async def activate_bundle_access(
    bot: Bot,
    *,
    telegram_id: int,
    order_uuid: str,
    username: str | None,
    full_name: str | None,
    days: int,
    local_days_left: int,
    tier: str,
    target_project: str | None,
) -> tuple[list[dict], list[dict]]:
    projects = bundle_projects_for_tier(tier, target_project)
    links: list[dict] = []
    failures: list[dict] = []

    source_invite = INVITE_LINK or ""
    try:
        if GROUP_ID:
            invite = await bot.create_chat_invite_link(
                GROUP_ID,
                member_limit=1,
                expire_date=int((datetime.utcnow() + timedelta(hours=72)).timestamp()),
            )
            source_invite = invite.invite_link
    except Exception as exc:
        logger.error("Cannot create ASMR bundle invite for %s: %s", telegram_id, exc)

    if source_invite:
        links.append({
            "project": SOURCE_PROJECT,
            "label": SOURCE_LABEL,
            "url": source_invite,
            "days_left": int(local_days_left),
        })
    else:
        failures.append({"project": SOURCE_PROJECT, "label": SOURCE_LABEL, "error": "invite unavailable"})

    timeout = ClientTimeout(total=12)
    async with ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(*[
            _send_peer_grant(
                session,
                project=project,
                telegram_id=telegram_id,
                order_uuid=order_uuid,
                username=username,
                full_name=full_name,
                days=days,
                tier=tier,
            )
            for project in projects
        ])

    for result in results:
        if result.get("ok") and result.get("url"):
            links.append({
                "project": result["project"],
                "label": result["label"],
                "url": result["url"],
                "days_left": result.get("days_left"),
            })
        else:
            failures.append(result)

    return links, failures


async def send_bundle_access_message(
    bot: Bot,
    *,
    telegram_id: int,
    tier: str,
    days: int,
    local_days_left: int,
    links: list[dict],
    lang: str = "en",
) -> None:
    rows = [
        [InlineKeyboardButton(text=f"Join {item.get('label') or item.get('project')}", url=item["url"])]
        for item in links
        if item.get("url")
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    tier_name = (tier or "").upper()
    if lang == "ru":
        text = (
            f"<b>{tier_name} подписка активирована!</b>\n\n"
            f"ASMR-доступ: <b>PRO</b>\n"
            f"Добавлено: <b>{days} дней</b>\n"
            f"Итого в ASMR: <b>{local_days_left} дней</b>\n\n"
            "Ссылки на все страницы доступны ниже. Каждая ссылка одноразовая и действует 72 часа."
        )
    else:
        text = (
            f"<b>{tier_name} subscription activated!</b>\n\n"
            f"ASMR access: <b>PRO</b>\n"
            f"Added: <b>{days} days</b>\n"
            f"ASMR total: <b>{local_days_left} days</b>\n\n"
            "Use the buttons below to join every included private page. Each link is one-time and valid for 72 hours."
        )
    await bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=keyboard)
