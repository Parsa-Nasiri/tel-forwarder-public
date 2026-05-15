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
SUBSCRIBER_REFRESH_INTERVAL = 60  # every 1 minute

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
# PROXY / CONFIG TEXT FORMATTING
#
# Strategy: We use Rubika's Metadata API to apply
#   - Quote  → wraps each config block visually
#   - Pre    → monospace code block for configs
#
# Plain text is sent as-is. Config lines (VPN prefixes)
# are grouped into blocks and annotated via metadata.
# ─────────────────────────────────────────────
def is_proxy_line(line: str) -> bool:
    stripped = line.strip().lower()
    return any(stripped.startswith(p) for p in VPN_PREFIXES)


def build_proxy_payload(text: str):
    """
    Parse the message text and return:
      (final_text, metadata_parts)

    Config lines are grouped into contiguous blocks.
    Each block gets:
      - MetadataTypeEnum: Pre  (mono / code block)
      - MetadataTypeEnum: Quote (quoted block)

    Non-config lines are kept as plain text.

    Returns a tuple:
      - final_text: str   — the assembled plain text
      - meta_parts: list  — list of MetadataPart dicts for the Rubika API
    """
    if not text:
        return "", []

    lines = text.splitlines()

    # ── pass 1: classify each line ──
    classified = []  # list of (is_proxy: bool, line: str)
    for line in lines:
        classified.append((is_proxy_line(line), line.strip() if is_proxy_line(line) else line))

    # ── pass 2: group consecutive proxy lines into blocks ──
    # Result is a flat sequence of segments: ("text"|"proxy", content)
    segments = []
    i = 0
    while i < len(classified):
        is_proxy, content = classified[i]
        if not is_proxy:
            segments.append(("text", content))
            i += 1
        else:
            # Collect consecutive proxy lines
            block_lines = []
            while i < len(classified) and classified[i][0]:
                block_lines.append(classified[i][1])
                i += 1
            segments.append(("proxy", "\n".join(block_lines)))

    # ── pass 3: build final_text and metadata ──
    # Rubika Metadata uses UTF-16 code unit offsets.
    # Python's str uses UTF-16 internally for BMP chars.
    # Emoji and some chars are 2 UTF-16 units; we must account for that.

    parts = []
    meta_parts = []

    def utf16_len(s: str) -> int:
        """Length of string in UTF-16 code units (surrogate pairs = 2)."""
        return sum(2 if ord(c) > 0xFFFF else 1 for c in s)

    cursor = 0  # UTF-16 offset into the assembled text

    for idx, (seg_type, content) in enumerate(segments):
        if idx > 0:
            # Add newline separator between segments
            separator = "\n"
            parts.append(separator)
            cursor += utf16_len(separator)

        if seg_type == "text":
            parts.append(content)
            cursor += utf16_len(content)
        else:
            # proxy block: apply Pre (mono) + Quote
            seg_len = utf16_len(content)
            # Pre = monospace code block
            meta_parts.append({
                "type": "Pre",
                "from_index": cursor,
                "length": seg_len,
            })
            # Quote = quoted visual block
            meta_parts.append({
                "type": "Quote",
                "from_index": cursor,
                "length": seg_len,
            })
            parts.append(content)
            cursor += seg_len

    final_text = "".join(parts)
    return final_text, meta_parts


# ─────────────────────────────────────────────
# UX
# ─────────────────────────────────────────────
def build_header(channel_name: str, msg_date) -> str:
    date_str = msg_date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "━━━━━━━━━━━━━━━━━━\n"
        f"📡 {channel_name}\n"
        f"🕐 {date_str}\n"
        "━━━━━━━━━━━━━━━━━━"
    )


def build_message_parts(channel_name: str, msg_date, body: str):
    """
    Returns (final_text, meta_parts) ready for the Rubika API.
    The header is plain text; body config lines get Pre+Quote metadata.
    """
    header = build_header(channel_name, msg_date)

    if not body:
        return header, []

    # Build proxy-aware body
    body_text, body_meta = build_proxy_payload(body)

    # Header offset in UTF-16
    def utf16_len(s: str) -> int:
        return sum(2 if ord(c) > 0xFFFF else 1 for c in s)

    header_and_sep = header + "\n\n"
    header_offset = utf16_len(header_and_sep)

    # Shift body metadata offsets by header length
    shifted_meta = []
    for part in body_meta:
        shifted_meta.append({
            **part,
            "from_index": part["from_index"] + header_offset,
        })

    final_text = header_and_sep + body_text
    return final_text, shifted_meta


