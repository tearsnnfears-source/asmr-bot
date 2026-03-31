"""
YooMoney API — создание ссылки на оплату и проверка поступления.
Документация: https://yoomoney.ru/docs/payment-buttons/using-api/forms
"""
import uuid
import hashlib
import httpx
import logging
from datetime import datetime, timedelta
from config import YOOMONEY_TOKEN, YOOMONEY_WALLET

logger = logging.getLogger(__name__)

YOOMONEY_API = "https://yoomoney.ru/api"


def generate_label() -> str:
    """Уникальный label для привязки платежа к пользователю."""
    return uuid.uuid4().hex[:16]


def make_payment_url(amount: int, label: str, comment: str = "asmrleaks.tv") -> str:
    return (
        f"https://yoomoney.ru/quickpay/confirm.xml?"
        f"receiver={YOOMONEY_WALLET}"
        f"&quickpay-form=donate"
        f"&paymentType=SB"
        f"&sum={amount}"
        f"&label={label}"
        f"&comment={comment}"
    )


async def check_payment(label: str, amount: int) -> bool:
    """
    Проверяет, пришёл ли платёж с данным label за последние 24 часа.
    Возвращает True если платёж найден и сумма совпадает.
    """
    if not YOOMONEY_TOKEN:
        logger.warning("YOOMONEY_TOKEN not set")
        return False

    from_dt = (datetime.utcnow() - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{YOOMONEY_API}/operation-history",
                headers={"Authorization": f"Bearer {YOOMONEY_TOKEN}"},
                data={
                    "type":   "deposition",
                    "label":  label,
                    "from":   from_dt,
                    "records": 5,
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

        for op in data.get("operations", []):
            if (
                op.get("label") == label
                and op.get("status") == "success"
                and float(op.get("amount", 0)) >= amount
            ):
                return True
        return False

    except Exception as e:
        logger.error(f"YooMoney check error: {e}")
        return False
