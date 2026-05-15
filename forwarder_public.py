# forwarder_public.py

import os
import re
import io
import json
import time
import shutil
import signal
import asyncio
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPhoto
from telethon.errors import FloodWaitError

# =========================================================
# ENV
# =========================================================

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

DATA_REPO_PAT = os.environ["DATA_REPO_PAT"]
DATA_REPO_URL = os.environ["DATA_REPO_URL"]

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

# =========================================================
# CONFIG
# =========================================================

RUN_DURATION = 20400

DATA_DIR = Path("data_repo")
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
STATE_FILE = DATA_DIR / "state.json"

CHANNELS_FILE = "channels.json"

MAX_IMAGE_SIZE = 10 * 1024 * 1024
MAX_VIDEO_SIZE = 50 * 1024 * 1024
MAX_AUDIO_SIZE = 25 * 1024 * 1024
MAX_FILE_SIZE = 50 * 1024 * 1024

RUBIKA_API_BASE = "https://botapi.rubika.ir/v3"

REACTION_SCHEDULE = [
    180,
    300,
    600,
    900,
    1500,
    2400,
    3600,
    5400,
    7200,
]

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger("forwarder")

# =========================================================
# GLOBALS
# =========================================================

subscribers = set()
state = {}
pending_edits = {}
shutdown_flag = False

# =========================================================
# UTIL
# =========================================================

def utc_now():
    return datetime.now(timezone.utc)

def ensure_json_file(path, default):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, ensure_ascii=False, indent=2)

def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_subscribers():
    global subscribers

    ensure_json_file(SUBSCRIBERS_FILE, [])

    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        subscribers = set(str(x) for x in data)

        logger.info(f"Loaded {len(subscribers)} subscribers")

    except Exception:
        logger.exception("Failed loading subscribers")

def save_subscribers():
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(list(subscribers)), f, ensure_ascii=False, indent=2)

def load_state():
    global state

    ensure_json_file(STATE_FILE, {})

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

    except Exception:
        logger.exception("Failed loading state")
        state = {}

def save_state():
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# =========================================================
# GIT
# =========================================================

def clone_repo():
    if DATA_DIR.exists():
        shutil.rmtree(DATA_DIR)

    auth_url = DATA_REPO_URL.replace(
        "https://",
        f"https://{DATA_REPO_PAT}@"
    )

    logger.info("Cloning data repo")

    subprocess.run(
        ["git", "clone", auth_url, str(DATA_DIR)],
        check=True
    )

def git_commit_push(message):
    try:
        subprocess.run(
            ["git", "-C", str(DATA_DIR), "add", "."],
            check=True
        )

        result = subprocess.run(
            ["git", "-C", str(DATA_DIR), "status", "--porcelain"],
            capture_output=True,
            text=True
        )

        if not result.stdout.strip():
            logger.info("No git changes")
            return

        subprocess.run(
            ["git", "-C", str(DATA_DIR), "commit", "-m", message],
            check=True
        )

        env = os.environ.copy()

        askpass = tempfile.NamedTemporaryFile(
            delete=False,
            mode="w",
            suffix=".sh"
        )

        askpass.write(
            "#!/bin/sh\n"
            f"echo '{DATA_REPO_PAT}'\n"
        )

        askpass.close()

        os.chmod(askpass.name, 0o700)

        env["GIT_ASKPASS"] = askpass.name

        subprocess.run(
            ["git", "-C", str(DATA_DIR), "push"],
            check=True,
            env=env
        )

        os.unlink(askpass.name)

        logger.info("Git push completed")

    except Exception:
        logger.exception("Git push failed")

# =========================================================
# RUBIKA API
# =========================================================

