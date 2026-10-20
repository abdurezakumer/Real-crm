import asyncio

from telegram import Bot

from realestate_crm.config import get_settings


async def register() -> None:
    settings = get_settings()
    async with Bot(settings.telegram_bot_token.get_secret_value()) as bot:
        target = settings.telegram_webhook_url.rstrip("/")
        # Always reapply the complete webhook configuration. Telegram retains
        # the previous allowed_updates list when a webhook is not reset, even
        # when its URL already matches this target.
        await bot.set_webhook(
            url=target,
            secret_token=settings.telegram_webhook_secret.get_secret_value(),
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
            max_connections=10,
        )
        info = await bot.get_webhook_info()
        if info.url != target:
            raise RuntimeError("Telegram did not confirm the requested webhook URL")
        allowed_updates = set(info.allowed_updates or [])
        if not {"message", "callback_query"}.issubset(allowed_updates):
            raise RuntimeError("Telegram did not confirm callback_query webhook updates")
        print(f"Webhook registered: {target}")


if __name__ == "__main__":
    asyncio.run(register())
