# forwarder_public.py

import os
import re
import json
import time
import asyncio
import logging
import tempfile
import subprocess
import shutil
import signal
from pathlib import Path
from datetime import datetime, timezone

import aiohttp
from telethon import TelegramClient, events
from telethon.sessions import StringSession
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

RUBIKA_API_BASE = "https://botapi.rubika.ir/v3"

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
shutdown_flag = False

# =========================================================
# FILE HELPERS
# =========================================================

def ensure_file(path, default):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default, f, indent=2)

def load_json(path, default):
    ensure_file(path, default)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

# =========================================================
# LOAD DATA
# =========================================================

def load_subscribers():
    global subscribers
    data = load_json(SUBSCRIBERS_FILE, [])
    subscribers = set(map(str, data))
    logger.info(f"Loaded {len(subscribers)} subscribers")

def save_subscribers():
    save_json(SUBSCRIBERS_FILE, list(subscribers))

def load_state():
    global state
    state = load_json(STATE_FILE, {})

def save_state():
    save_json(STATE_FILE, state)

def load_channels():
    return load_json(CHANNELS_FILE, [])

# =========================================================
# RUBIKA API
# =========================================================

async def rubika_request(session, method, payload):
    url = f"{RUBIKA_API_BASE}/{RUBIKA_BOT_TOKEN}/{method}"

    try:
        async with session.post(url, json=payload) as r:
            data = await r.json()

            if not data.get("ok"):
                raise Exception(data.get("description", "unknown error"))

            return data

    except Exception as e:
        raise Exception(str(e))


async def rubika_send_message(session, chat_id, text):
    return await rubika_request(session, "sendMessage", {
        "chat_id": chat_id,
        "text": text
    })

# =========================================================
# SAFE SEND (FIX -15 ISSUE)
# =========================================================

async def safe_send(session, chat_id, text):
    try:
        return await rubika_send_message(session, chat_id, text)

    except Exception as e:
        msg = str(e)
        logger.warning(f"send failed {chat_id}: {msg}")

        if "-15" in msg or "403" in msg:
            subscribers.discard(chat_id)
            save_subscribers()
            logger.info(f"Removed dead subscriber {chat_id}")

        return None

# =========================================================
# FORMATTER
# =========================================================

VPN_PATTERN = re.compile(r"^(vmess://|vless://|ss://|trojan://)", re.I)
IP_PATTERN = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}(:\d+)?$")

def format_message(channel, text):
    lines = text.splitlines()
    out = []

    header = (
        "━━━━━━━━━━━━━━\n"
        f"{channel}\n"
        f"{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n"
        "━━━━━━━━━━━━━━\n\n"
    )

    for l in lines:
        l = l.strip()

        if not l:
            continue

        if VPN_PATTERN.match(l) or IP_PATTERN.match(l):
            out.append(f"> ```{l}```")
        else:
            out.append(l)

    return header + "\n".join(out)

# =========================================================
# BROADCAST TEXT
# =========================================================

async def broadcast_text(session, channel_name, message):
    text = format_message(channel_name, message.message or "")

    for chat_id in list(subscribers):
        result = await safe_send(session, chat_id, text)

        if not result:
            continue

        await asyncio.sleep(0.2)

# =========================================================
# TELEGRAM CLIENT
# =========================================================

client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH
)

async def process(event, session):
    msg = event.message

    channel = (
        getattr(event.chat, "title", None)
        or "channel"
    )

    state[str(msg.chat_id)] = msg.id
    save_state()

    await broadcast_text(session, channel, msg)

# =========================================================
# SUBSCRIBER POLLER
# =========================================================

async def poll_subscribers(session):
    while not shutdown_flag:
        try:
            # placeholder for Rubika updates if needed
            await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"poll error {e}")

# =========================================================
# SHUTDOWN
# =========================================================

def stop(*_):
    global shutdown_flag
    shutdown_flag = True

signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

# =========================================================
# MAIN
# =========================================================

async def main():
    global shutdown_flag

    logger.info("starting")

    load_subscribers()
    load_state()
    channels = load_channels()

    async with aiohttp.ClientSession() as session:

        await client.start()
        logger.info("telegram connected")

        @client.on(events.NewMessage(chats=channels))
        async def handler(event):
            await process(event, session)

        start = time.time()

        while not shutdown_flag:
            if time.time() - start > RUN_DURATION:
                break
            await asyncio.sleep(5)

        save_state()
        save_subscribers()

        await client.disconnect()

        logger.info("shutdown done")

# =========================================================
# ENTRY
# =========================================================

if __name__ == "__main__":
    asyncio.run(main())
