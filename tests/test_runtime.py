"""Run the stock Minishop admin middleware, event bus and job runner against PostgreSQL."""

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bot.app.web.admin_api_impl.auth import admin_auth_middleware
from bot.app.web.admin_api_impl.users_listing import admin_users_list_route
from bot.app.web.context import SESSION_FACTORY, SETTINGS
from bot.app.web.webapp.assets import _csrf_protection_middleware
from bot.app.web.webapp_auth import create_webapp_session_token
from bot.infra import events
from bot.plugins.extensions.jobs import run_once
from bot.plugins.loader import register, reset_plugins, run_setup
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import ROLE_ADMIN, grant_role
from config.settings import Settings as HostSettings
from db.base import Base
from db.dal import payment_dal, subscription_dal
from db.migrator import run_migration_chains
from db.models import Subscription, User
from minishop_blacklist import plugin
from minishop_blacklist.refresh import refresh_sources
from minishop_blacklist.settings import SUPPORTS_USERS_TABLE
from minishop_blacklist.sources import parse_source
from minishop_blacklist.storage import claim_due_sources, record_refresh, request_source_refresh
from sqlalchemy import true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_BASE = "/api/admin/minishop-blacklist"
_CUSTOMER_ID = -301
_TELEGRAM_ID = 998877661
_OTHER_TELEGRAM_ID = 898877661


class Telegram:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_message(self, **message: object) -> SimpleNamespace:
        self.sent.append(message)
        return SimpleNamespace(message_id=len(self.sent))


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch) -> AsyncIterator[tuple[PluginContext, Telegram]]:
    url = os.environ.get("MINISHOP_BLACKLIST_RUNTIME_TEST_DATABASE")
    if not url:
        pytest.skip("Set MINISHOP_BLACKLIST_RUNTIME_TEST_DATABASE to a PostgreSQL test database")
    monkeypatch.setenv("MINISHOP_PLUGIN_STORE", str(tmp_path / "plugin-store"))
    monkeypatch.delenv("MINISHOP_PLUGIN_GENERATION", raising=False)
    engine = create_async_engine(url)
    schema = f"blacklist_runtime_{uuid4().hex}"
    isolated = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    session_factory = async_sessionmaker(isolated, expire_on_commit=False)
    settings = HostSettings(
        _env_file=None,
        POSTGRES_USER="placeholder",
        POSTGRES_PASSWORD="placeholder",
        DEFAULT_LANGUAGE="ru",
        WEBAPP_SESSION_SECRET="local-regression-testing-only",
        SUBSCRIPTION_MINI_APP_URL="https://admin.example/shop/",
        TELEGRAM_ENABLED=False,
        PLUGINS_ENABLED=True,
        PLUGINS_STRICT=True,
        ADMIN_USERS_LIST_CACHE_TTL_SECONDS=0,
    )
    monkeypatch.setattr("config.settings._settings_instance", settings)
    bot = Telegram()
    ctx = PluginContext(settings=settings, session_factory=session_factory, bot=bot)
    register(plugin)
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        async with isolated.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(run_migration_chains, {plugin.name: plugin.migrations()})
        run_setup(ctx)
        async with session_factory.begin() as session:
            session.add_all(
                [
                    User(user_id=200, telegram_id=2001),
                    User(user_id=_CUSTOMER_ID, telegram_id=_TELEGRAM_ID, first_name="<Tester>"),
                ]
            )
            await session.flush()
            await grant_role(session, 200, ROLE_ADMIN)
        yield ctx, bot
    finally:
        events.reset_subscribers()
        reset_plugins()
        await isolated.dispose()
        async with engine.begin() as connection:
            await connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await engine.dispose()


def _auth(settings: HostSettings, user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_webapp_session_token(settings, user_id)}"}


async def _client(ctx: PluginContext) -> TestClient:
    app = web.Application(middlewares=[_csrf_protection_middleware, admin_auth_middleware])
    app[SETTINGS] = ctx.settings
    app[SESSION_FACTORY] = ctx.require_session_factory()
    app.router.add_get("/api/admin/users", admin_users_list_route)
    plugin.setup_web(ctx, app, scope=WEB_SCOPE_WEBAPP)
    return TestClient(TestServer(app))


