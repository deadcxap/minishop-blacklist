"""Optional native users table contributions on hosts with admin_users_table v1."""

import json
from dataclasses import replace
from pathlib import Path

from bot.plugins.extensions import (
    AdminUserColumn,
    AdminUserFilter,
    AdminUserFilterOption,
    ExtensionContributions,
)
from db.models import User
from sqlalchemy import and_, case, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from .storage_models import Configuration, Entry, Source


def with_users_table(
    contributions: ExtensionContributions, language: str, locales_dir: Path
) -> ExtensionContributions:
    lang = "ru" if language.lower().split("-")[0] == "ru" else "en"
    labels = json.loads((locales_dir / f"{lang}.json").read_text(encoding="utf-8"))
    title = labels["admin_minishop_blacklist_status"]
    enabled = exists().where(Configuration.id == 1, Configuration.users_visible.is_(True))
    matched = exists().where(Entry.telegram_id == User.telegram_id).correlate(User)
    stale = (
        exists()
        .where(
            Entry.telegram_id == User.telegram_id,
            Source.id == Entry.source_id,
            Source.last_error.is_not(None),
        )
        .correlate(User)
    )
    covered = exists().select_from(Source) & ~exists().where(
        or_(Source.last_success_at.is_(None), Source.last_error.is_not(None))
    )
    clear = and_(User.telegram_id.is_not(None), covered, ~matched)

    async def column(session: AsyncSession, ids: tuple[int, ...]) -> dict[int, str | None]:
        status = case(
            (~enabled, None),
            (matched, case((stale, "stale_match"), else_="match")),
            (and_(User.telegram_id.is_not(None), covered), "clear"),
            else_="unknown",
        )
        rows = await session.execute(select(User.user_id, status).where(User.user_id.in_(ids)))
        return {
            uid: labels[f"admin_minishop_blacklist_{state}"] if state else None
            for uid, state in rows
        }

    def predicate(value: str) -> ColumnElement[bool]:
        # A saved filter becomes neutral if the operator turns display off.
        return ~enabled | (matched if value == "match" else clear)

    options = tuple(
        AdminUserFilterOption(value, labels[f"admin_minishop_blacklist_{value}"])
        for value in ("match", "clear")
    )
    return replace(
        contributions,
        admin_user_columns=(AdminUserColumn("status", title, column),),
        admin_user_filters=(AdminUserFilter("status", title, options, predicate),),
    )