def build_welcome() -> str:
    return (
        "شما پذیرفته شدید\n\n"
        "کانفیگ‌های جدید به‌صورت خودکار ارسال می‌شوند.\n\n"
        "لینک‌های پروکسی داخل بخش قابل‌کپی قرار می‌گیرند."
    )


def build_skip_message(file_type: str, size_mb: float) -> str:
    return (
        f"⚠️ فایل بزرگ رد شد\n\n"
        f"نوع: {file_type}\n"
        f"حجم: {size_mb:.1f} MB"
    )


def get_top_reactions(message) -> str:
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
    return "  ".join(f"{emoji} {count}" for emoji, count in top)


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


def load_subscribers() -> set:
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_subscribers(subscribers: set):
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(subscribers), f, ensure_ascii=False, indent=2)


# ─────────────────────────────────────────────
# RUBIKA API
# ─────────────────────────────────────────────
def rubika_post(method: str, payload=None, timeout=20):
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{method}"
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
        if response.status_code != 200:
            logger.error(f"{method} HTTP {response.status_code}: {response.text[:300]}")
            return None
        return response.json()
    except Exception as e:
        logger.error(f"{method} error: {e}")
        return None


def send_text(chat_id: str, text: str, meta_parts: list = None):
    """
    Send a text message. If meta_parts is provided, use Metadata API
    for rich formatting (Quote, Pre, Bold, etc).
    """
    payload = {
        "chat_id": chat_id,
        "text": text,
    }

    if meta_parts:
        payload["metadata"] = {"meta_data_parts": meta_parts}

    data = rubika_post("sendMessage", payload)

    if not data:
        return False, None

    if data.get("status") == "OK":
        msg_id = data.get("data", {}).get("message_id")
        return True, msg_id

    logger.warning(f"sendMessage non-OK for {chat_id}: {data}")
    return False, None


def edit_text(chat_id: str, message_id: str, text: str, meta_parts: list = None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
    }
    if meta_parts:
        payload["metadata"] = {"meta_data_parts": meta_parts}

    data = rubika_post("editMessageText", payload)
    return bool(data and data.get("status") == "OK")


def send_file(chat_id: str, file_id: str, caption: str, meta_parts: list = None):
    payload = {
        "chat_id": chat_id,
        "file_id": file_id,
        "text": caption,
    }
    if meta_parts:
        payload["metadata"] = {"meta_data_parts": meta_parts}

    data = rubika_post("sendFile", payload)

    if not data:
        return False, None

    if data.get("status") == "OK":
        msg_id = data.get("data", {}).get("message_id")
        return True, msg_id

    return False, None


def upload_file(file_bytes: bytes, filename: str, file_type: str):
    req = rubika_post("requestSendFile", {"type": file_type})
    if not req:
        return None

    upload_url = req.get("data", {}).get("upload_url")
    if not upload_url:
        logger.error("upload_url missing")
        return None

    try:
        response = requests.post(
            upload_url,
            files={"file": (filename, file_bytes)},
            timeout=120,
        )
        if response.status_code != 200:
            logger.error(f"Upload HTTP {response.status_code}")
            return None

        data = response.json()
        file_id = data.get("data", {}).get("file_id")
        if file_id:
            return file_id
    except Exception as e:
        logger.error(f"Upload error: {e}")

    return None