async def _configure(ctx: PluginContext, client: TestClient) -> dict[str, object]:
    headers = _auth(ctx.settings, 200)
    response = await client.get(f"{_BASE}/settings", headers=headers)
    body = await response.json()
    assert response.status == 200 and body["settings"]["refreshHours"] == 24
    assert len(body["settings"]["sources"]) == 2
    assert body["supportsUsersTable"] is SUPPORTS_USERS_TABLE
    assert body["settings"]["display"]["usersTable"] is True
    settings = body["settings"]
    settings.update(
        refreshHours=48,
        chatId="-1001234567890",
        topicId="5",
        events=["register", "trial", "payment", "expired"],
    )
    response = await client.put(f"{_BASE}/settings", headers=headers, json=settings)
    assert response.status == 200
    assert (await response.json())["settings"]["refreshHours"] == 48

    now = datetime.now(UTC)
    async with ctx.require_session_factory().begin() as session:
        first, second = await claim_due_sources(session, now)
        await record_refresh(
            session,
            first,
            now,
            snapshot=parse_source(
                f"{_TELEGRAM_ID} # Причина А https://example.org/one\n"
                f"{_TELEGRAM_ID} # Причина Б\n"
                f"{_OTHER_TELEGRAM_ID} # Другой пользователь\n".encode()
            ),
        )
        await record_refresh(
            session,
            second,
            now,
            snapshot=parse_source(
                f"{_TELEGRAM_ID} # Причина А https://example.org/two\n"
                f"{_TELEGRAM_ID} # <причина В>\n".encode()
            ),
        )
    return settings


@pytest.mark.asyncio
async def test_settings_and_statuses_require_admin_and_current_visibility(runtime) -> None:
    ctx, _bot = runtime
    async with await _client(ctx) as client:
        assert (await client.get(f"{_BASE}/settings")).status == 401
        assert (
            await client.get(f"{_BASE}/settings", headers=_auth(ctx.settings, _CUSTOMER_ID))
        ).status == 403

        settings = await _configure(ctx, client)
        headers = _auth(ctx.settings, 200)
        response = await client.post(
            f"{_BASE}/status",
            headers=headers,
            json={"view": "ticket", "telegramIds": [str(_TELEGRAM_ID), "123"]},
        )
        status = (await response.json())["statuses"]
        assert status["123"]["state"] == "clear"
        found = status[str(_TELEGRAM_ID)]
        assert found["reasons"] == [
            {"text": "Причина А", "links": ["https://example.org/one", "https://example.org/two"]},
            {"text": "Причина Б", "links": []},
            {"text": "<причина В>", "links": []},
        ]
        matches = await client.get(f"{_BASE}/matches", headers=headers)
        matched_page = await matches.json()
        assert matched_page["total"] == 1
        assert matched_page["rows"][0]["telegramId"] == str(_TELEGRAM_ID)
        assert matched_page["rows"][0]["userId"].startswith("ms_")

        cookie = "rw_webapp_session=" + create_webapp_session_token(ctx.settings, 200)
        settings["display"]["ticket"] = False
        response = await client.put(f"{_BASE}/settings", headers={"Cookie": cookie}, json=settings)
        assert response.status == 403
        response = await client.put(f"{_BASE}/settings", headers=headers, json=settings)
        assert response.status == 200
        response = await client.post(
            f"{_BASE}/status",
            headers=headers,
            json={"view": "ticket", "telegramIds": [str(_TELEGRAM_ID)]},
        )
        assert await response.json() == {"ok": True, "enabled": False, "statuses": {}}


@pytest.mark.asyncio
async def test_native_filters_and_manual_refresh(runtime, monkeypatch) -> None:
    ctx, _bot = runtime
    app_factory = ctx.require_session_factory()
    async with await _client(ctx) as client:
        await _configure(ctx, client)
        headers = _auth(ctx.settings, 200)
        async with app_factory.begin() as session:
            session.add(User(user_id=909, telegram_id=_OTHER_TELEGRAM_ID, first_name="Zulu"))
            session.add(User(user_id=910, telegram_id=777000, first_name="Zzz", referred_by_id=909))

        result = await client.get(f"{_BASE}/matches?sort=name_desc&limit=1", headers=headers)
        page = await result.json()
        assert page["total"] == 2
        assert page["rows"][0]["telegramId"] == str(_OTHER_TELEGRAM_ID)
        assert page["rows"][0]["profile"]["invited_users_count"] == 1
        assert page["rows"][0]["profile"]["panel_status"] == "bot_only"
        result = await client.get(f"{_BASE}/matches?sort=name_desc&offset=1", headers=headers)
        assert (await result.json())["rows"][0]["telegramId"] == str(_TELEGRAM_ID)
        result = await client.get(f"{_BASE}/matches?q={_TELEGRAM_ID}&limit=1", headers=headers)
        assert (await result.json())["total"] == 1
        result = await client.get(f"{_BASE}/matches?filter=no_tg", headers=headers)
        assert (await result.json())["total"] == 0

        denied = await client.post(
            f"{_BASE}/sources/refresh", headers=_auth(ctx.settings, _CUSTOMER_ID), json={}
        )
        assert denied.status == 403
        assert await refresh_sources(app_factory) == 0
        result = await client.post(f"{_BASE}/sources/refresh", headers=headers, json={})
        assert (await result.json())["refreshPending"] is True

        async def fetch(_url: str):
            return parse_source(f"{_TELEGRAM_ID} # Обновлённый комментарий\n".encode())

        monkeypatch.setattr("minishop_blacklist.refresh.fetch_source", fetch)
        assert await refresh_sources(app_factory) == 2
        result = await client.get(f"{_BASE}/settings", headers=headers)
        assert (await result.json())["refreshPending"] is False
        result = await client.get(f"{_BASE}/matches", headers=headers)
        page = await result.json()
        assert page["total"] == 1
        assert page["rows"][0]["reasons"] == [{"text": "Обновлённый комментарий", "links": []}]


