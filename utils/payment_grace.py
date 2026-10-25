"""Persistent, project-local Tribute grace policy; timestamps are stored in UTC."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Boolean, DateTime, String, BigInteger, select
from sqlalchemy.orm import Mapped, mapped_column

from config import PROJECT_KEY
from database import Base, async_session

MOSCOW = timezone(timedelta(hours=3), "Europe/Moscow")


class PaymentGraceSetting(Base):
    __tablename__ = "payment_grace_settings"

    project: Mapped[str] = mapped_column(String(32), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pending_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    effective_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


def utc_now():
    return datetime.now(timezone.utc)


def aware_utc(value):
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class GracePolicy:
    enabled: bool = True
    pending_enabled: bool | None = None
    effective_at: datetime | None = None

    def at(self, now):
        if self.effective_at and aware_utc(now) >= aware_utc(self.effective_at):
            return GracePolicy(self.pending_enabled)
        return self


async def load_policy(session=None, now=None):
    if session is None:
        async with async_session() as session:
            return await load_policy(session, now)
    row = await session.get(PaymentGraceSetting, PROJECT_KEY)
    policy = GracePolicy() if row is None else GracePolicy(
        row.enabled, row.pending_enabled,
        aware_utc(row.effective_at) if row.effective_at else None,
    )
    return policy.at(now or utc_now())


async def schedule_policy(session, enabled, admin_id, now=None):
    now = aware_utc(now or utc_now())
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Unsupported grace settings database")
    await session.execute(insert(PaymentGraceSetting).values(
        project=PROJECT_KEY, enabled=True,
    ).on_conflict_do_nothing(index_elements=["project"]))
    row = (await session.execute(select(PaymentGraceSetting).where(
        PaymentGraceSetting.project == PROJECT_KEY,
    ).with_for_update())).scalar_one()
    current = GracePolicy(row.enabled, row.pending_enabled, row.effective_at).at(now)
    row.enabled = current.enabled
    if enabled == current.enabled:
        row.pending_enabled = None
        row.effective_at = None
    elif current.pending_enabled != enabled or current.effective_at is None:
        tomorrow = now.astimezone(MOSCOW).date() + timedelta(days=1)
        midnight = datetime.combine(tomorrow, datetime.min.time(), MOSCOW)
        row.pending_enabled = enabled
        row.effective_at = midnight.astimezone(timezone.utc).replace(tzinfo=None)
    row.updated_by = admin_id
    await session.commit()
    return await load_policy(session, now)


def has_paid_access(user, policy):
    if user is None or not user.is_active:
        return False
    if (user.tier or "").lower() == "lifetime":
        return True
    return user.units > 0 or (
        policy.enabled
        and (user.last_payment_method or "").lower() in {"tribute", "tribute_v2"}
        and user.units > -7
    )
