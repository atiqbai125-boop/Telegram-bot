from __future__ import annotations

import asyncio
import logging

import uvicorn

from bot import build_application, setup_logging
from config import get_settings
from dashboard import account as user_account, app as dashboard_app


async def serve() -> None:
    settings = get_settings(require_bot_token=True)
    application = build_application(settings)
    application.bot_data["user_account_controller"] = user_account
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=False)

    server = uvicorn.Server(
        uvicorn.Config(
            dashboard_app,
            host=settings.dashboard_host,
            port=settings.dashboard_port,
            log_level="info",
            proxy_headers=True,
            access_log=False,
        )
    )
    logging.getLogger("song_bot").info(
        "Dashboard listening on http://%s:%s; Telegram bot polling is active",
        settings.dashboard_host,
        settings.dashboard_port,
    )
    try:
        await server.serve()
    finally:
        if application.updater and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        await application.shutdown()


def main() -> None:
    setup_logging()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