@pytest.mark.skipif(not SUPPORTS_USERS_TABLE, reason="Minishop lacks admin_users_table")
@pytest.mark.asyncio
async def test_users_table_status_filter_and_visibility(runtime) -> None:
    from bot.plugins.extensions import (
        AdminUserColumn,
        AdminUserFilter,
        AdminUserFilterOption,
        ExtensionContributions,
    )
    from bot.plugins.extensions.registry import get_registry

    async def sample_column(_session: AsyncSession, ids: tuple[int, ...]) -> dict[int, str | None]:
        return dict.fromkeys(ids, "Sample")

    get_registry().register(
        "example",
        "1.0.0",
        ExtensionContributions(
            admin_user_columns=(AdminUserColumn("status", "Example", sample_column),),
            admin_user_filters=(
                AdminUserFilter(
                    "status", "Example", (AdminUserFilterOption("all", "All"),), lambda _: true()
                ),
            ),
        ),
    )
    ctx, _bot = runtime
    extension = "minishop-blacklist:status"
    users_path = "/api/admin/users"
    headers = _auth(ctx.settings, 200)
    async with await _client(ctx) as client:
        forbidden = await client.get(users_path, headers=_auth(ctx.settings, _CUSTOMER_ID))
        assert forbidden.status == 403
        settings = await _configure(ctx, client)
        async with ctx.require_session_factory().begin() as session:
            customer = await session.get(User, _CUSTOMER_ID)
            customer.first_name = "Native A"
            session.add_all(
                [
                    User(user_id=301, telegram_id=_OTHER_TELEGRAM_ID, first_name="Native B"),
                    User(user_id=302, telegram_id=80002, first_name="Native Clear"),
                    User(user_id=303, first_name="Native Unlinked"),
                ]
            )

        async def load_users(
            option: str = "", *, page: int = 0, page_size: int = 25, query: str = "Native"
        ) -> dict:
            params = {
                "q": query,
                "sort": "name_asc",
                "page": str(page),
                "page_size": str(page_size),
            }
            if option:
                params["extension_filter"] = f"{extension}={option}"
            response = await client.get(users_path, headers=headers, params=params)
            assert response.status == 200
            payload = await response.json()
            assert payload["ok"]
            return payload

        initial = await load_users()
        assert initial["total"] == 4
        assert {row["user_id"]: row["extension_values"][extension] for row in initial["users"]} == {
            _CUSTOMER_ID: "⛔️", 301: "⛔️", 302: "❎", 303: "❔",
        }
        column = next(item for item in initial["extension_columns"] if item["id"] == extension)
        assert column["label"] == "Чёрный список"
        first = await load_users("match", page_size=1)
        second = await load_users("match", page=1, page_size=1)
        assert first["total"] == second["total"] == 2
        assert {first["users"][0]["user_id"], second["users"][0]["user_id"]} == {_CUSTOMER_ID, 301}
        assert (await load_users("match", query="Native Clear"))["total"] == 0
        assert [row["user_id"] for row in (await load_users("clear"))["users"]] == [302]
        assert [row["user_id"] for row in (await load_users("unlinked"))["users"]] == [303]
        assert (await load_users("unknown"))["total"] == 0

        async with ctx.require_session_factory().begin() as session:
            admin = await session.get(User, 200)
            admin.language_code = "en"
            await request_source_refresh(session)
            now = datetime.now(UTC)
            first_source, _ = await claim_due_sources(session, now)
            await session.flush()
            assert await record_refresh(session, first_source, now, error="source_timeout")

        unknown = await load_users("unknown")
        assert [row["user_id"] for row in unknown["users"]] == [302]
        assert unknown["users"][0]["extension_values"][extension] == "⏳"
        english = next(item for item in unknown["extension_filters"] if item["id"] == extension)
        assert english["label"] == "Blacklist"
        assert {option["value"]: option["label"] for option in english["options"]} == {
            "match": "Match found",
            "clear": "No match",
            "unknown": "Check incomplete",
            "unlinked": "No Telegram linked",
        }
        assert (await load_users("clear"))["total"] == 0
        assert (await load_users("match"))["total"] == 2

        settings["display"]["usersTable"] = False
        assert (await client.put(f"{_BASE}/settings", headers=headers, json=settings)).status == 200
        settings["display"].pop("usersTable")
        legacy = await client.put(f"{_BASE}/settings", headers=headers, json=settings)
        assert (await legacy.json())["settings"]["display"]["usersTable"] is False
        disabled = await load_users("match")
        assert disabled["total"] == 4
        assert disabled["extension_columns"] == [{"id": "example:status", "label": "Example"}]
        assert [item["id"] for item in disabled["extension_filters"]] == ["example:status"]
        for row in disabled["users"]:
            assert row["extension_values"].get(extension) is None
            assert row["extension_values"]["example:status"] == "Sample"

        settings["display"]["usersTable"] = True
        settings["sources"] = []
        assert (await client.put(f"{_BASE}/settings", headers=headers, json=settings)).status == 200
        assert (await load_users("match"))["total"] == 0
        assert (await load_users("clear"))["total"] == 0
        assert (await load_users("unknown"))["total"] == 3


