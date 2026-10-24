"""Safe, actionable administrator notices; never render raw exception payloads."""
import asyncio
import hashlib
import html
import logging
import re

logger = logging.getLogger(__name__)
ADMIN_NOTICE_TIMEOUT_SECONDS = 5

_REASONS = {
    "BOT_BLOCKED": (
        "Не может отправить сообщение: пользователь заблокировал бота.",
        "Пользователю нужно разблокировать бота и нажать /start. Затем проверьте доставку ссылок.",
    ),
    "USER_DEACTIVATED": (
        "Аккаунт пользователя удалён или деактивирован в Telegram.",
        "Проверьте Telegram ID пользователя. На удалённый аккаунт сообщение доставить нельзя.",
    ),
    "CHAT_NOT_STARTED": (
        "Бот не может первым начать личный диалог с пользователем.",
        "Попросите пользователя открыть именно этого бота и нажать /start.",
    ),
    "CHAT_NOT_FOUND": (
        "Telegram не нашёл чат с указанным пользователем или группой.",
        "Проверьте ID и попросите пользователя открыть бота и нажать /start.",
    ),
    "BOT_REMOVED": (
        "Бот удалён из группы или заблокирован в ней.",
        "Проверьте группу и восстановите необходимые права бота.",
    ),
    "INSUFFICIENT_RIGHTS": (
        "У бота недостаточно прав для этой операции в группе.",
        "Проверьте права администратора бота, в том числе создание приглашений.",
    ),
    "RATE_LIMIT": (
        "Telegram временно ограничил частоту запросов.",
        "Дождитесь окончания ограничения и проверьте доставку. Не повторяйте начисление дней.",
    ),
    "NETWORK_UNCONFIRMED": (
        "Нет подтверждения от Telegram или сервиса: таймаут либо ошибка соединения.",
        "Сначала проверьте результат операции: запрос мог выполниться до обрыва связи.",
    ),
    "TELEGRAM_UNAVAILABLE": (
        "Telegram временно недоступен или вернул серверную ошибку.",
        "Проверьте результат операции и доступность Telegram, не повторяя платёж.",
    ),
    "BOT_AUTH_FAILED": (
        "Telegram отклонил авторизацию бота.",
        "Проверьте настройку токена бота. Не отправляйте токен в чат.",
    ),
    "FORBIDDEN": (
        "Telegram запретил операцию; причина не определена как блокировка пользователем.",
        "Проверьте доступ бота к нужному чату и его права.",
    ),
    "INVALID_MESSAGE": (
        "Telegram отклонил текст или формат сообщения.",
        "Проверьте шаблон сообщения и HTML-разметку; доступ повторно не начисляйте.",
    ),
    "PEER_NOT_CONFIGURED": (
        "Подключение связанного проекта не настроено.",
        "Проверьте адрес и служебный ключ проекта, не публикуя их значения.",
    ),
    "PEER_AUTH_FAILED": (
        "Связанный сервис отклонил авторизацию запроса.",
        "Проверьте согласованность служебных ключей и адрес сервиса.",
    ),
    "PEER_UNAVAILABLE": (
        "Связанный сервис недоступен или вернул серверную ошибку.",
        "Сверьте его статус доступа и журналы. Не повторяйте начисление вслепую.",
    ),
    "INVITE_UNAVAILABLE": (
        "Не удалось получить ссылку-приглашение.",
        "Проверьте группу, права бота и наличие резервной ссылки.",
    ),
    "DATABASE_ERROR": (
        "Не удалось выполнить или подтвердить операцию с базой данных.",
        "Проверьте журнал операции и сохранённый баланс до любых повторных действий.",
    ),
    "UNKNOWN": (
        "Не удалось определить причину автоматически.",
        "Проверьте технический журнал по коду и этапу операции; не повторяйте платёж вслепую.",
    ),
}

_ACCESS_STATES = {
    "active": "Дни ASMR уже начислены. Повторно начислять дни не нужно.",
    "links_saved": "Дни ASMR начислены, ссылки сохранены в Mini App LINKS. Повторно начислять дни не нужно.",
    "bundle_unconfirmed": "Дни ASMR начислены. Состояние остальных проектов и сохранение ссылок требуют сверки. Не повторяйте платёж или начисление.",
    "link_only": "Ошибка относится к подготовке ссылки и сама по себе не означает сбой начисления. При необходимости сверьте баланс.",
    "unknown": "Результат активации не подтверждён. Сначала сверьте баланс и журнал операции.",
}