async def rubika_request(
    session,
    method,
    payload,
    retries=3
):
    url = f"{RUBIKA_API_BASE}/{RUBIKA_BOT_TOKEN}/{method}"

    for attempt in range(retries):

        try:
            async with session.post(
                url,
                json=payload,
                timeout=120
            ) as resp:

                text = await resp.text()

                if resp.status in [429, 502, 503, 504]:

                    wait_time = (2 ** attempt)

                    logger.warning(
                        f"{method} retry in {wait_time}s"
                    )

                    await asyncio.sleep(wait_time)
                    continue

                if resp.status >= 400:
                    raise Exception(
                        f"HTTP {resp.status}: {text}"
                    )

                data = json.loads(text)

                if not data.get("ok", False):

                    desc = data.get("description", "")

                    if "AUTH" in desc.upper():
                        logger.critical("Rubika auth failed")

                    raise Exception(desc)

                return data

        except Exception as e:

            if attempt == retries - 1:
                raise

            wait_time = (2 ** attempt)

            logger.warning(
                f"Rubika request error: {e}"
            )

            await asyncio.sleep(wait_time)

async def rubika_send_message(
    session,
    chat_id,
    text
):
    payload = {
        "chat_id": chat_id,
        "text": text
    }

    return await rubika_request(
        session,
        "sendMessage",
        payload
    )

async def rubika_edit_message(
    session,
    chat_id,
    message_id,
    text
):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text
    }

    return await rubika_request(
        session,
        "editMessageText",
        payload
    )

async def rubika_get_updates(session):
    return await rubika_request(
        session,
        "getUpdates",
        {}
    )

async def rubika_request_send_file(
    session,
    file_name,
    size
):
    payload = {
        "file_name": file_name,
        "size": size
    }

    return await rubika_request(
        session,
        "requestSendFile",
        payload
    )

async def rubika_upload_file(
    upload_url,
    file_path
):
    async with aiohttp.ClientSession() as session:

        with open(file_path, "rb") as f:

            form = aiohttp.FormData()
            form.add_field(
                "file",
                f,
                filename=os.path.basename(file_path)
            )

            async with session.post(
                upload_url,
                data=form
            ) as resp:

                if resp.status >= 400:
                    raise Exception(
                        f"Upload failed {resp.status}"
                    )

                return await resp.json()

async def rubika_send_file(
    session,
    chat_id,
    file_id,
    file_type,
    caption=""
):
    payload = {
        "chat_id": chat_id,
        "file_id": file_id,
        "file_type": file_type,
        "caption": caption
    }

    return await rubika_request(
        session,
        "sendFile",
        payload
    )

# =========================================================
# FORMATTER
# =========================================================

VPN_PATTERN = re.compile(
    r"^(vmess://|vless://|trojan://|ss://|ssr://|hy2://|hysteria://|tuic://)",
    re.I
)

IP_PATTERN = re.compile(
    r"^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?$"
)

def markdown_escape(text):
    return text.replace("`", "\\`")

def make_header(channel_name):

    now = utc_now().strftime("%Y-%m-%d %H:%M UTC")

    return (
        "━━━━━━━━━━━━━━\n"
        f"**{channel_name}**\n"
        f"__{now}__\n"
        "━━━━━━━━━━━━━━\n\n"
    )

def format_proxy_line(line):

    clean = markdown_escape(line.strip())

    return f"> ```{clean}```"

def format_message(channel_name, text):

    if not text:
        text = ""

    lines = text.splitlines()

    formatted = []

    for line in lines:

        stripped = line.strip()

        if not stripped:
            formatted.append("")
            continue

        if VPN_PATTERN.match(stripped):
            formatted.append(format_proxy_line(stripped))
            continue

        if IP_PATTERN.match(stripped):
            formatted.append(format_proxy_line(stripped))
            continue

        formatted.append(stripped)

    body = "\n".join(formatted)

    return make_header(channel_name) + body

# =========================================================
# REACTIONS
# =========================================================

