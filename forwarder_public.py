import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import timezone
from pathlib import Path

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─────────────────────────────────────────────
# ENV
# ─────────────────────────────────────────────
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

DATA_REPO_PAT = os.environ["DATA_REPO_PAT"]
DATA_REPO_URL = os.environ["DATA_REPO_URL"]

ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
RUN_DURATION = 20400
SUBSCRIBER_REFRESH_INTERVAL = 60

REACTION_EDIT_SCHEDULE = [
    (180, "3m"),
    (300, "5m"),
    (600, "10m"),
    (900, "15m"),
    (1500, "25m"),
    (1800, "30m"),
    (3600, "1H"),
    (7200, "2H"),
]

MAX_FILE_SIZE_MB = {
    "Image": 10,
    "Video": 50,
    "File": 50,
    "Music": 50,
    "Voice": 10,
    "Gif": 50,
}

VPN_PREFIXES = (
    "vmess://",
    "vless://",
    "trojan://",
    "ss://",
    "ssr://",
    "hysteria://",
    "hysteria2://",
    "tuic://",
    "wireguard://",
    "socks5://",
)

# ─────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────
DATA_REPO_DIR = Path("data_repo")

STATE_FILE = DATA_REPO_DIR / "state.json"
SUBSCRIBERS_FILE = DATA_REPO_DIR / "subscribers.json"
CHANNELS_FILE = Path("channels.json")

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

logger = logging.getLogger("forwarder")

# ─────────────────────────────────────────────
# MARKDOWN
# ─────────────────────────────────────────────
def md_bold(text):
    return f"**{text}**"


def md_italic(text):
    return f"__{text}__"


def md_mono(text):
    return f"`{text}`"


def md_code(text):
    return f"```\n{text}\n```"


def is_proxy_line(line):
    line = line.strip().lower()
    return any(line.startswith(prefix) for prefix in VPN_PREFIXES)


def format_proxy_text(text):
    if not text:
        return ""

    lines = text.splitlines()

    result = []
    proxy_buffer = []

    def flush():
        nonlocal proxy_buffer

        if proxy_buffer:
            result.append(md_code("\n".join(proxy_buffer)))
            proxy_buffer = []

    for line in lines:
        stripped = line.strip()

        if is_proxy_line(stripped):
            proxy_buffer.append(stripped)
        else:
            flush()
            result.append(line)

    flush()

    return "\n".join(result)


# ─────────────────────────────────────────────
# UX
# ─────────────────────────────────────────────
def build_header(channel_name, msg_date):
    date_str = msg_date.astimezone(timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )

    return (
        "━━━━━━━━━━━━━━━━━━\n"
        f"📡 {md_bold(channel_name)}\n"
        f"🕐 {md_italic(date_str)}\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def build_message(channel_name, msg_date, body):
    header = build_header(channel_name, msg_date)

    body = format_proxy_text(body)

    if body:
        return f"{header}\n\n{body}"

    return header


def build_welcome():
    return (
        f"{md_bold('شما پذیرفته شدید')}\n\n"
        "کانفیگ‌های جدید به‌صورت خودکار ارسال می‌شوند.\n\n"
        "لینک‌های پروکسی داخل بخش قابل‌کپی قرار می‌گیرند."
    )


def build_skip_message(file_type, size_mb):
    return (
        f"⚠️ {md_bold('فایل بزرگ رد شد')}\n\n"
        f"نوع: {md_mono(file_type)}\n"
        f"حجم: {md_mono(f'{size_mb:.1f} MB')}"
    )


def get_top_reactions(message):
    if not message.reactions:
        return ""

    if not message.reactions.results:
        return ""

    reactions = []

    for item in message.reactions.results:
        emoji = getattr(item.reaction, "emoticon", str(item.reaction))
        reactions.append((emoji, item.count))

    reactions.sort(key=lambda x: x[1], reverse=True)

    top = reactions[:3]

    return "  ".join(
        f"{emoji} {md_bold(str(count))}"
        for emoji, count in top
    )

# ─────────────────────────────────────────────
# FILES
# ─────────────────────────────────────────────
def load_channels():
    if not CHANNELS_FILE.exists():
        logger.error("channels.json not found")
        sys.exit(1)

    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)

    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_subscribers():
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))

    return set()


def save_subscribers(subscribers):
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)

    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(
            sorted(subscribers),
            f,
            ensure_ascii=False,
            indent=2,
        )

