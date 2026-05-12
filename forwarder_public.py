import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()
DATA_REPO_URL = os.environ.get("DATA_REPO_URL", "").strip()
DATA_REPO_PAT = os.environ.get("DATA_REPO_PAT", "").strip()

DATA_REPO_DIR = Path("data_repo")
CHANNELS_FILE = Path("channels.json")
STATE_FILE = DATA_REPO_DIR / "state.json"
SUBSCRIBERS_FILE = DATA_REPO_DIR / "subscribers.json"

RUN_DURATION = 20400  # 5h 40m, fits GitHub Actions 6h job limit
SUBSCRIBER_REFRESH_INTERVAL = 60

MAX_FILE_SIZE_MB = {
    "Image": 10,
    "Video": 50,
    "File": 50,
    "Music": 50,
    "Voice": 10,
    "Gif": 50,
}

REACTION_EDIT_SCHEDULE = [
    (180, "3 min"),
    (300, "5 min"),
    (600, "10 min"),
    (900, "15 min"),
    (1500, "25 min"),
    (1800, "30 min"),
    (3600, "1H"),
    (7200, "2H"),
]

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("rubika-forwarder")

# ─────────────────────────────────────────────
#  Markdown helpers
# ─────────────────────────────────────────────
def md_bold(text: str) -> str:
    return f"**{text}**"


def md_italic(text: str) -> str:
    return f"__{text}__"


def md_mono(text: str) -> str:
    return f"`{text}`"


def md_code_block(text: str) -> str:
    return f"```\n{text}\n```"


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
    "http://",
    "https://",
)


def is_vpn_line(line: str) -> bool:
    stripped = line.strip().lower()
    return any(stripped.startswith(prefix) for prefix in VPN_PREFIXES)


def format_message_body(text: str) -> str:
    """Keep configs in code blocks so they are easier to copy.

    This is intentionally conservative. Only lines that look like config links
    or proxy strings go inside monospace blocks.
    """
    if not text:
        return ""

    lines = text.splitlines()
    out: list[str] = []
    buffer: list[str] = []

    def flush_buffer() -> None:
        if buffer:
            out.append(md_code_block("\n".join(buffer).strip()))
            buffer.clear()

    for line in lines:
        stripped = line.rstrip()
        if is_vpn_line(stripped):
            buffer.append(stripped)
        else:
            flush_buffer()
            out.append(stripped)

    flush_buffer()
    return "\n".join(out).strip()


def build_message_header(channel_name: str, msg_date: datetime) -> str:
    date_str = msg_date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        f"📡 {md_bold(channel_name)}\n"
        f"🕐 {md_italic(date_str)}\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def build_full_text(channel_name: str, msg_date: datetime, body: str) -> str:
    header = build_message_header(channel_name, msg_date)
    body = format_message_body(body)
    return f"{header}\n\n{body}" if body else header


def build_welcome_message() -> str:
    return (
        f"{md_bold('شما پذیرفته شدید')}\n\n"
        "از این لحظه، پیام‌های جدید برای شما ارسال می‌شوند.\n"
        "برای کپی راحت‌تر، لینک‌های کانفیگ داخل بلوک monospace می‌آیند."
    )


def build_skip_message(file_type: str, size_mb: float) -> str:
    return (
        f"⚠️ {md_bold('فایل حجیم رد شد')}\n\n"
        f"نوع: {md_mono(file_type)}\n"
        f"حجم: {md_mono(f'{size_mb:.1f} MB')}"
    )


def get_top_reactions(message) -> str:
    if not getattr(message, "reactions", None) or not message.reactions.results:
        return ""

    counts: list[tuple[str, int]] = []
    for item in message.reactions.results:
        emoji = getattr(item.reaction, "emoticon", None) or str(item.reaction)
        counts.append((emoji, item.count))

    counts.sort(key=lambda x: x[1], reverse=True)
    top = counts[:3]
    return "  ".join(f"{emoji} {md_bold(str(count))}" for emoji, count in top)


# ─────────────────────────────────────────────
#  Persistent storage
# ─────────────────────────────────────────────
def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        logger.error(f"Channels file {CHANNELS_FILE} not found")
        sys.exit(1)

    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("channels.json must contain a JSON list")
    return [str(item) for item in data]


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_subscribers() -> set[str]:
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                return {str(x) for x in data}
    return set()


