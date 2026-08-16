import os
import asyncio
import logging
import tempfile
from dataclasses import dataclass

from aiohttp import web
from telethon import TelegramClient
from telethon.sessions import StringSession

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

OLD_BOT_USERNAME = os.getenv(
    "OLD_BOT_USERNAME",
    "@GmailFarmerBot"
)

REPLY_TIMEOUT = int(
    os.getenv("REPLY_TIMEOUT", "30")
)

# Maximum number of jobs waiting in memory.
# Increase if you expect heavy traffic.
MAX_QUEUE_SIZE = int(
    os.getenv("MAX_QUEUE_SIZE", "100")
)

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("relay")

# ============================================================
# TELETHON USER CLIENT
# ============================================================

user_client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH
)

# ============================================================
# JOB
# ============================================================

@dataclass
class RelayJob:
    chat_id: int
    message_id: int
    message: object
    bot: object

# ============================================================
# QUEUE
# ============================================================

relay_queue = asyncio.Queue(
    maxsize=MAX_QUEUE_SIZE
)

# ============================================================
# HEALTH SERVER
# ============================================================

async def health(request):
    return web.Response(
        text="Telegram relay is running."
    )


async def start_health_server():

    app = web.Application()

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    port = int(
        os.getenv("PORT", "8080")
    )

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        port
    )

    await site.start()

    logger.info(
        "Health server listening on port %s",
        port
    )


# ============================================================
# DOWNLOAD NEW BOT MEDIA
# ============================================================

async def download_message_media(message):

    attachment = message.effective_attachment

    if not attachment:
        return None

    telegram_file = None
    suffix = ".bin"

    if message.photo:
        telegram_file = await message.photo[-1].get_file()
        suffix = ".jpg"

    elif message.video:
        telegram_file = await message.video.get_file()
        suffix = ".mp4"

    elif message.document:
        telegram_file = await message.document.get_file()

    elif message.audio:
        telegram_file = await message.audio.get_file()
        suffix = ".mp3"

    elif message.voice:
        telegram_file = await message.voice.get_file()
        suffix = ".ogg"

    elif message.animation:
        telegram_file = await message.animation.get_file()
        suffix = ".mp4"

    elif message.video_note:
        telegram_file = await message.video_note.get_file()
        suffix = ".mp4"

    if not telegram_file:
        return None

    temp = tempfile.NamedTemporaryFile(
        delete=False,
        suffix=suffix
    )

    path = temp.name
    temp.close()

    await telegram_file.download_to_drive(path)

    return path


# ============================================================
# SEND + RECEIVE (COMBINED — SAME CONVERSATION)
# ============================================================
# IMPORTANT:
# Sending and waiting for the reply must happen inside the
# SAME conversation() block. If the message is sent before the
# conversation is opened, a fast reply from the old bot can
# arrive before the response listener is registered, and
# conversation.get_response() will miss it (silent timeout even
# though the old bot actually replied).

async def send_and_receive_old_bot(message):

    replies = []
    media_path = None

    try:

        async with user_client.conversation(
            OLD_BOT_USERNAME,
            timeout=REPLY_TIMEOUT,
            exclusive=True
        ) as conversation:

            # -----------------------------
            # SEND (inside the conversation)
            # -----------------------------

            if message.text:

                await conversation.send_message(
                    message.text
                )

                logger.info(
                    "Text sent to old bot."
                )

            else:

                media_path = await download_message_media(
                    message
                )

                if not media_path:
                    raise RuntimeError(
                        "Unsupported Telegram message type."
                    )

                caption = message.caption

                # Conversation doesn't wrap send_file, but since
                # this runs after the conversation is opened, the
                # response listener is already active and will
                # still catch the reply.
                await user_client.send_file(
                    OLD_BOT_USERNAME,
                    media_path,
                    caption=caption
                )

                logger.info(
                    "Media sent to old bot."
                )

            # -----------------------------
            # RECEIVE
            # -----------------------------

            try:

                first = await conversation.get_response(
                    timeout=REPLY_TIMEOUT
                )

                replies.append(first)

            except asyncio.TimeoutError:

                logger.warning(
                    "Old bot did not reply within %s seconds.",
                    REPLY_TIMEOUT
                )

                return replies

            # Capture additional messages that arrive
            # immediately after the first response.
            while True:

                try:

                    extra = await conversation.get_response(
                        timeout=2
                    )

                    replies.append(extra)

                except asyncio.TimeoutError:

                    break

    except Exception as exc:

        logger.exception(
            "Old bot conversation failed: %s",
            exc
        )

    finally:

        if media_path:

            try:
                os.remove(media_path)
            except OSError:
                pass

    return replies


# ============================================================
# FORWARD OLD BOT RESPONSE TO USER
# ============================================================