async def reaction_worker(
    client,
    session,
    original_chat,
    original_msg_id,
    rubika_chat_id,
    rubika_msg_id,
    original_text
):

    for delay in REACTION_SCHEDULE:

        try:

            await asyncio.sleep(delay)

            msg = await client.get_messages(
                original_chat,
                ids=original_msg_id
            )

            if not msg:
                return

            reactions = []

            if msg.reactions and msg.reactions.results:

                top = msg.reactions.results[:3]

                for r in top:
                    reactions.append(
                        f"{r.reaction.emoticon} {r.count}"
                    )

            if not reactions:
                continue

            new_text = (
                original_text
                + "\n\n━━━━━━━━━━━━━━\n"
                + " ".join(reactions)
            )

            await rubika_edit_message(
                session,
                rubika_chat_id,
                rubika_msg_id,
                new_text
            )

            logger.info(
                f"Reaction edit updated {rubika_msg_id}"
            )

        except Exception:
            logger.exception("Reaction update failed")

# =========================================================
# BROADCAST
# =========================================================

async def broadcast_text(
    session,
    client,
    channel_name,
    message
):

    formatted = format_message(
        channel_name,
        message.message or ""
    )

    for chat_id in list(subscribers):

        try:

            result = await rubika_send_message(
                session,
                chat_id,
                formatted
            )

            msg_id = (
                result
                .get("result", {})
                .get("message_update", {})
                .get("message_id")
            )

            if msg_id:

                asyncio.create_task(
                    reaction_worker(
                        client,
                        session,
                        message.chat_id,
                        message.id,
                        chat_id,
                        msg_id,
                        formatted
                    )
                )

            await asyncio.sleep(0.3)

        except Exception:
            logger.exception(
                f"Text broadcast failed to {chat_id}"
            )

async def broadcast_media(
    session,
    client,
    channel_name,
    message
):

    caption = format_message(
        channel_name,
        message.message or ""
    )

    tmp = None

    try:

        tmp = await message.download_media(
            file=bytes
        )

        if not tmp:
            return

        file_bytes = tmp
        size = len(file_bytes)

        if message.photo:
            limit = MAX_IMAGE_SIZE
            file_type = "Image"

        elif message.video:
            limit = MAX_VIDEO_SIZE
            file_type = "Video"

        elif message.voice:
            limit = MAX_AUDIO_SIZE
            file_type = "Voice"

        elif message.audio:
            limit = MAX_AUDIO_SIZE
            file_type = "Audio"

        else:
            limit = MAX_FILE_SIZE
            file_type = "File"

        if size > limit:

            notice = (
                caption
                + "\n\n"
                + "⚠️ File too large to upload."
            )

            await broadcast_text(
                session,
                client,
                channel_name,
                type(
                    "obj",
                    (),
                    {"message": notice}
                )
            )

            return

        suffix = ".bin"

        if message.file and message.file.ext:
            suffix = message.file.ext

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=suffix
        ) as f:

            f.write(file_bytes)
            temp_path = f.name

        req = await rubika_request_send_file(
            session,
            os.path.basename(temp_path),
            size
        )

        upload_url = req["result"]["upload_url"]

        upload = await rubika_upload_file(
            upload_url,
            temp_path
        )

        file_id = upload["data"]["file_id"]

        for chat_id in list(subscribers):

            try:

                result = await rubika_send_file(
                    session,
                    chat_id,
                    file_id,
                    file_type,
                    caption
                )

                msg_id = (
                    result
                    .get("result", {})
                    .get("message_update", {})
                    .get("message_id")
                )

                if msg_id:

                    asyncio.create_task(
                        reaction_worker(
                            client,
                            session,
                            message.chat_id,
                            message.id,
                            chat_id,
                            msg_id,
                            caption
                        )
                    )

                await asyncio.sleep(0.5)

            except Exception:
                logger.exception(
                    f"Media broadcast failed {chat_id}"
                )

        os.unlink(temp_path)

    except Exception:
        logger.exception("Media broadcast error")

# =========================================================
# SUBSCRIBERS
# =========================================================

WELCOME_MESSAGE = """
**Welcome 👋**

This bot forwards the latest VPN and proxy configs from Telegram channels.

Features:
• Fast updates
• Clean formatting
• Easy copy UI
• Media support
• Reaction stats

You will automatically receive new posts.
"""