# ─────────────────────────────────────────────
# GET UPDATES & SUBSCRIBER MANAGEMENT
#
# Every 60 seconds:
#   1. Call getUpdates with current offset_id
#   2. Extract all chat_ids from updates
#   3. Diff against known subscribers
#   4. New ones → send welcome → add to set → save → push
# ─────────────────────────────────────────────
def extract_chat_ids_from_updates(updates: list) -> set:
    """
    Extract every unique chat_id from any update type.
    We cast a wide net: StartedBot, NewMessage, /start commands.
    """
    found = set()
    for update in updates:
        chat_id = update.get("chat_id")
        if not chat_id:
            continue
        update_type = update.get("type", "")

        if update_type == "StartedBot":
            logger.info(f"[SUBSCRIBER] StartedBot event from chat_id={chat_id}")
            found.add(chat_id)
            continue

        if update_type == "NewMessage":
            msg = update.get("new_message", {})
            text = str(msg.get("text", "")).strip().lower()
            aux_data = msg.get("aux_data", {})
            button_id = str(aux_data.get("button_id", "")).lower()

            if text == "/start" or button_id == "start":
                logger.info(f"[SUBSCRIBER] /start from chat_id={chat_id}")
                found.add(chat_id)

    return found


def _welcome_and_register(new_chat_ids: set, subscribers: set):
    """
    For each genuinely new chat_id:
      - Send welcome message
      - Log result
      - Add to subscribers set
    """
    if not new_chat_ids:
        return

    logger.info(f"[SUBSCRIBER] {len(new_chat_ids)} new subscriber(s) detected: {sorted(new_chat_ids)}")

    for chat_id in sorted(new_chat_ids):
        ok, msg_id = send_text(chat_id, build_welcome())
        if ok:
            logger.info(f"[SUBSCRIBER] ✅ Welcome sent to {chat_id} (msg_id={msg_id})")
            subscribers.add(chat_id)
        else:
            logger.warning(f"[SUBSCRIBER] ❌ Failed to send welcome to {chat_id} — skipping add")

    save_subscribers(subscribers)
    logger.info(f"[SUBSCRIBER] Saved. Total subscribers: {len(subscribers)}")
    push_repo()


def fetch_updates(subscribers: set, state: dict):
    """
    Poll Rubika getUpdates, find new chat_ids not in subscribers,
    welcome them, persist, and push.
    """
    logger.info("[POLL] Fetching Rubika updates…")

    payload = {"limit": 200}
    offset_id = state.get("rubika_offset")
    if offset_id:
        payload["offset_id"] = offset_id

    data = rubika_post("getUpdates", payload, timeout=40)
    if not data:
        logger.warning("[POLL] getUpdates returned no data")
        return subscribers, state

    updates = data.get("data", {}).get("updates", [])
    next_offset = data.get("data", {}).get("next_offset_id")

    logger.info(f"[POLL] Received {len(updates)} update(s). next_offset={next_offset}")

    # Always ensure admin is a subscriber
    if ADMIN_CHAT_ID:
        if ADMIN_CHAT_ID not in subscribers:
            logger.info(f"[SUBSCRIBER] Adding ADMIN_CHAT_ID={ADMIN_CHAT_ID}")
            subscribers.add(ADMIN_CHAT_ID)

    # Diff: find chat_ids we've never seen before
    found_ids = extract_chat_ids_from_updates(updates)
    new_ids = found_ids - subscribers
    logger.info(f"[POLL] Found {len(found_ids)} chat_id(s) in updates, {len(new_ids)} are new")

    _welcome_and_register(new_ids, subscribers)

    if next_offset:
        state["rubika_offset"] = next_offset
        save_state(state)
        logger.debug(f"[POLL] Offset advanced to {next_offset}")

    return subscribers, state


# ─────────────────────────────────────────────
# MEDIA
# ─────────────────────────────────────────────
def get_file_type(media) -> str:
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
def broadcast_text(subscribers: set, text: str, meta_parts: list = None):
    results = []
    for chat_id in list(subscribers):
        ok, msg_id = send_text(chat_id, text, meta_parts)
        if ok and msg_id:
            results.append({
                "chat_id": chat_id,
                "msg_id": msg_id,
                "full_text": text,
                "meta_parts": meta_parts or [],
            })
    return results


def broadcast_file(subscribers: set, file_id: str, caption: str, meta_parts: list = None):
    results = []
    for chat_id in list(subscribers):
        ok, msg_id = send_file(chat_id, file_id, caption, meta_parts)
        if ok and msg_id:
            results.append({
                "chat_id": chat_id,
                "msg_id": msg_id,
                "full_text": caption,
                "meta_parts": meta_parts or [],
            })
    return results