def save_subscribers(subscribers: set[str]) -> None:
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(subscribers), f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
#  Rubika REST API wrapper
# ─────────────────────────────────────────────
def _rubika_post(endpoint: str, payload: dict | None = None, timeout: int = 20) -> dict | None:
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{endpoint}"
    try:
        resp = requests.post(url, json=payload or {}, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        logger.error(f"Rubika {endpoint} HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        logger.error(f"Rubika {endpoint} error: {exc}")
    return None


def _extract_field(data: dict, *paths: str) -> str | None:
    for path in paths:
        cur: object = data
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            return str(cur)
    return None


def _rubika_file_type(telegram_media) -> str:
    if getattr(telegram_media, "photo", None):
        return "Image"
    if getattr(telegram_media, "video", None):
        return "Video"
    if getattr(telegram_media, "voice", None):
        return "Voice"
    if getattr(telegram_media, "audio", None):
        return "Music"
    if getattr(telegram_media, "document", None):
        mime = getattr(telegram_media.document, "mime_type", "")
        if mime == "video/mp4" and getattr(telegram_media, "gif", False):
            return "Gif"
        return "File"
    return "File"


def _send_payload(endpoint: str, base: dict) -> tuple[bool, str | None]:
    data = _rubika_post(endpoint, base)
    if not data:
        return False, None

    if data.get("status") == "OK" or data.get("ok"):
        msg_id = _extract_field(data, "data.message_id", "message_id", "result.message_id")
        return True, msg_id

    logger.error(f"{endpoint} failed: {data}")
    return False, None


def send_text_to_rubika(chat_id: str, text: str) -> tuple[bool, str | None]:
    return _send_payload(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        },
    )


def send_file_to_rubika(chat_id: str, file_id: str, caption: str) -> tuple[bool, str | None]:
    return _send_payload(
        "sendFile",
        {
            "chat_id": chat_id,
            "file_id": file_id,
            "text": caption,
            "parse_mode": "Markdown",
        },
    )


def edit_text_in_rubika(chat_id: str, message_id: str, new_text: str) -> bool:
    data = _rubika_post(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": new_text,
            "parse_mode": "Markdown",
        },
    )
    return bool(data and (data.get("status") == "OK" or data.get("ok")))


# ─────────────────────────────────────────────
#  File upload
# ─────────────────────────────────────────────
def upload_to_rubika(file_bytes: bytes, filename: str, file_type: str) -> str | None:
    req = _rubika_post("requestSendFile", {"type": file_type})
    if not req:
        return None

    upload_url = _extract_field(req, "data.upload_url", "upload_url", "result.upload_url")
    if not upload_url:
        logger.error(f"requestSendFile missing upload_url: {req}")
        return None

    try:
        resp = requests.post(upload_url, files={"file": (filename, file_bytes)}, timeout=90)
        if resp.status_code != 200:
            logger.error(f"Storage upload failed {resp.status_code}: {resp.text[:200]}")
            return None

        data = resp.json()
        file_id = _extract_field(data, "data.file_id", "file_id", "result.file_id")
        if file_id:
            logger.info(f"Uploaded {filename} -> {file_id}")
            return file_id

        logger.error(f"Upload response missing file_id: {data}")
    except Exception as exc:
        logger.error(f"Upload exception: {exc}")
    return None


# ─────────────────────────────────────────────
#  Subscriber management via getUpdates polling
# ─────────────────────────────────────────────
_welcomed: set[str] = set()


def _is_start_event(update: dict) -> bool:
    update_type = update.get("type")
    if update_type == "StartedBot":
        return True

    if update_type != "NewMessage":
        return False

    new_message = update.get("new_message") or {}
    text = str(new_message.get("text") or "").strip()
    aux_data = new_message.get("aux_data") or {}
    button_id = str(aux_data.get("button_id") or "").strip().lower()

    return text == "/start" or button_id == "start"


def _extract_chat_id(update: dict) -> str | None:
    return (
        update.get("chat_id")
        or (update.get("new_message") or {}).get("chat_id")
        or (update.get("updated_message") or {}).get("chat_id")
    )


def _process_updates(updates: Iterable[dict], subscribers: set[str]) -> set[str]:
    new_subscribers: set[str] = set()

    for update in updates:
        chat_id = _extract_chat_id(update)
        if not chat_id:
            continue

        if not _is_start_event(update):
            continue

        if chat_id in subscribers or chat_id in new_subscribers:
            continue

        new_subscribers.add(chat_id)
        logger.info(f"New subscriber: {chat_id}")

    return new_subscribers