async def subscriber_poller(session):

    global subscribers

    while not shutdown_flag:

        try:

            data = await rubika_get_updates(session)

            updates = data.get("result", {}).get("updates", [])

            for update in updates:

                try:

                    message = update.get("new_message", {})

                    chat_id = str(
                        message.get("chat_id", "")
                    )

                    if not chat_id:
                        continue

                    text = (
                        message
                        .get("text", "")
                        .strip()
                        .lower()
                    )

                    started = (
                        "startedbot"
                        in json.dumps(update).lower()
                    )

                    if (
                        started
                        or text == "/start"
                        or text == "start"
                    ):

                        if chat_id not in subscribers:

                            subscribers.add(chat_id)

                            save_subscribers()

                            git_commit_push(
                                f"Add subscriber {chat_id}"
                            )

                            logger.info(
                                f"New subscriber {chat_id}"
                            )

                            await rubika_send_message(
                                session,
                                chat_id,
                                WELCOME_MESSAGE
                            )

                except Exception:
                    logger.exception(
                        "Subscriber parse failed"
                    )

        except Exception:
            logger.exception("Subscriber poll failed")

        await asyncio.sleep(60)

# =========================================================
# TELEGRAM
# =========================================================

client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH
)

async def process_message(
    session,
    event
):

    try:

        message = event.message

        channel_name = (
            getattr(event.chat, "title", None)
            or getattr(event.chat, "username", None)
            or "Telegram Channel"
        )

        logger.info(
            f"New message from {channel_name}"
        )

        state[str(message.chat_id)] = message.id
        save_state()

        if message.media:
            await broadcast_media(
                session,
                client,
                channel_name,
                message
            )

        else:
            await broadcast_text(
                session,
                client,
                channel_name,
                message
            )

    except FloodWaitError as e:

        logger.warning(
            f"Flood wait {e.seconds}s"
        )

        await asyncio.sleep(e.seconds)

    except Exception:
        logger.exception("Message process failed")

async def catch_up(session, channels):

    logger.info("Running catch-up")

    for ch in channels:

        try:

            entity = await client.get_entity(ch)

            last_id = state.get(str(entity.id), 0)

            messages = await client.get_messages(
                entity,
                limit=10
            )

            messages = list(reversed(messages))

            for msg in messages:

                if msg.id <= last_id:
                    continue

                fake_event = type(
                    "obj",
                    (),
                    {
                        "message": msg,
                        "chat": entity
                    }
                )

                await process_message(
                    session,
                    fake_event
                )

        except Exception:
            logger.exception(
                f"Catch-up failed for {ch}"
            )

# =========================================================
# SHUTDOWN
# =========================================================

def stop_signal(*args):
    global shutdown_flag
    shutdown_flag = True

signal.signal(signal.SIGINT, stop_signal)
signal.signal(signal.SIGTERM, stop_signal)

# =========================================================
# MAIN
# =========================================================

async def main():

    global shutdown_flag

    logger.info("Booting forwarder")

    clone_repo()

    load_subscribers()
    load_state()

    channels = load_channels()

    logger.info(
        f"Loaded {len(channels)} channels"
    )

    async with aiohttp.ClientSession() as session:

        await client.start()

        logger.info("Telegram connected")

        await catch_up(session, channels)

        @client.on(events.NewMessage(chats=channels))
        async def handler(event):
            await process_message(session, event)

        asyncio.create_task(
            subscriber_poller(session)
        )

        started = time.time()

        while not shutdown_flag:

            elapsed = time.time() - started

            if elapsed >= RUN_DURATION:
                logger.info("Run duration reached")
                break

            await asyncio.sleep(5)

        logger.info("Saving state")

        save_state()
        save_subscribers()

        git_commit_push("Final save")

        await client.disconnect()

        logger.info("Shutdown complete")

# =========================================================
# ENTRY
# =========================================================

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        pass

    except Exception:
        logger.exception("Fatal crash")