# ─────────────────────────────────────────────
# REACTIONS
# ─────────────────────────────────────────────
pending_edits = {}


async def delayed_reaction_updates(client, channel_name: str, tg_msg_id: int):
    key = (channel_name, tg_msg_id)
    start = time.monotonic()

    for seconds, label in REACTION_EDIT_SCHEDULE:
        wait = seconds - (time.monotonic() - start)
        if wait > 0:
            await asyncio.sleep(wait)

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
                edit_text(
                    entry["chat_id"],
                    entry["msg_id"],
                    new_text,
                    entry.get("meta_parts") or None,
                )

            logger.info(f"[REACTION] Edit {label}: {channel_name} msg#{tg_msg_id}")

        except Exception as e:
            logger.error(f"[REACTION] Edit error: {e}")

    pending_edits.pop(key, None)


# ─────────────────────────────────────────────
# FORWARD
# ─────────────────────────────────────────────
async def forward_message(client, message, channel_name: str, state: dict, subscribers: set):
    if message.id <= state.get(channel_name, 0):
        return

    if not subscribers:
        logger.warning(f"[FORWARD] No subscribers — skipping {channel_name} msg#{message.id}")
        return

    msg_date = (
        message.date.replace(tzinfo=timezone.utc)
        if message.date.tzinfo is None
        else message.date
    )

    # ── TEXT ONLY ──
    if message.text and not message.media:
        text, meta_parts = build_message_parts(channel_name, msg_date, message.text)
        deliveries = broadcast_text(subscribers, text, meta_parts if meta_parts else None)

        if deliveries:
            state[channel_name] = message.id
            save_state(state)
            key = (channel_name, message.id)
            pending_edits[key] = deliveries
            asyncio.create_task(delayed_reaction_updates(client, channel_name, message.id))
            logger.info(f"[FORWARD] Text msg#{message.id} from {channel_name} → {len(deliveries)} subscriber(s)")

        return

    # ── MEDIA ──
    if not message.media or not message.file:
        return

    file_type = get_file_type(message.media)
    max_bytes = MAX_FILE_SIZE_MB.get(file_type, 50) * 1024 * 1024

    if message.file.size > max_bytes:
        size_mb = message.file.size / 1024 / 1024
        logger.warning(f"[FORWARD] Skipping large {file_type} ({size_mb:.1f} MB) from {channel_name}")
        broadcast_text(subscribers, build_skip_message(file_type, size_mb))
        return

    try:
        file_bytes = await client.download_media(message, file=bytes)
    except Exception as e:
        logger.error(f"[FORWARD] Download failed for {channel_name} msg#{message.id}: {e}")
        return

    filename = message.file.name or "file.bin"
    file_id = upload_file(file_bytes, filename, file_type)
    if not file_id:
        return

    caption, meta_parts = build_message_parts(channel_name, msg_date, message.text or "")
    deliveries = broadcast_file(subscribers, file_id, caption, meta_parts if meta_parts else None)

    if deliveries:
        state[channel_name] = message.id
        save_state(state)
        key = (channel_name, message.id)
        pending_edits[key] = deliveries
        asyncio.create_task(delayed_reaction_updates(client, channel_name, message.id))
        logger.info(f"[FORWARD] Media msg#{message.id} ({file_type}) from {channel_name} → {len(deliveries)} subscriber(s)")