# ─────────────────────────────────────────────
# RUBIKA API
# ─────────────────────────────────────────────
def rubika_post(method, payload=None, timeout=20):
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{method}"

    try:
        response = requests.post(
            url,
            json=payload or {},
            timeout=timeout,
        )

        if response.status_code != 200:
            logger.error(
                f"{method} HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
            return None

        return response.json()

    except Exception as e:
        logger.error(f"{method} error: {e}")
        return None


def send_text(chat_id, text):
    data = rubika_post(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
    )

    if not data:
        return False, None

    if data.get("status") == "OK":
        msg_id = data.get("data", {}).get("message_id")
        return True, msg_id

    return False, None


def edit_text(chat_id, message_id, text):
    data = rubika_post(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "Markdown",
        }
    )

    return bool(data and data.get("status") == "OK")


def send_file(chat_id, file_id, caption):
    data = rubika_post(
        "sendFile",
        {
            "chat_id": chat_id,
            "file_id": file_id,
            "text": caption,
            "parse_mode": "Markdown",
        }
    )

    if not data:
        return False, None

    if data.get("status") == "OK":
        msg_id = data.get("data", {}).get("message_id")
        return True, msg_id

    return False, None


def upload_file(file_bytes, filename, file_type):
    req = rubika_post(
        "requestSendFile",
        {
            "type": file_type
        }
    )

    if not req:
        return None

    upload_url = req.get("data", {}).get("upload_url")

    if not upload_url:
        logger.error("upload_url missing")
        return None

    try:
        response = requests.post(
            upload_url,
            files={
                "file": (filename, file_bytes)
            },
            timeout=120,
        )

        if response.status_code != 200:
            logger.error(
                f"Upload HTTP {response.status_code}"
            )
            return None

        data = response.json()

        file_id = data.get("data", {}).get("file_id")

        if file_id:
            return file_id

    except Exception as e:
        logger.error(f"Upload error: {e}")

    return None

# ─────────────────────────────────────────────
# GET UPDATES
# ─────────────────────────────────────────────
def extract_new_subscribers(updates, subscribers):
    new_users = set()

    for update in updates:
        chat_id = update.get("chat_id")

        if not chat_id:
            continue

        update_type = update.get("type", "")

        # StartedBot
        if update_type == "StartedBot":
            if chat_id not in subscribers:
                new_users.add(chat_id)

            continue

        # /start message
        if update_type == "NewMessage":
            msg = update.get("new_message", {})

            text = str(
                msg.get("text", "")
            ).strip().lower()

            aux_data = msg.get("aux_data", {})

            button_id = str(
                aux_data.get("button_id", "")
            ).lower()

            if text == "/start" or button_id == "start":
                if chat_id not in subscribers:
                    new_users.add(chat_id)

    return new_users


def fetch_updates(subscribers, state):
    payload = {
        "limit": 200,
        "state": "all",
    }

    offset_id = state.get("rubika_offset")

    if offset_id:
        payload["offset_id"] = offset_id

    data = rubika_post(
        "getUpdates",
        payload,
        timeout=40,
    )

    if not data:
        return subscribers, state

    updates = data.get("data", {}).get("updates", [])

    next_offset = data.get("data", {}).get(
        "next_offset_id"
    )

    # TEMP DEBUG
    logger.info(
        f"Rubika updates: {len(updates)}"
    )

    new_users = extract_new_subscribers(
        updates,
        subscribers,
    )

    if ADMIN_CHAT_ID:
        subscribers.add(ADMIN_CHAT_ID)

    if new_users:
        for chat_id in sorted(new_users):
            logger.info(
                f"New subscriber: {chat_id}"
            )

            ok, _ = send_text(
                chat_id,
                build_welcome(),
            )

            if ok:
                subscribers.add(chat_id)

        save_subscribers(subscribers)

    if next_offset:
        state["rubika_offset"] = next_offset
        save_state(state)

    return subscribers, state

# ─────────────────────────────────────────────
# MEDIA
# ─────────────────────────────────────────────
def get_file_type(media):
    if getattr(media, "photo", None):
        return "Image"

    if getattr(media, "video", None):
        return "Video"

    if getattr(media, "voice", None):
        return "Voice"

    if getattr(media, "audio", None):
        return "Music"

    return "File"

