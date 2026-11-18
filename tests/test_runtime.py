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
from bot.plugins.capabilities import CORE_PLUGIN_CAPABILITIES
from bot.plugins.extensions.jobs import run_once
from bot.plugins.loader import register, reset_plugins, run_setup
from bot.plugins.spec import WEB_SCOPE_WEBAPP, PluginContext
from bot.services.account_roles import ROLE_ADMIN, grant_role
from config.settings import Settings as HostSettings
from db.base import Base
from db.dal import payment_dal, subscription_dal
from db.extension_models import ExtensionOperation
from db.migrator import run_migration_chains
from db.models import Subscription, User
from minishop_blacklist import plugin
from minishop_blacklist.refresh import refresh_sources
from minishop_blacklist.sources import parse_source
from minishop_blacklist.storage import claim_due_sources, record_refresh
from minishop_blacklist.storage_models import Source
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

_BASE = "/api/admin/minishop-blacklist"
_CUSTOMER_ID = -301
_TELEGRAM_ID = 5998877661
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
    plugin.setup_web(ctx, app, scope=WEB_SCOPE_WEBAPP)
    app.router.add_get("/api/admin/users", admin_users_list_route)
    return TestClient(TestServer(app))


async def _configure(ctx: PluginContext, client: TestClient) -> dict[str, object]:
    headers = _auth(ctx.settings, 200)
    response = await client.get(f"{_BASE}/settings", headers=headers)
    body = await response.json()
    assert response.status == 200 and body["settings"]["refreshHours"] == 24
    assert len(body["settings"]["sources"]) == 2
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
        async with ctx.require_session_factory()() as session:
            accounts = (await session.execute(select(User.telegram_id, User.is_banned))).all()
            assert set(accounts) == {(2001, False), (_TELEGRAM_ID, False)}

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

        async def fetch(_url: str, _previous_sha256: str | None):
            return parse_source(f"{_TELEGRAM_ID} # Обновлённый комментарий\n".encode())

        monkeypatch.setattr("minishop_blacklist.refresh.fetch_source", fetch)
        assert await refresh_sources(app_factory) == 2
        result = await client.get(f"{_BASE}/settings", headers=headers)
        assert (await result.json())["refreshPending"] is False
        result = await client.get(f"{_BASE}/matches", headers=headers)
        page = await result.json()
        assert page["total"] == 1
        assert page["rows"][0]["reasons"] == [{"text": "Обновлённый комментарий", "links": []}]


@pytest.mark.asyncio
async def test_native_table_filters_count_and_visibility(runtime) -> None:
    ctx, _bot = runtime
    async with await _client(ctx) as client:
        settings = await _configure(ctx, client)
        headers = _auth(ctx.settings, 200)
        route = "/api/admin/users"
        key = "minishop-blacklist:status"
        response = await client.get(route, headers=headers)
        page = await response.json()
        if not CORE_PLUGIN_CAPABILITIES.get("admin_users_table"):
            assert "extension_columns" not in page
            return
        assert page["extension_columns"][0]["id"] == key
        assert page["extension_filters"][0]["id"] == key
        for selected, telegram_id in (("match", _TELEGRAM_ID), ("clear", 2001)):
            response = await client.get(
                route,
                headers=headers,
                params={
                    "extension_filter": f"{key}={selected}",
                    "page_size": 1,
                },
            )
            page = await response.json()
            assert page["total"] == 1
            assert page["users"][0]["telegram_id"] == telegram_id
            assert page["users"][0]["extension_values"][key]
        async with ctx.require_session_factory().begin() as session:
            await session.execute(update(Source).values(last_error="source_timeout"))
        response = await client.get(
            route, headers=headers, params={"extension_filter": f"{key}=clear"}
        )
        assert (await response.json())["total"] == 0
        response = await client.get(
            route, headers=headers, params={"extension_filter": f"{key}=match"}
        )
        page = await response.json()
        assert page["total"] == 1
        assert "копии" in page["users"][0]["extension_values"][key]
        settings["display"]["users"] = False
        assert (await client.put(f"{_BASE}/settings", headers=headers, json=settings)).status == 200
        result = await client.get(
            route, headers=headers, params={"extension_filter": f"{key}=match"}
        )
        page = await result.json()
        assert page["total"] == 2
        assert all(row["extension_values"][key] is None for row in page["users"])


@pytest.mark.asyncio
async def test_payment_alert_setting_controls_durable_delivery(runtime) -> None:
    ctx, bot = runtime
    sessions = ctx.require_session_factory()
    headers = _auth(ctx.settings, 200)
    conditional = CORE_PLUGIN_CAPABILITIES.get("durable_jobs", 0) >= 2
    async with await _client(ctx) as client:
        settings = await _configure(ctx, client)
        for enabled in (False, True, False):
            settings["events"] = ["payment"] if enabled else []
            assert (
                await client.put(f"{_BASE}/settings", headers=headers, json=settings)
            ).status == 200
            async with sessions.begin() as session:
                payment = await payment_dal.create_payment_record(
                    session,
                    {
                        "user_id": _CUSTOMER_ID,
                        "amount": 10.0,
                        "currency": "RUB",
                        "status": "pending",
                        "provider": "test",
                        "sale_mode": "subscription",
                    },
                )
                for _ in range(2):
                    await payment_dal.update_payment_status_by_db_id(
                        session, payment.payment_id, "succeeded"
                    )
            async with sessions() as session:
                row = await session.get(type(payment), payment.payment_id)
                assert row is not None and row.status == "succeeded"
                queued = await session.scalar(
                    select(func.count())
                    .select_from(ExtensionOperation)
                    .where(
                        ExtensionOperation.state == "queued",
                        ExtensionOperation.kind == "send-payment-alert",
                    )
                )
                assert queued == int(enabled or not conditional)
            while await run_once(ctx):
                pass
    assert len(bot.sent) == 1


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
    await events.emit(events.USER_REGISTERED, {"user_id": 200, "telegram_id": 2001})
    async with ctx.require_session_factory()() as session:
        queued = await session.scalar(select(func.count()).select_from(ExtensionOperation))
        assert queued == 5
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
