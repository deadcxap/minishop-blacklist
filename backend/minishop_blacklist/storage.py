"""Store source snapshots and join them to current Minishop accounts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, insert, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from .settings import DisplaySettings, Settings
from .sources import ParsedSource, Reason, merge_reasons
from .storage_models import Configuration, Entry, Source, StoredReason

_REFRESH_LEASE = timedelta(minutes=3)


@dataclass(frozen=True)
class SourceStatus:
    url: str
    last_success_at: datetime | None
    last_attempt_at: datetime | None
    last_error: str | None
    count: int
    invalid_lines: int


@dataclass(frozen=True)
class Match:
    state: Literal["match", "clear", "unknown"]
    reasons: tuple[Reason, ...] = ()
    stale: bool = False


@dataclass(frozen=True)
class RefreshLease:
    source_id: UUID
    url: str
    token: UUID


async def get_settings(session: AsyncSession) -> Settings:
    rows = (
        await session.execute(
            select(Configuration, Source.url)
            .select_from(Configuration)
            .outerjoin(Source, true())
            .order_by(Source.position, Source.id)
        )
    ).all()
    configuration = rows[0][0]
    return Settings(
        sources=tuple(url for _, url in rows if url is not None),
        refresh_hours=configuration.refresh_hours,
        display=DisplaySettings(
            users=configuration.users_visible,
            user_card=configuration.user_card_visible,
            ticket=configuration.ticket_visible,
            users_table=configuration.users_table_visible,
        ),
        chat_id=configuration.chat_id,
        topic_id=configuration.topic_id,
        events=tuple(configuration.events),
    )


async def save_settings(
    session: AsyncSession, settings: Settings, *, preserve_users_table: bool = False
) -> Settings:
    configuration = await session.get(
        Configuration, 1, with_for_update=True, populate_existing=True
    )
    if configuration is None:
        raise RuntimeError("blacklist configuration migration is missing")
    if preserve_users_table:
        settings = replace(
            settings,
            display=replace(settings.display, users_table=configuration.users_table_visible),
        )
    configuration.refresh_hours = settings.refresh_hours
    configuration.users_visible = settings.display.users
    configuration.user_card_visible = settings.display.user_card
    configuration.ticket_visible = settings.display.ticket
    configuration.users_table_visible = settings.display.users_table
    configuration.chat_id = settings.chat_id
    configuration.topic_id = settings.topic_id
    configuration.events = list(settings.events)

    sources = {source.url: source for source in await session.scalars(select(Source))}
    for position, url in enumerate(settings.sources):
        source = sources.pop(url, None)
        if source is None:
            session.add(Source(url=url, position=position))
        else:
            source.position = position
    for source in sources.values():
        await session.delete(source)
    await session.flush()
    return settings


async def list_source_statuses(session: AsyncSession) -> list[SourceStatus]:
    return [
        SourceStatus(
            url=source.url,
            last_success_at=source.last_success_at,
            last_attempt_at=source.last_attempt_at,
            last_error=source.last_error,
            count=source.count,
            invalid_lines=source.invalid_lines,
        )
        for source in await session.scalars(select(Source).order_by(Source.position, Source.id))
    ]


def _snapshot_reasons(reasons: list[StoredReason]) -> tuple[Reason, ...]:
    return tuple(Reason(value["text"], tuple(value["links"])) for value in reasons)


async def get_matches(session: AsyncSession, telegram_ids: Sequence[int]) -> dict[int, Match]:
    ids = set(telegram_ids)
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(Source.last_success_at, Source.last_error, Entry.telegram_id, Entry.reasons)
            .select_from(Source)
            .outerjoin(Entry, and_(Entry.source_id == Source.id, Entry.telegram_id.in_(ids)))
            .order_by(Source.position, Source.id)
        )
    ).all()
    covered = bool(rows) and all(
        success is not None and error is None for success, error, _, _ in rows
    )
    reasons_by_id: dict[int, list[tuple[Reason, ...]]] = {}
    stale_ids: set[int] = set()
    for _, error, telegram_id, reasons in rows:
        if telegram_id is not None:
            reasons_by_id.setdefault(telegram_id, []).append(_snapshot_reasons(reasons))
            if error is not None:
                stale_ids.add(telegram_id)
    return {
        telegram_id: Match(
            "match", merge_reasons(reasons_by_id[telegram_id]), stale=telegram_id in stale_ids
        )
        if telegram_id in reasons_by_id
        else Match("clear" if covered else "unknown")
        for telegram_id in ids
    }


async def claim_due_sources(session: AsyncSession, now: datetime) -> list[RefreshLease]:
    schedule = (
        await session.execute(
            select(Configuration.refresh_hours, Configuration.refresh_requested_at).where(
                Configuration.id == 1
            )
        )
    ).one_or_none()
    if schedule is None:
        raise RuntimeError("blacklist configuration migration is missing")
    hours, requested_at = schedule
    cutoff = now - timedelta(hours=hours)
    due = [Source.last_attempt_at.is_(None), Source.last_attempt_at <= cutoff]
    if requested_at is not None:
        due.append(Source.last_attempt_at < requested_at)
    sources = await session.scalars(
        select(Source)
        .where(
            or_(*due),
            or_(Source.lease_until.is_(None), Source.lease_until <= now),
        )
        .order_by(Source.position, Source.id)
        .with_for_update(skip_locked=True)
    )
    leases = []
    for source in sources:
        token = uuid4()
        source.lease_token = token
        source.lease_until = now + _REFRESH_LEASE
        leases.append(RefreshLease(source.id, source.url, token))
    return leases


async def source_refresh_pending(session: AsyncSession) -> bool:
    awaiting_download = (
        select(Source.id)
        .join(Configuration, Configuration.id == 1)
        .where(
            Configuration.refresh_requested_at.is_not(None),
            or_(
                Source.last_attempt_at.is_(None),
                Source.last_attempt_at < Configuration.refresh_requested_at,
            ),
        )
    )
    return bool(await session.scalar(select(awaiting_download.exists())))


async def request_source_refresh(session: AsyncSession) -> None:
    configuration = await session.get(
        Configuration, 1, with_for_update=True, populate_existing=True
    )
    if configuration is None:
        raise RuntimeError("blacklist configuration migration is missing")
    if await source_refresh_pending(session):
        return
    if await session.scalar(select(Source.id).limit(1)) is not None:
        configuration.refresh_requested_at = datetime.now(UTC)
        await session.flush()


async def record_refresh(
    session: AsyncSession,
    lease: RefreshLease,
    now: datetime,
    *,
    snapshot: ParsedSource | None = None,
    error: str | None = None,
) -> bool:
    if (snapshot is None) == (error is None):
        raise ValueError("a snapshot or an error must be supplied")
    source = await session.scalar(
        select(Source)
        .where(Source.id == lease.source_id, Source.lease_token == lease.token)
        .with_for_update()
    )
    if source is None:
        return False
    if snapshot is not None:
        await session.execute(delete(Entry).where(Entry.source_id == source.id))
        if snapshot.entries:
            await session.execute(
                insert(Entry),
                [
                    {
                        "source_id": source.id,
                        "telegram_id": telegram_id,
                        "reasons": [
                            {"text": reason.text, "links": list(reason.links)} for reason in reasons
                        ],
                    }
                    for telegram_id, reasons in snapshot.entries.items()
                ],
            )
        source.last_success_at = now
        source.count = len(snapshot.entries)
        source.invalid_lines = snapshot.invalid_lines
    source.last_attempt_at = now
    source.last_error = error
    source.lease_token = None
    source.lease_until = None
    return True