# ─────────────────────────────────────────────
# BROADCAST
# ─────────────────────────────────────────────
def broadcast_text(subscribers, text):
    results = []

    for chat_id in list(subscribers):
        ok, msg_id = send_text(chat_id, text)

        if ok and msg_id:
            results.append({
                "chat_id": chat_id,
                "msg_id": msg_id,
                "full_text": text,
            })

    return results


def broadcast_file(subscribers, file_id, caption):
    results = []

    for chat_id in list(subscribers):
        ok, msg_id = send_file(
            chat_id,
            file_id,
            caption,
        )

        if ok and msg_id:
            results.append({
                "chat_id": chat_id,
                "msg_id": msg_id,
                "full_text": caption,
            })

    return results

# ─────────────────────────────────────────────
# REACTIONS
# ─────────────────────────────────────────────
pending_edits = {}


async def delayed_reaction_updates(
    client,
    channel_name,
    tg_msg_id,
):
    key = (channel_name, tg_msg_id)

    start = time.monotonic()

    for seconds, label in REACTION_EDIT_SCHEDULE:
        wait = seconds - (
            time.monotonic() - start
        )

        if wait > 0:
            await asyncio.sleep(wait)

        entries = pending_edits.get(key)

        if not entries:
            return

        try:
            msg = await client.get_messages(
                channel_name,
                ids=tg_msg_id,
            )

            if not msg:
                continue

            reactions = get_top_reactions(msg)

            if not reactions:
                continue

            for entry in entries:
                new_text = (
                    f"{entry['full_text']}\n\n"
                    f"💬 {reactions}"
                )

                edit_text(
                    entry["chat_id"],
                    entry["msg_id"],
                    new_text,
                )

            logger.info(
                f"Reaction edit {label}: "
                f"{channel_name} {tg_msg_id}"
            )

        except Exception as e:
            logger.error(
                f"Reaction edit error: {e}"
            )

    pending_edits.pop(key, None)

# ─────────────────────────────────────────────
# FORWARD
# ─────────────────────────────────────────────
async def forward_message(
    client,
    message,
    channel_name,
    state,
    subscribers,
):
    if message.id <= state.get(channel_name, 0):
        return

    if not subscribers:
        return

    msg_date = (
        message.date.replace(
            tzinfo=timezone.utc
        )
        if message.date.tzinfo is None
        else message.date
    )

    # TEXT
    if message.text and not message.media:
        text = build_message(
            channel_name,
            msg_date,
            message.text,
        )

        deliveries = broadcast_text(
            subscribers,
            text,
        )

        if deliveries:
            state[channel_name] = message.id
            save_state(state)

            key = (
                channel_name,
                message.id,
            )

            pending_edits[key] = deliveries

            asyncio.create_task(
                delayed_reaction_updates(
                    client,
                    channel_name,
                    message.id,
                )
            )

        return

    # MEDIA
    if not message.media:
        return

    if not message.file:
        return

    file_type = get_file_type(
        message.media
    )

    max_bytes = (
        MAX_FILE_SIZE_MB.get(
            file_type,
            50,
        )
        * 1024
        * 1024
    )

    if message.file.size > max_bytes:
        size_mb = (
            message.file.size
            / 1024
            / 1024
        )

        broadcast_text(
            subscribers,
            build_skip_message(
                file_type,
                size_mb,
            )
        )

        return

    try:
        file_bytes = await client.download_media(
            message,
            file=bytes,
        )

    except Exception as e:
        logger.error(
            f"Download failed: {e}"
        )
        return

    filename = (
        message.file.name
        or "file.bin"
    )

    file_id = upload_file(
        file_bytes,
        filename,
        file_type,
    )

    if not file_id:
        return

    caption = build_message(
        channel_name,
        msg_date,
        message.text or "",
    )

    deliveries = broadcast_file(
        subscribers,
        file_id,
        caption,
    )

    if deliveries:
        state[channel_name] = message.id
        save_state(state)

        key = (
            channel_name,
            message.id,
        )

        pending_edits[key] = deliveries

        asyncio.create_task(
            delayed_reaction_updates(
                client,
                channel_name,
                message.id,
            )
        )

