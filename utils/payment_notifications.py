"""Detailed administrator receipts; no payment or access side effects."""
from html import escape


PROJECT_NAMES = {
    'asmrleaks': 'ASMRLeaks', 'privateleaks': 'PrivateLeaks',
    'asianleaks': 'AsianLeaks', 'extraleaks': 'ExtraLeaks',
}


def payment_admin_notice(*, provider, telegram_id, username, amount, tier,
                         project, days, total_days, receipt_id, channel=None,
                         renewal=False, promo_code=None, links_count=None,
                         local_tier=None):
    is_stars = provider == 'TG Stars'
    event = 'Новая оплата' if is_stars else 'Продление подписки' if renewal else 'Новая подписка'
    icon = '⭐' if is_stars else '💳'
    nick = '@' + str(username).lstrip('@') if username else f'id{telegram_id}'
    receipt_label = 'Payment ID' if is_stars else 'Subscription ID'
    channel = channel or PROJECT_NAMES.get(project, project)
    total = f'{total_days} дн.' if total_days is not None else 'Не указан'
    period = f'+{days} дней'
    if tier == 'lifetime':
        period, total = 'Бессрочный доступ', 'Без ограничений'
    elif is_stars and total_days is not None and total_days > 9000 and project != 'asmrleaks':
        total = 'Без ограничений'
    lines = [
        f'{icon} <b>{escape(event)} — {escape(provider)}</b>',
        f'👤 {escape(nick)} | <code>{escape(str(telegram_id))}</code>',
        f'💶 Сумма: <b>{escape(str(amount))}</b>',
        f'💎 Tier: <b>{escape(str(tier).upper())}</b>',
        f'📣 Канал: <code>{escape(str(channel))}</code>',
        f'🧾 {receipt_label}: <code>{escape(str(receipt_id or "Не указан"))}</code>',
        f'📅 {escape(period)} | Итого: {escape(total)}',
    ]
    if local_tier:
        lines.append(f'🎙 ASMR tier: <b>{escape(local_tier.upper())}</b>')
    if links_count is not None:
        lines.append(f'🔗 Ссылок: {links_count}')
    if promo_code:
        lines.append(f'🎟 Промокод: <code>{escape(str(promo_code))}</code>')
    return '\n'.join(lines)
