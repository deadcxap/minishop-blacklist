"""Apply Minishop's native user filters and sorting within the blacklist."""

from datetime import datetime
from functools import partial

from bot.app.web.admin_api_impl.users_listing import _admin_tariff_keys
from bot.app.web.admin_api_impl.users_queries import _filter_and_sort_users
from bot.plugins.capabilities import CORE_PLUGIN_CAPABILITIES
from config.settings import Settings
from db.models import User
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState

from .storage_models import Entry


async def query_matched_users(
    session: AsyncSession,
    settings: Settings,
    *,
    limit: int,
    offset: int,
    query: str,
    filter_value: str,
    panel_status: str,
    premium_traffic: str,
    sort: str,
    tariff_filter: str,
    now: datetime,
) -> tuple[list[User], int]:
    criterion = User.telegram_id.in_(select(Entry.telegram_id))
    load_page = partial(
        _filter_and_sort_users,
        session,
        page_size=limit,
        query=query,
        filter_value=filter_value,
        panel_status=panel_status,
        premium_traffic=premium_traffic,
        sort_value=sort,
        tariff_filter=tariff_filter,
        valid_tariff_keys=_admin_tariff_keys(settings),
        now=now,
    )
    if CORE_PLUGIN_CAPABILITIES.get("admin_users_table", 0) >= 1 and offset % limit == 0:
        return await load_page(page=offset // limit, extension_predicates=(criterion,))

    def restrict_to_matches(state: ORMExecuteState) -> None:
        # Minishop executes two selects here: the user page and its total.
        statement = state.statement.where(criterion)
        if statement.column_descriptions[0]["expr"] is User:
            statement = statement.offset(offset)
        state.statement = statement

    # Scope the extra predicate to exactly that pair of queries. Profile, payment,
    # and referral lookups still see all relevant Minishop rows.
    event.listen(session.sync_session, "do_orm_execute", restrict_to_matches)
    try:
        return await load_page(page=0)
    finally:
        event.remove(session.sync_session, "do_orm_execute", restrict_to_matches)