# ─────────────────────────────────────────────
# GIT
# ─────────────────────────────────────────────
def clone_repo():
    if DATA_REPO_DIR.exists():
        shutil.rmtree(DATA_REPO_DIR)

    helper = Path(
        "/tmp/git-helper.sh"
    )

    helper.write_text(
        "#!/bin/sh\n"
        "echo 'username=x-access-token'\n"
        f"echo 'password={DATA_REPO_PAT}'\n"
    )

    os.chmod(helper, 0o755)

    try:
        subprocess.run(
            [
                "git",
                "-c",
                f"credential.helper={helper}",
                "clone",
                "--depth",
                "1",
                DATA_REPO_URL,
                str(DATA_REPO_DIR),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        logger.info(
            "Data repo cloned"
        )

    finally:
        helper.unlink(
            missing_ok=True
        )


def push_repo():
    try:
        subprocess.run(
            [
                "git",
                "config",
                "user.email",
                "actions@github.com",
            ],
            cwd=DATA_REPO_DIR,
            check=True,
        )

        subprocess.run(
            [
                "git",
                "config",
                "user.name",
                "GitHub Actions",
            ],
            cwd=DATA_REPO_DIR,
            check=True,
        )

        subprocess.run(
            [
                "git",
                "add",
                ".",
            ],
            cwd=DATA_REPO_DIR,
            check=True,
        )

        diff = subprocess.run(
            [
                "git",
                "diff",
                "--staged",
                "--quiet",
            ],
            cwd=DATA_REPO_DIR,
        )

        if diff.returncode != 0:
            subprocess.run(
                [
                    "git",
                    "commit",
                    "-m",
                    "update subscribers/state",
                ],
                cwd=DATA_REPO_DIR,
                check=True,
            )

            subprocess.run(
                [
                    "git",
                    "push",
                ],
                cwd=DATA_REPO_DIR,
                check=True,
            )

            logger.info(
                "Data pushed"
            )

    except Exception as e:
        logger.error(
            f"Push error: {e}"
        )

# ─────────────────────────────────────────────
# CATCHUP
# ─────────────────────────────────────────────
async def catchup(
    client,
    channels,
    state,
    subscribers,
):
    if not state:
        logger.info(
            "First run. Setting markers."
        )

        for channel in channels:
            try:
                msgs = await client.get_messages(
                    channel,
                    limit=1,
                )

                state[channel] = (
                    msgs[0].id
                    if msgs
                    else 0
                )

            except Exception as e:
                logger.error(
                    f"Marker error {channel}: {e}"
                )

        save_state(state)
        return

    for channel in channels:
        try:
            msgs = await client.get_messages(
                channel,
                limit=10,
            )

            for msg in reversed(msgs):
                if msg.id <= state.get(
                    channel,
                    0,
                ):
                    continue

                await forward_message(
                    client,
                    msg,
                    channel,
                    state,
                    subscribers,
                )

        except Exception as e:
            logger.error(
                f"Catchup error: {e}"
            )

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    clone_repo()

    channels = load_channels()

    state = load_state()

    subscribers = load_subscribers()

    # FIRST POLL IMMEDIATELY
    subscribers, state = fetch_updates(
        subscribers,
        state,
    )

    client = TelegramClient(
        StringSession(
            STRING_SESSION
        ),
        API_ID,
        API_HASH,
    )

    await client.start()

    logger.info(
        "Telegram connected"
    )

    await catchup(
        client,
        channels,
        state,
        subscribers,
    )

    @client.on(
        events.NewMessage(
            chats=channels
        )
    )
    async def new_message(event):
        try:
            chat = await event.get_chat()

            channel_name = (
                chat.title
                or chat.username
                or str(chat.id)
            )

            await forward_message(
                client,
                event.message,
                channel_name,
                state,
                subscribers,
            )

        except Exception as e:
            logger.error(
                f"Handler error: {e}"
            )

    async def poll_subscribers():
        nonlocal subscribers
        nonlocal state

        while True:
            try:
                subscribers, state = fetch_updates(
                    subscribers,
                    state,
                )

            except Exception as e:
                logger.error(
                    f"Poll error: {e}"
                )

            await asyncio.sleep(
                SUBSCRIBER_REFRESH_INTERVAL
            )

    asyncio.create_task(
        poll_subscribers()
    )

    start = time.monotonic()

    while True:
        if (
            time.monotonic() - start
            >= RUN_DURATION
        ):
            break

        await asyncio.sleep(30)

    save_state(state)

    save_subscribers(
        subscribers
    )

    push_repo()

    await client.disconnect()

    logger.info("Finished")


if __name__ == "__main__":
    asyncio.run(main())