def fetch_updates(subscribers: set[str], state: dict) -> tuple[set[str], dict]:
    payload = {"limit": 200}
    offset_id = state.get("rubika_updates_offset_id")
    if offset_id:
        payload["offset_id"] = offset_id

    data = _rubika_post("getUpdates", payload)
    if not data:
        return subscribers, state

    updates = data.get("updates") or (data.get("data") or {}).get("updates") or []
    next_offset = data.get("next_offset_id") or (data.get("data") or {}).get("next_offset_id")

    new_subscribers = _process_updates(updates, subscribers)
    if ADMIN_CHAT_ID:
        new_subscribers.discard(ADMIN_CHAT_ID)

    changed = False
    if new_subscribers:
        subscribers |= new_subscribers
        _welcomed.update(new_subscribers)
        save_subscribers(subscribers)
        changed = True

        for chat_id in sorted(new_subscribers):
            welcome_text = build_welcome_message()
            ok, _ = send_text_to_rubika(chat_id, welcome_text)
            if ok:
                _welcomed.add(chat_id)
            else:
                logger.warning(f"Welcome message failed: {chat_id}")

    if next_offset and next_offset != state.get("rubika_updates_offset_id"):
        state["rubika_updates_offset_id"] = next_offset
        changed = True

    if changed:
        save_state(state)

    return subscribers, state


# ─────────────────────────────────────────────
#  Broadcast helpers
# ─────────────────────────────────────────────
def broadcast_text(subscribers: set[str], text: str) -> list[dict]:
    results = []
    for chat_id in list(subscribers):
        ok, msg_id = send_text_to_rubika(chat_id, text)
        if ok and msg_id:
            results.append({"chat_id": chat_id, "rubika_msg_id": msg_id, "full_text": text})
    return results


def broadcast_file(subscribers: set[str], file_id: str, caption: str) -> list[dict]:
    results = []
    for chat_id in list(subscribers):
        ok, msg_id = send_file_to_rubika(chat_id, file_id, caption)
        if ok and msg_id:
            results.append({"chat_id": chat_id, "rubika_msg_id": msg_id, "full_text": caption})
    return results


# ─────────────────────────────────────────────
#  Delayed reaction edits
# ─────────────────────────────────────────────
pending_edits: dict[tuple[str, int], list[dict]] = {}
reaction_tasks: set[asyncio.Task] = set()


async def delayed_reaction_updates(client: TelegramClient, channel_name: str, tg_msg_id: int) -> None:
    key = (channel_name, tg_msg_id)
    start = asyncio.get_running_loop().time()

    for delay_seconds, label in REACTION_EDIT_SCHEDULE:
        sleep_for = delay_seconds - (asyncio.get_running_loop().time() - start)
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)

        entries = pending_edits.get(key)
        if not entries:
            return

        try:
            msg = await client.get_messages(channel_name, ids=tg_msg_id)
            if not msg:
                continue

            reactions = get_top_reactions(msg)
            if not reactions:
                continue

            for entry in entries:
                new_text = f"{entry['full_text']}\n\n💬 {reactions}"
                edit_text_in_rubika(entry["chat_id"], entry["rubika_msg_id"], new_text)

            logger.info(f"Reaction edit applied at {label} for {channel_name}:{tg_msg_id}")
        except Exception as exc:
            logger.error(f"Reaction edit error ({label}) for {channel_name}:{tg_msg_id}: {exc}")

    pending_edits.pop(key, None)


# ─────────────────────────────────────────────
#  Core forwarding
# ─────────────────────────────────────────────
async def forward_message(
    client: TelegramClient,
    message,
    channel_name: str,
    state: dict,
    subscribers: set[str],
    force: bool = False,
) -> None:
    if not force and message.id <= state.get(channel_name, 0):
        return

    if not subscribers:
        return

    msg_date = message.date.replace(tzinfo=timezone.utc) if message.date.tzinfo is None else message.date

    # Text only
    if message.text and not message.media:
        full_text = build_full_text(channel_name, msg_date, message.text)
        deliveries = broadcast_text(subscribers, full_text)
        if deliveries:
            state[channel_name] = message.id
            save_state(state)
            key = (channel_name, message.id)
            pending_edits[key] = deliveries
            task = asyncio.create_task(delayed_reaction_updates(client, channel_name, message.id))
            reaction_tasks.add(task)
            task.add_done_callback(reaction_tasks.discard)
        return

    # Media
    if not message.media:
        return

    if not message.file or not message.file.size:
        state[channel_name] = message.id
        save_state(state)
        return

    file_type = _rubika_file_type(message.media)
    max_bytes = MAX_FILE_SIZE_MB.get(file_type, 50) * 1024 * 1024

    if message.file.size > max_bytes:
        size_mb = message.file.size / (1024 * 1024)
        skip_msg = build_skip_message(file_type, size_mb)
        broadcast_text(subscribers, skip_msg)
        state[channel_name] = message.id
        save_state(state)
        return

    filename_map = {
        "Image": "photo.jpg",
        "Voice": "voice.ogg",
        "Music": message.file.name or "audio.mp3",
        "Video": message.file.name or "video.mp4",
        "Gif": message.file.name or "animation.mp4",
    }
    filename = filename_map.get(file_type, message.file.name or "file")

    try:
        file_bytes = await client.download_media(message, file=bytes)
    except Exception as exc:
        logger.error(f"Download failed for msg {message.id}: {exc}")
        return

    if not file_bytes:
        logger.error(f"Downloaded empty media for msg {message.id}")
        return

    file_id = upload_to_rubika(file_bytes, filename, file_type)
    if not file_id:
        return

    caption = build_full_text(channel_name, msg_date, message.text or "")
    deliveries = broadcast_file(subscribers, file_id, caption)
    if deliveries:
        state[channel_name] = message.id
        save_state(state)
        key = (channel_name, message.id)
        pending_edits[key] = deliveries
        task = asyncio.create_task(delayed_reaction_updates(client, channel_name, message.id))
        reaction_tasks.add(task)
        task.add_done_callback(reaction_tasks.discard)


