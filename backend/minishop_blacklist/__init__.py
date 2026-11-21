"""Minishop Plugin API v1 entry point."""

from pathlib import Path

from aiohttp import web
from bot.middlewares.i18n import JsonI18n
from bot.plugins.extensions import ExtensionContributions
from bot.plugins.spec import WEB_SCOPE_WEBAPP, Plugin, PluginContext, WorkerTaskSpec
from db.migrator import Migration

from .alerts import PLUGIN_ID, alert_contributions, subscribe_to_events
from .api import setup_routes
from .migrations import migrations
from .settings import SUPPORTS_USERS_TABLE
from .worker import run_refresh_worker

__version__ = "1.1.1"


class BlacklistPlugin(Plugin):
    name = PLUGIN_ID
    version = __version__
    plugin_api_min_version = 1
    plugin_api_max_version = 1

    def setup(self, ctx: PluginContext) -> None:
        if ctx.session_factory is not None:
            subscribe_to_events(ctx)

    def extensions(self, ctx: PluginContext) -> ExtensionContributions:
        contributions = alert_contributions()
        if not SUPPORTS_USERS_TABLE:
            return contributions

        from .users_table import add_users_table

        return add_users_table(contributions, self._i18n(ctx), ctx.settings.DEFAULT_LANGUAGE)

    def migrations(self) -> list[Migration]:
        return migrations()

    def setup_web(self, ctx: PluginContext, app: web.Application, *, scope: str) -> None:
        if scope == WEB_SCOPE_WEBAPP:
            setup_routes(app)
            if SUPPORTS_USERS_TABLE:
                from .users_table import setup_users_table

                setup_users_table(app, self._i18n(ctx), ctx.settings.DEFAULT_LANGUAGE)

    def locales_dir(self) -> Path:
        packaged = Path(__file__).parent / "locales"
        return packaged if packaged.is_dir() else Path(__file__).parents[2] / "locales"

    def _i18n(self, ctx: PluginContext) -> JsonI18n:
        return ctx.i18n or JsonI18n(str(self.locales_dir()), default=ctx.settings.DEFAULT_LANGUAGE)

    def worker_tasks(self, ctx: PluginContext) -> list[WorkerTaskSpec]:
        return [WorkerTaskSpec(name=f"{self.name}.refresh", factory=run_refresh_worker)]


plugin = BlacklistPlugin()