@pytest.mark.asyncio
async def test_four_event_categories_use_target_topic_and_ignore_duplicates(runtime) -> None:
    ctx, bot = runtime
    async with await _client(ctx) as client:
        await _configure(ctx, client)
    now = datetime.now(UTC)
    async with ctx.require_session_factory().begin() as session:
        active_trial = Subscription(
            user_id=_CUSTOMER_ID,
            panel_user_uuid="trial",
            end_date=now + timedelta(days=1),
            status_from_panel="ACTIVE",
            provider="trial",
        )
        expired = Subscription(user_id=_CUSTOMER_ID, panel_user_uuid="expired", end_date=now)
        session.add_all([active_trial, expired])
        await session.flush()
        await subscription_dal.record_subscription_notification(
            session, expired.subscription_id, "expired:event"
        )
        for sale_mode in ("subscription", "balance_topup"):
            payment = await payment_dal.create_payment_record(
                session,
                {
                    "user_id": _CUSTOMER_ID,
                    "amount": 10.0,
                    "currency": "RUB",
                    "status": "pending",
                    "provider": "test",
                    "sale_mode": sale_mode,
                },
            )
            await payment_dal.update_payment_status_by_db_id(
                session, payment.payment_id, "succeeded"
            )
            if sale_mode == "balance_topup":
                topup_payment_id = payment.payment_id

    registration = {"user_id": _CUSTOMER_ID, "telegram_id": _TELEGRAM_ID}
    trial = {"user_id": _CUSTOMER_ID, "end_date": active_trial.end_date.isoformat()}
    expiry = {
        "user_id": _CUSTOMER_ID,
        "subscription_id": expired.subscription_id,
        "end_date": expired.end_date.isoformat(),
    }
    topup = {"sale_mode": "balance_topup", "payment_db_id": topup_payment_id}
    for event, payload in (
        (events.USER_REGISTERED, registration),
        (events.TRIAL_ACTIVATED, trial),
        (events.SUBSCRIPTION_EXPIRED, expiry),
        (events.PAYMENT_SUCCEEDED, topup),
    ):
        await events.emit(event, payload)
        await events.emit(event, payload)
    while await run_once(ctx):
        pass

    assert len(bot.sent) == 5
    texts = [message["text"] for message in bot.sent]
    for title in ("Зарегистрировался", "Взял триал", "Истёк срок подписки"):
        assert sum(f"<b>{title}</b>" in text for text in texts) == 1
    assert sum("<b>Оплатил</b>" in text for text in texts) == 2
    for message in bot.sent:
        assert message["chat_id"] == -1001234567890
        assert message["message_thread_id"] == 5
        assert "&lt;Tester&gt;" in message["text"] and "&lt;причина В&gt;" in message["text"]