# ─────────────────────────────────────────────
#  Git repo sync
# ─────────────────────────────────────────────
def git_clone_data_repo(token: str, repo_url: str) -> None:
    if DATA_REPO_DIR.exists():
        shutil.rmtree(DATA_REPO_DIR)

    helper = Path("/tmp/git-cred-helper.sh")
    helper.write_text(
        f"#!/bin/sh\necho 'username=x-access-token'\necho 'password={token}'\n",
        encoding="utf-8",
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
                repo_url,
                str(DATA_REPO_DIR),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Data repo cloned")
    except subprocess.CalledProcessError as exc:
        logger.error(f"Clone failed: {exc.stderr}")
        raise
    finally:
        helper.unlink(missing_ok=True)


def git_push_data_repo() -> None:
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=DATA_REPO_DIR, check=True)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], cwd=DATA_REPO_DIR, check=True)
        subprocess.run(["git", "add", "state.json", "subscribers.json"], cwd=DATA_REPO_DIR, check=True)

        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=DATA_REPO_DIR, capture_output=True)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "chore: update state & subscribers"], cwd=DATA_REPO_DIR, check=True)
            subprocess.run(["git", "push"], cwd=DATA_REPO_DIR, check=True)
            logger.info("Data pushed to private repo")
    except subprocess.CalledProcessError as exc:
        logger.error(f"Git push failed: {exc}")


# ─────────────────────────────────────────────
#  Startup catch-up
# ─────────────────────────────────────────────
async def catch_up(client: TelegramClient, channels: list[str], state: dict, subscribers: set[str]) -> None:
    missing_markers = [ch for ch in channels if ch not in state]
    if not state or missing_markers:
        for channel in channels:
            if channel in state:
                continue
            try:
                msgs = await client.get_messages(channel, limit=1)
                state[channel] = msgs[0].id if msgs else 0
            except Exception as exc:
                logger.error(f"Failed to init marker for {channel}: {exc}")
        save_state(state)
        return

    for channel in channels:
        try:
            msgs = await client.get_messages(channel, limit=10)
            for msg in reversed(msgs or []):
                if msg.id <= state.get(channel, 0):
                    continue
                if not msg.text and not msg.media:
                    continue
                await forward_message(client, msg, channel, state, subscribers)
        except Exception as exc:
            logger.error(f"Catch-up error for {channel}: {exc}")


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
async def main() -> None:
    if not all([API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN]):
        logger.error("Missing required environment variables")
        sys.exit(1)

    if not DATA_REPO_PAT or not DATA_REPO_URL:
        logger.error("DATA_REPO_PAT and DATA_REPO_URL must be set")
        sys.exit(1)

    git_clone_data_repo(DATA_REPO_PAT, DATA_REPO_URL)

    channels = load_channels()
    state = load_state()
    subscribers = load_subscribers()

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()

    # Seed subscriber state once, then poll every minute.
    subscribers, state = fetch_updates(subscribers, state)

    await catch_up(client, channels, state, subscribers)

    @client.on(events.NewMessage(chats=channels))
    async def on_new_tg_message(event):
        try:
            chat = await event.get_chat()
            channel_title = chat.title or chat.username or str(chat.id)
            await forward_message(client, event.message, channel_title, state, subscribers)
        except Exception as exc:
            logger.error(f"Forward handler error: {exc}")

    async def poll_rubika_subscribers() -> None:
        while True:
            await asyncio.sleep(SUBSCRIBER_REFRESH_INTERVAL)
            try:
                nonlocal subscribers, state
                subscribers, state = fetch_updates(subscribers, state)
            except Exception as exc:
                logger.error(f"Subscriber poll error: {exc}")

    asyncio.create_task(poll_rubika_subscribers())

    start = time.monotonic()
    while True:
        if time.monotonic() - start >= RUN_DURATION:
            break
        await asyncio.sleep(30)

    save_state(state)
    save_subscribers(subscribers)
    git_push_data_repo()
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