# ─────────────────────────────────────────────
# GIT
# ─────────────────────────────────────────────
def clone_repo():
    if DATA_REPO_DIR.exists():
        shutil.rmtree(DATA_REPO_DIR)

    helper = Path("/tmp/git-helper.sh")
    helper.write_text(
        "#!/bin/sh\n"
        "echo 'username=x-access-token'\n"
        f"echo 'password={DATA_REPO_PAT}'\n"
    )
    os.chmod(helper, 0o755)

    try:
        subprocess.run(
            ["git", "-c", f"credential.helper={helper}", "clone", "--depth", "1", DATA_REPO_URL, str(DATA_REPO_DIR)],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("[GIT] Data repo cloned")
    finally:
        helper.unlink(missing_ok=True)


def push_repo():
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], cwd=DATA_REPO_DIR, check=True)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], cwd=DATA_REPO_DIR, check=True)
        subprocess.run(["git", "add", "."], cwd=DATA_REPO_DIR, check=True)

        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=DATA_REPO_DIR)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "update subscribers/state"], cwd=DATA_REPO_DIR, check=True)
            subprocess.run(["git", "push"], cwd=DATA_REPO_DIR, check=True)
            logger.info("[GIT] Data pushed to remote repo")
        else:
            logger.debug("[GIT] Nothing to push (no staged changes)")

    except Exception as e:
        logger.error(f"[GIT] Push error: {e}")


# ─────────────────────────────────────────────
# CATCHUP
# ─────────────────────────────────────────────
async def catchup(client, channels: list, state: dict, subscribers: set):
    if not state:
        logger.info("[CATCHUP] First run — setting message ID markers for all channels")
        for channel in channels:
            try:
                entity = await asyncio.wait_for(client.get_entity(channel), timeout=5.0)
                msgs = await asyncio.wait_for(client.get_messages(entity, limit=1), timeout=5.0)
                state[channel] = msgs[0].id if msgs else 0
                logger.info(f"[CATCHUP] Marker for {channel}: {state[channel]}")
            except asyncio.TimeoutError:
                logger.error(f"[CATCHUP] Timeout for {channel} — marker set to 0")
                state[channel] = 0
            except Exception as e:
                logger.error(f"[CATCHUP] Error for {channel}: {e} — marker set to 0")
                state[channel] = 0
        save_state(state)
        return

    logger.info("[CATCHUP] Catching up missed messages…")
    for channel in channels:
        try:
            entity = await asyncio.wait_for(client.get_entity(channel), timeout=5.0)
            msgs = await asyncio.wait_for(client.get_messages(entity, limit=10), timeout=5.0)
            forwarded = 0
            for msg in reversed(msgs):
                if msg.id <= state.get(channel, 0):
                    continue
                await forward_message(client, msg, channel, state, subscribers)
                forwarded += 1
            if forwarded:
                logger.info(f"[CATCHUP] Forwarded {forwarded} message(s) from {channel}")
        except asyncio.TimeoutError:
            logger.error(f"[CATCHUP] Timeout catching up on {channel} — skipping")
        except Exception as e:
            logger.error(f"[CATCHUP] Error on {channel}: {e}")


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    clone_repo()

    channels = load_channels()
    state = load_state()
    subscribers = load_subscribers()

    logger.info(f"[MAIN] Loaded {len(subscribers)} subscriber(s), {len(channels)} channel(s)")

    # First poll immediately on startup
    subscribers, state = fetch_updates(subscribers, state)

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("[MAIN] Telegram connected")

    await catchup(client, channels, state, subscribers)

    @client.on(events.NewMessage(chats=channels))
    async def new_message(event):
        try:
            chat = await event.get_chat()
            channel_name = chat.title or chat.username or str(chat.id)
            await forward_message(client, event.message, channel_name, state, subscribers)
        except Exception as e:
            logger.error(f"[HANDLER] Error: {e}")

    async def poll_subscribers():
        nonlocal subscribers, state
        while True:
            await asyncio.sleep(SUBSCRIBER_REFRESH_INTERVAL)
            try:
                logger.info("[POLL] Running scheduled subscriber poll…")
                subscribers, state = fetch_updates(subscribers, state)
            except Exception as e:
                logger.error(f"[POLL] Error during scheduled poll: {e}")

    asyncio.create_task(poll_subscribers())

    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= RUN_DURATION:
            logger.info(f"[MAIN] Run duration ({RUN_DURATION}s) reached — shutting down")
            break
        await asyncio.sleep(30)

    save_state(state)
    save_subscribers(subscribers)
    push_repo()

    await client.disconnect()
    logger.info("[MAIN] Finished cleanly")


if __name__ == "__main__":
    asyncio.run(main())
