"""Minishop Plugin API v1 entry point."""

from pathlib import Path

from aiohttp import web
from bot.plugins.capabilities import CORE_PLUGIN_CAPABILITIES
from bot.plugins.extensions import ExtensionContributions
from bot.plugins.spec import WEB_SCOPE_WEBAPP, Plugin, PluginContext, WorkerTaskSpec
from db.migrator import Migration

from .alerts import PLUGIN_ID, alert_contributions, subscribe_to_events
from .api import setup_routes
from .migrations import migrations
from .worker import run_refresh_worker

__version__ = "1.0.0"


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
        if CORE_PLUGIN_CAPABILITIES.get("admin_users_table", 0) >= 1:
            from .users_table import with_users_table

            return with_users_table(
                contributions, ctx.settings.DEFAULT_LANGUAGE, self.locales_dir()
            )
        return contributions

    def migrations(self) -> list[Migration]:
        return migrations()

    def setup_web(self, ctx: PluginContext, app: web.Application, *, scope: str) -> None:
        if scope == WEB_SCOPE_WEBAPP:
            setup_routes(app)

    def locales_dir(self) -> Path:
        packaged = Path(__file__).parent / "locales"
        return packaged if packaged.is_dir() else Path(__file__).parents[2] / "locales"

    def worker_tasks(self, ctx: PluginContext) -> list[WorkerTaskSpec]:
        return [WorkerTaskSpec(name=f"{self.name}.refresh", factory=run_refresh_worker)]


plugin = BlacklistPlugin()