def describe_error(error: Exception | str) -> dict:
    message = str(getattr(error, "message", None) or error).lower()
    types = {cls.__name__ for cls in type(error).__mro__}
    retry_after = getattr(error, "retry_after", None)
    retry_after = retry_after if type(retry_after) is int and retry_after > 0 else None
    code = "UNKNOWN"
    if "bot was blocked by the user" in message or "bot is blocked by the user" in message:
        code = "BOT_BLOCKED"
    elif "user is deactivated" in message or "user deactivated" in message:
        code = "USER_DEACTIVATED"
    elif "can't initiate conversation" in message or "cannot initiate conversation" in message:
        code = "CHAT_NOT_STARTED"
    elif "chat not found" in message:
        code = "CHAT_NOT_FOUND"
    elif "bot was kicked" in message or "bot is not a member" in message:
        code = "BOT_REMOVED"
    elif any(value in message for value in ("not enough rights", "chat_admin_required", "administrator rights", "need administrator")):
        code = "INSUFFICIENT_RIGHTS"
    elif retry_after or "TelegramRetryAfter" in types or "too many requests" in message or "retry after" in message:
        code = "RATE_LIMIT"
    elif "TelegramUnauthorizedError" in types:
        code = "BOT_AUTH_FAILED"
    elif "TelegramServerError" in types or "RestartingTelegram" in types:
        code = "TELEGRAM_UNAVAILABLE"
    elif types.intersection({"DBAPIError", "IntegrityError", "OperationalError", "PendingRollbackError"}):
        code = "DATABASE_ERROR"
    elif types.intersection({"TimeoutError", "ConnectionError", "TelegramNetworkError", "ClientConnectionError", "ClientConnectorError"}) or any(value in message for value in ("timed out", "timeout", "connection reset", "cannot connect")):
        code = "NETWORK_UNCONFIRMED"
    elif "TelegramForbiddenError" in types:
        code = "FORBIDDEN"
    elif "can't parse entities" in message or "message is too long" in message:
        code = "INVALID_MESSAGE"
    elif "not configured" in message:
        code = "PEER_NOT_CONFIGURED"
    elif re.search(r"\bhttp\s+(401|403)\b", message):
        code = "PEER_AUTH_FAILED"
    elif re.search(r"\bhttp\s+5\d\d\b", message):
        code = "PEER_UNAVAILABLE"
    elif "invite unavailable" in message:
        code = "INVITE_UNAVAILABLE"
    reason, action = _REASONS[code]
    if code == "RATE_LIMIT" and retry_after:
        reason += f" Повторная попытка разрешена не раньше чем через {retry_after} сек."
    error_type = type(error).__name__ if isinstance(error, Exception) else "ServiceError"
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", error_type):
        error_type = "Exception"
    return {"code": code, "reason": reason, "action": action, "type": error_type}


def format_admin_error(*, title: str, stage: str, error: Exception | str,
                       telegram_id: int, access_state: str = "unknown",
                       order_uuid: str | None = None, fallback_available: bool = False) -> str:
    info = describe_error(error)
    lines = [f"<b>{html.escape(title)}</b>", f"Пользователь: <code>{int(telegram_id)}</code>"]
    if order_uuid:
        reference = hashlib.sha256(str(order_uuid).encode()).hexdigest()[:12]
        lines.append(f"Операция: <code>{reference}</code>")
    lines.extend([
        f"Этап: {html.escape(stage)}",
        f"Причина: {html.escape(info['reason'])}",
        f"Доступ: {html.escape(_ACCESS_STATES.get(access_state, _ACCESS_STATES['unknown']))}",
        f"Что сделать: {html.escape(info['action'])}",
        f"Код: <code>{info['code']}</code>",
    ])
    if info["code"] == "UNKNOWN":
        lines.append(f"Технический тип: <code>{info['type']}</code>")
    if fallback_available:
        lines.append("Резервная ссылка доступна; подготовка индивидуальной ссылки не завершена.")
    return "\n".join(lines)


def format_bundle_failures(failures: list[dict]) -> str:
    if not failures:
        return ""
    labels = {"asmrleaks": "ASMRLeaks", "privateleaks": "PrivateLeaks",
              "asianleaks": "AsianLeaks", "extraleaks": "ExtraLeaks"}
    lines = ["\n<b>Не получены ссылки из части проектов:</b>"]
    for failure in failures[:10]:
        info = describe_error(failure.get("error") or "unknown")
        label = labels.get(failure.get("project"), "Другой проект")
        lines.append(f"{label}: {html.escape(info['reason'])} [{info['code']}]")
    lines.append("Сначала сверьте доступ в этих проектах. Повторно начислять дни вслепую нельзя.")
    return "\n".join(lines)


async def notify_admin_error(bot, *, admin_ids=None, **details) -> bool:
    try:
        if admin_ids is None:
            from config import ADMIN_IDS
            admin_ids = ADMIN_IDS
        text = format_admin_error(**details)
        info = describe_error(details["error"])
        recipients = tuple(admin_ids)
    except Exception:
        logger.warning("Could not prepare administrator error notice")
        return False
    log = logger.error if info["code"] in {"UNKNOWN", "DATABASE_ERROR", "BOT_AUTH_FAILED", "PEER_AUTH_FAILED"} else logger.warning
    log("Bot operation failed: user=%s stage=%s code=%s type=%s",
        details["telegram_id"], details["stage"], info["code"], info["type"])
    delivered = False
    for admin_id in recipients:
        try:
            await asyncio.wait_for(bot.send_message(admin_id, text, parse_mode="HTML"),
                                   timeout=ADMIN_NOTICE_TIMEOUT_SECONDS)
            delivered = True
        except Exception as error:
            logger.warning("Admin notice delivery failed: admin=%s code=%s",
                           admin_id, describe_error(error)["code"])
    return delivered
