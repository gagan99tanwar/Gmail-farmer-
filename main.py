import os
import asyncio
from telethon import TelegramClient, events
from telethon.sessions import StringSession

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

FARMER_USERNAME = os.environ.get(
    "FARMER_USERNAME",
    "@GmailFarmerBot"
)

user_client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH
)

bot_client = TelegramClient(
    "bot",
    API_ID,
    API_HASH
)

pending_users = set()


@bot_client.on(events.NewMessage(pattern=r"^/start$"))
async def start(event):
    await event.reply(
        "Welcome!\n\n"
        "/register - Request a new task\n"
        "/balance - Check balance"
    )


@bot_client.on(events.NewMessage(pattern=r"^/register$"))
async def register(event):
    pending_users.add(event.sender_id)

    await user_client.send_message(
        FARMER_USERNAME,
        "Register a new gmail"
    )

    await event.reply(
        "Request sent. Waiting for response..."
    )


@bot_client.on(events.NewMessage(pattern=r"^/balance$"))
async def balance(event):
    pending_users.add(event.sender_id)

    await user_client.send_message(
        FARMER_USERNAME,
        "Balance"
    )

    await event.reply(
        "Balance request sent."
    )


@user_client.on(events.NewMessage())
async def farmer_response(event):

    sender = await event.get_sender()

    username = getattr(sender, "username", None)

    if not username:
        return

    if username.lower() != FARMER_USERNAME.lstrip("@").lower():
        return

    text = event.raw_text or ""

    # Sensitive information is intentionally not relayed.
    blocked_terms = (
        "password",
        "otp",
        "verification code",
        "app password",
        "secret key",
        "authentication",
        "authenticator",
        "qr code"
    )

    if any(term in text.lower() for term in blocked_terms):
        return

    for user_id in list(pending_users):

        try:
            await bot_client.send_message(
                user_id,
                text or "Status update received."
            )

        except Exception as e:
            print(
                f"Could not message {user_id}: {e}"
            )

    pending_users.clear()


async def main():

    await user_client.start()

    await bot_client.start(
        bot_token=BOT_TOKEN
    )

    me = await user_client.get_me()

    print(
        "Telegram account connected:",
        getattr(me, "username", None)
    )

    print("Relay bot is running.")

    await asyncio.gather(
        user_client.run_until_disconnected(),
        bot_client.run_until_disconnected()
    )


if __name__ == "__main__":
    asyncio.run(main())