async def send_old_bot_reply_to_user(
    bot,
    chat_id,
    replies
):

    if not replies:

        await bot.send_message(
            chat_id=chat_id,
            text="⚠️ Old bot did not reply."
        )

        return

    for reply in replies:

        # -----------------------------
        # TEXT
        # -----------------------------

        if reply.message:

            try:

                await bot.send_message(
                    chat_id=chat_id,
                    text=reply.message
                )

            except Exception as exc:

                logger.exception(
                    "Text forwarding failed: %s",
                    exc
                )

        # -----------------------------
        # MEDIA
        # -----------------------------

        if reply.media:

            temp_path = None

            try:

                temp_path = await reply.download_media()

                if not temp_path:
                    continue

                caption = reply.text or None

                # PHOTO
                if reply.photo:

                    with open(
                        temp_path,
                        "rb"
                    ) as f:

                        await bot.send_photo(
                            chat_id=chat_id,
                            photo=f,
                            caption=caption
                        )

                # VIDEO
                elif reply.video:

                    with open(
                        temp_path,
                        "rb"
                    ) as f:

                        await bot.send_video(
                            chat_id=chat_id,
                            video=f,
                            caption=caption
                        )

                # AUDIO
                elif reply.audio:

                    with open(
                        temp_path,
                        "rb"
                    ) as f:

                        await bot.send_audio(
                            chat_id=chat_id,
                            audio=f,
                            caption=caption
                        )

                # VOICE
                elif reply.voice:

                    with open(
                        temp_path,
                        "rb"
                    ) as f:

                        await bot.send_voice(
                            chat_id=chat_id,
                            voice=f,
                            caption=caption
                        )

                # EVERYTHING ELSE
                else:

                    with open(
                        temp_path,
                        "rb"
                    ) as f:

                        await bot.send_document(
                            chat_id=chat_id,
                            document=f,
                            caption=caption
                        )

            except Exception as exc:

                logger.exception(
                    "Media forwarding failed: %s",
                    exc
                )

                try:

                    await bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ Received media from old bot, "
                             "but forwarding failed."
                    )

                except Exception:
                    pass

            finally:

                if temp_path:

                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass


# ============================================================
# PROCESS ONE QUEUED USER
# ============================================================

async def process_job(job: RelayJob):

    bot = job.bot

    status_message = None

    try:

        status_message = await bot.send_message(
            chat_id=job.chat_id,
            text="⏳ Processing..."
        )

        logger.info(
            "Processing user=%s message=%s",
            job.chat_id,
            job.message_id
        )

        # Send to old bot and wait for reply in the SAME
        # conversation, so no fast replies are missed.
        replies = await send_and_receive_old_bot(
            job.message
        )

        # Delete processing message.
        if status_message:

            try:
                await status_message.delete()
            except Exception:
                pass

        # Send replies to correct user.
        await send_old_bot_reply_to_user(
            bot,
            job.chat_id,
            replies
        )

    except Exception as exc:

        logger.exception(
            "Job failed for user %s: %s",
            job.chat_id,
            exc
        )

        try:

            await bot.send_message(
                chat_id=job.chat_id,
                text=(
                    "❌ Something went wrong while "
                    "processing your request."
                )
            )

        except Exception:
            pass


# ============================================================
# QUEUE WORKER
# ============================================================

async def queue_worker():

    logger.info("Queue worker started.")

    while True:

        job = await relay_queue.get()

        try:

            await process_job(job)

        except Exception as exc:

            logger.exception(
                "Worker error: %s",
                exc
            )

        finally:

            relay_queue.task_done()


# ============================================================
# NEW BOT MESSAGE HANDLER
# ============================================================

async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    if not message:
        return

    user = update.effective_user

    if not user:
        return

    # Only private chats.
    # Remove this check if you intentionally want groups.
    if update.effective_chat.type != "private":

        await message.reply_text(
            "Please use this bot in private chat."
        )

        return

    # Check queue capacity.
    if relay_queue.full():

        await message.reply_text(
            "⏳ Server is busy right now. "
            "Please try again in a moment."
        )

        return

    job = RelayJob(
        chat_id=message.chat_id,
        message_id=message.message_id,
        message=message,
        bot=context.bot
    )

    await relay_queue.put(job)

    position = relay_queue.qsize()

    await message.reply_text(
        f"📨 Request received.\n"
        f"Queue position: {position}"
    )

    logger.info(
        "Queued user=%s position=%s",
        user.id,
        position
    )


# ============================================================
# MAIN
# ============================================================

async def main():

    logger.info("Starting Telethon...")

    await user_client.start()

    me = await user_client.get_me()

    logger.info(
        "Telegram account connected: %s",
        me.username or me.id
    )

    # Health server
    await start_health_server()

    # New bot
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Accept normal messages and media.
    application.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND,
            handle_message
        )
    )

    await application.initialize()
    await application.start()

    # Start queue worker.
    worker_task = asyncio.create_task(
        queue_worker()
    )

    if application.updater:

        await application.updater.start_polling()

    logger.info(
        "Relay bot is ONLINE."
    )

    try:

        await asyncio.Event().wait()

    finally:

        worker_task.cancel()

        try:
            await worker_task
        except asyncio.CancelledError:
            pass

        if application.updater:

            await application.updater.stop()

        await application.stop()
        await application.shutdown()

        await user_client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
