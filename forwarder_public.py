import os
import json
import time
import asyncio
import logging
import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime, timezone

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────
API_ID          = int(os.environ["API_ID"])
API_HASH        = os.environ["API_HASH"]
STRING_SESSION  = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]
ADMIN_CHAT_ID   = os.environ.get("ADMIN_CHAT_ID", "")

DATA_REPO_URL   = os.environ.get("DATA_REPO_URL", "")
DATA_REPO_DIR   = Path("data_repo")
CHANNELS_FILE   = Path("channels.json")

RUN_DURATION    = 20400          # 5 h 40 min (fits GitHub's 6‑hour job limit)
SUBSCRIBER_REFRESH_INTERVAL = 60  # seconds – check for new /start users frequently

MAX_FILE_SIZE_MB = {
    "Image": 10, "Video": 50, "File": 50,
    "Music": 50, "Voice": 10, "Gif": 50,
}

# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  Rubika Markdown helpers
#  Rubika supports: **bold**, __italic__, `mono`, ~~strike~~
# ─────────────────────────────────────────────
def md_bold(text: str) -> str:
    return f"**{text}**"

def md_italic(text: str) -> str:
    return f"__{text}__"

def md_mono(text: str) -> str:
    return f"`{text}`"

def md_code_block(text: str) -> str:
    return f"```\n{text}\n```"

def is_vpn_config(text: str) -> bool:
    """Detect if a message contains VPN config strings."""
    vpn_prefixes = (
        "vmess://", "vless://", "trojan://", "ss://",
        "ssr://", "hysteria://", "hysteria2://", "tuic://",
        "wireguard://", "socks5://", "http://", "https://",
    )
    low = text.lower()
    return any(low.startswith(p) or ("\n" + p) in low for p in vpn_prefixes)

def format_vpn_text(text: str) -> str:
    """Wrap each VPN config line in monospace; leave plain lines alone."""
    lines = text.splitlines()
    result = []
    vpn_prefixes = (
        "vmess://", "vless://", "trojan://", "ss://",
        "ssr://", "hysteria://", "hysteria2://", "tuic://",
        "wireguard://",
    )
    for line in lines:
        stripped = line.strip()
        if any(stripped.lower().startswith(p) for p in vpn_prefixes):
            result.append(md_mono(stripped))
        else:
            result.append(line)
    return "\n".join(result)

def build_message_header(channel_name: str, msg_date: datetime) -> str:
    date_str = msg_date.strftime("%Y-%m-%d  %H:%M UTC")
    return (
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📡 {md_bold(channel_name)}\n"
        f"🕐 __{date_str}__\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

def build_full_text(channel_name: str, msg_date: datetime, body: str) -> str:
    header = build_message_header(channel_name, msg_date)
    if body:
        formatted_body = format_vpn_text(body)
        return f"{header}\n\n{formatted_body}"
    return header

def build_welcome_message(chat_id: str) -> str:
    return (
        f"🚀 {md_bold('VPN Config Bot خوش آمدید!')}\n\n"
        f"این ربات به‌صورت خودکار کانفیگ‌های VPN را از کانال‌های تلگرام دریافت کرده "
        f"و برای شما ارسال می‌کند.\n\n"
        f"📌 {md_bold('کانفیگ‌های پشتیبانی‌شده:')}\n"
        f"  • `vmess://`  • `vless://`  • `trojan://`\n"
        f"  • `ss://`  • `hysteria2://`  • `tuic://`\n\n"
        f"✅ اشتراک شما فعال شد! کانفیگ‌های جدید به‌محض انتشار ارسال می‌شوند.\n\n"
        f"__برای لغو اشتراک، ربات را مسدود (بلاک) کنید.__"
    )

def build_skip_message(file_type: str, size_mb: float) -> str:
    return (
        f"⚠️ {md_bold('فایل حجیم – رد شد')}\n\n"
        f"نوع: `{file_type}`\n"
        f"حجم: `{size_mb:.1f} MB`\n\n"
        f"__این فایل از حد مجاز بزرگ‌تر است.__"
    )

def get_top_reactions(message) -> str:
    if not message.reactions or not message.reactions.results:
        return ""
    counts = []
    for r in message.reactions.results:
        emoji = r.reaction.emoticon if hasattr(r.reaction, "emoticon") else str(r.reaction)
        counts.append((emoji, r.count))
    counts.sort(key=lambda x: x[1], reverse=True)
    top = counts[:3]
    return "  ".join(f"{emoji} {md_bold(str(count))}" for emoji, count in top)

# ─────────────────────────────────────────────
#  Persistent storage (data_repo)
# ─────────────────────────────────────────────
_state_file       = DATA_REPO_DIR / "state.json"
_subscribers_file = DATA_REPO_DIR / "subscribers.json"

def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        logger.error(f"Channels file {CHANNELS_FILE} not found!")
        sys.exit(1)
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state() -> dict:
    if _state_file.exists():
        with open(_state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    _state_file.parent.mkdir(parents=True, exist_ok=True)
    with open(_state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

def load_subscribers() -> set:
    if _subscribers_file.exists():
        with open(_subscribers_file, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_subscribers(subscribers: set):
    _subscribers_file.parent.mkdir(parents=True, exist_ok=True)
    with open(_subscribers_file, "w", encoding="utf-8") as f:
        json.dump(sorted(subscribers), f, indent=2)

# ─────────────────────────────────────────────
#  Rubika REST API wrapper
# ─────────────────────────────────────────────
def _rubika_post(endpoint: str, payload: dict, timeout: int = 20) -> dict | None:
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        if resp.status_code == 200:
            return resp.json()
        logger.error(f"Rubika {endpoint} HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as e:
        logger.error(f"Rubika {endpoint} error: {e}")
    return None

def _extract_field(data: dict, *paths: str) -> str | None:
    for path in paths:
        cur = data
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur is not None:
            return str(cur)
    return None

def _rubika_file_type(telegram_media) -> str:
    if hasattr(telegram_media, "photo") and telegram_media.photo:
        return "Image"
    if hasattr(telegram_media, "video") and telegram_media.video:
        return "Video"
    if hasattr(telegram_media, "voice") and telegram_media.voice:
        return "Voice"
    if hasattr(telegram_media, "audio") and telegram_media.audio:
        return "Music"
    if hasattr(telegram_media, "document") and telegram_media.document:
        mime = getattr(telegram_media.document, "mime_type", "")
        if mime == "video/mp4" and getattr(telegram_media, "gif", False):
            return "Gif"
        return "File"
    return "File"

# ── Sending ────────────────────────────────────
def _send_payload(endpoint: str, base: dict) -> tuple[bool, str | None]:
    data = _rubika_post(endpoint, base)
    if not data:
        return False, None
    if data.get("status") == "OK" or data.get("ok"):
        msg_id = _extract_field(data,
            "data.message_id", "message_id", "result.message_id")
        return True, msg_id
    logger.error(f"{endpoint} failed: {data}")
    return False, None

def send_text_to_rubika(chat_id: str, text: str) -> tuple[bool, str | None]:
    return _send_payload("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    })

def send_file_to_rubika(chat_id: str, file_id: str, caption: str) -> tuple[bool, str | None]:
    return _send_payload("sendFile", {
        "chat_id": chat_id,
        "file_id": file_id,
        "text": caption,
        "parse_mode": "Markdown",
    })

def edit_text_in_rubika(chat_id: str, message_id: str, new_text: str) -> bool:
    data = _rubika_post("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
        "parse_mode": "Markdown",
    })
    return data is not None and (data.get("status") == "OK" or data.get("ok"))

# ── File upload ────────────────────────────────
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
            logger.info(f"✅ Uploaded {filename} ({len(file_bytes)//1024} KB) → {file_id}")
            return file_id
        logger.error(f"Upload response missing file_id: {data}")
    except Exception as e:
        logger.error(f"Upload exception: {e}")
    return None

# ─────────────────────────────────────────────
#  Subscriber management via getUpdates polling
# ─────────────────────────────────────────────
# Track which chat_ids already received a welcome
_welcomed: set[str] = set()

def _process_updates(
    updates: list,
    subscribers: set,
    send_welcome: bool = True,
) -> tuple[set, set]:
    """Return (new_chat_ids, removed_chat_ids). Sends /start welcome if needed."""
    added, removed = set(), set()
    for update in updates:
        update_type = update.get("type")
        chat_id = (
            update.get("chat_id")
            or update.get("new_message", {}).get("chat_id")
            or update.get("updated_message", {}).get("chat_id")
        )
        if not chat_id:
            continue

        if update_type == "StartedBot":
            added.add(chat_id)
            # Send welcome message to new subscribers
            if send_welcome and chat_id not in _welcomed:
                welcome = build_welcome_message(chat_id)
                ok, _ = send_text_to_rubika(chat_id, welcome)
                if ok:
                    _welcomed.add(chat_id)
                    logger.info(f"🎉 Sent welcome to {chat_id}")
                else:
                    logger.warning(f"Could not send welcome to {chat_id}")

        elif update_type == "StoppedBot":
            removed.add(chat_id)
            logger.info(f"👋 {chat_id} stopped the bot")

        elif update_type == "NewMessage":
            # Capture any new chatter (backup method) – no welcome for this
            if chat_id not in subscribers and chat_id not in added:
                added.add(chat_id)

    return added, removed

def fetch_and_apply_updates(subscribers: set, state: dict, send_welcome: bool = True) -> tuple[set, dict]:
    offset_id = state.get("updates_offset_id")
    payload = {"limit": 200}
    if offset_id:
        payload["offset_id"] = offset_id

    data = _rubika_post("getUpdates", payload)
    if not data:
        return subscribers, state

    updates = (
        data.get("updates")
        or (data.get("data") or {}).get("updates")
        or []
    )
    next_offset = (
        data.get("next_offset_id")
        or (data.get("data") or {}).get("next_offset_id")
    )

    added, removed = _process_updates(updates, subscribers, send_welcome=send_welcome)

    if ADMIN_CHAT_ID:
        added.add(ADMIN_CHAT_ID)

    new_set = (subscribers | added) - removed

    if next_offset:
        state["updates_offset_id"] = next_offset

    if new_set != subscribers:
        save_subscribers(new_set)
        logger.info(
            f"👥 Subscribers: {len(new_set)}  (+{len(added)} −{len(removed)})"
        )

    return new_set, state

# ─────────────────────────────────────────────
#  Broadcast helpers
# ─────────────────────────────────────────────
def broadcast_text(subscribers: set, text: str) -> list[dict]:
    """Send text to all subscribers. Returns list of {chat_id, rubika_msg_id, full_text}."""
    results = []
    dead = set()
    for chat_id in list(subscribers):
        ok, msg_id = send_text_to_rubika(chat_id, text)
        if ok and msg_id:
            results.append({"chat_id": chat_id, "rubika_msg_id": msg_id, "full_text": text})
        else:
            logger.warning(f"Could not deliver to {chat_id}")
            # Don't remove immediately – might be a transient error
    return results

def broadcast_file(subscribers: set, file_id: str, caption: str) -> list[dict]:
    results = []
    for chat_id in list(subscribers):
        ok, msg_id = send_file_to_rubika(chat_id, file_id, caption)
        if ok and msg_id:
            results.append({"chat_id": chat_id, "rubika_msg_id": msg_id, "full_text": caption})
        else:
            logger.warning(f"Could not deliver file to {chat_id}")
    return results

# ─────────────────────────────────────────────
#  Delayed reaction edits
# ─────────────────────────────────────────────
pending_edits: dict[tuple, list[dict]] = {}

async def delayed_reaction_updates(client: TelegramClient, channel_name: str, tg_msg_id: int):
    key = (channel_name, tg_msg_id)
    for delay, label in [(300, "5 min"), (600, "15 min")]:
        await asyncio.sleep(delay)
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
                new_text = entry["full_text"] + f"\n\n💬 {reactions}"
                edit_text_in_rubika(entry["chat_id"], entry["rubika_msg_id"], new_text)
            logger.info(f"✅ Reaction edit ({label}) for msg {tg_msg_id}")
        except Exception as e:
            logger.error(f"Reaction edit error ({label}) for {tg_msg_id}: {e}")
    pending_edits.pop(key, None)

# ─────────────────────────────────────────────
#  Core forwarding
# ─────────────────────────────────────────────
async def forward_message(
    client: TelegramClient,
    message,
    channel_name: str,
    state: dict,
    subscribers: set,
    force: bool = False,
):
    if not force:
        last_id = state.get(channel_name, 0)
        if message.id <= last_id:
            return

    if not subscribers:
        logger.warning("No subscribers – skipping forward")
        return

    msg_date = message.date.replace(tzinfo=timezone.utc) if message.date.tzinfo is None else message.date

    # ── TEXT only ──────────────────────────────
    if message.text and not message.media:
        full_text = build_full_text(channel_name, msg_date, message.text)
        deliveries = broadcast_text(subscribers, full_text)
        if deliveries:
            state[channel_name] = message.id
            save_state(state)
            key = (channel_name, message.id)
            pending_edits[key] = deliveries
            asyncio.ensure_future(delayed_reaction_updates(client, channel_name, message.id))
        return

    # ── MEDIA ──────────────────────────────────
    if not message.media:
        return

    if not message.file or not message.file.size:
        logger.warning(f"Msg {message.id}: no file info, skipping")
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
        "Gif":   message.file.name or "animation.mp4",
    }
    filename = filename_map.get(file_type, message.file.name or "file")

    try:
        file_bytes = await client.download_media(message, file=bytes)
        logger.info(f"⬇️  Downloaded {file_type} ({len(file_bytes)//1024} KB) from {channel_name}")
    except Exception as e:
        logger.error(f"Download failed for msg {message.id}: {e}")
        return

    file_id = upload_to_rubika(file_bytes, filename, file_type)
    if not file_id:
        logger.error("Upload to Rubika failed – skipping media")
        return

    caption_body = message.text or ""
    caption = build_full_text(channel_name, msg_date, caption_body)

    deliveries = broadcast_file(subscribers, file_id, caption)
    if deliveries:
        state[channel_name] = message.id
        save_state(state)
        key = (channel_name, message.id)
        pending_edits[key] = deliveries
        asyncio.ensure_future(delayed_reaction_updates(client, channel_name, message.id))

# ─────────────────────────────────────────────
#  Git data-repo sync
# ─────────────────────────────────────────────
def git_clone_data_repo(token: str, repo_url: str):
    if DATA_REPO_DIR.exists():
        shutil.rmtree(DATA_REPO_DIR)

    helper = Path("/tmp/git-cred-helper.sh")
    helper.write_text(
        f"#!/bin/sh\necho 'username=x-access-token'\necho 'password={token}'\n"
    )
    os.chmod(helper, 0o755)

    try:
        subprocess.run(
            ["git", "-c", f"credential.helper={helper}",
             "clone", "--depth", "1", repo_url, str(DATA_REPO_DIR)],
            check=True, capture_output=True, text=True,
        )
        logger.info("📦 Data repo cloned.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Clone failed: {e.stderr}")
        raise
    finally:
        helper.unlink(missing_ok=True)

def git_push_data_repo():
    os.chdir(DATA_REPO_DIR)
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "user.name",  "GitHub Actions"],     check=True)
        subprocess.run(["git", "add", "state.json", "subscribers.json"],       check=True)
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "chore: update state & subscribers"], check=True)
            subprocess.run(["git", "push"], check=True)
            logger.info("🚀 Data pushed to private repo.")
        else:
            logger.info("No changes to push.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Git push failed: {e}")
    finally:
        os.chdir("..")

# ─────────────────────────────────────────────
#  Startup catch-up
# ─────────────────────────────────────────────
async def catch_up(client, channels, state, subscribers):
    first_run = not state or not any(state.get(ch) for ch in channels)

    if first_run:
        logger.info("🆕 First run – setting start markers without forwarding old messages")
        for channel in channels:
            try:
                msgs = await client.get_messages(channel, limit=1)
                state[channel] = msgs[0].id if msgs else 0
                logger.info(f"  Start marker for {channel} at msg {state[channel]}")
            except Exception as e:
                logger.error(f"  Failed to init {channel}: {e}")
        save_state(state)
        return

    logger.info("🔍 Checking for missed messages since last run…")
    for channel in channels:
        try:
            msgs = await client.get_messages(channel, limit=10)
            for msg in reversed(msgs or []):
                if msg.id <= state.get(channel, 0):
                    continue
                if not msg.text and not msg.media:
                    continue
                logger.info(f"  Forwarding missed msg {msg.id} from {channel}")
                await forward_message(client, msg, channel, state, subscribers)
        except Exception as e:
            logger.error(f"Catch-up error for {channel}: {e}")

# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
async def main():
    if not all([API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN]):
        logger.error("Missing required environment variables!")
        sys.exit(1)

    token   = os.environ.get("DATA_REPO_PAT", "")
    repo_url = os.environ.get("DATA_REPO_URL", "")
    if not token or not repo_url:
        logger.error("DATA_REPO_PAT and DATA_REPO_URL must be set!")
        sys.exit(1)

    git_clone_data_repo(token, repo_url)

    channels    = load_channels()
    state       = load_state()
    subscribers = load_subscribers()

    logger.info(f"📡 Monitoring {len(channels)} channels: {channels}")

    # ── Initial subscriber refresh (full history, no duplicate welcomes) ──
    _welcomed.update(subscribers)  # Existing subscribers already welcomed
    subscribers, state = fetch_and_apply_updates(subscribers, state, send_welcome=True)
    logger.info(f"👥 Subscribers at startup: {len(subscribers)}")

    if not subscribers:
        logger.warning("No subscribers yet. Users need to /start the bot first.")

    # ── Telegram client ────────────────────────
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("✅ Telegram client connected")

    await catch_up(client, channels, state, subscribers)

    # ── Live Telegram → Rubika forward handler ─
    @client.on(events.NewMessage(chats=channels))
    async def on_new_tg_message(event):
        try:
            chat = await event.get_chat()
            channel_title = chat.title or chat.username or str(chat.id)
            await forward_message(client, event.message, channel_title, state, subscribers)
        except Exception as e:
            logger.error(f"Forward handler error: {e}")

    # ── Background: poll Rubika for new /start users ──────────────────────
    async def poll_rubika_subscribers():
        while True:
            await asyncio.sleep(SUBSCRIBER_REFRESH_INTERVAL)
            try:
                nonlocal subscribers
                new_subs, updated_state = fetch_and_apply_updates(
                    subscribers, state, send_welcome=True
                )
                subscribers.clear()
                subscribers.update(new_subs)
                state.update(updated_state)
                # Periodic disk flush
                save_subscribers(subscribers)
                save_state(state)
            except Exception as e:
                logger.error(f"Subscriber poll error: {e}")

    asyncio.ensure_future(poll_rubika_subscribers())

    # ── Run until time limit ──────────────────────────────────────────────
    logger.info(f"🔄 Forwarding live – will run for {RUN_DURATION/3600:.1f} h")
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= RUN_DURATION:
            logger.info("⏰ Time limit reached – shutting down cleanly")
            break
        await asyncio.sleep(30)

    # ── Persist state and exit ─────────────────
    save_state(state)
    save_subscribers(subscribers)
    git_push_data_repo()
    await client.disconnect()
    logger.info("👋 Session closed. See you in 6 hours!")


if __name__ == "__main__":
    asyncio.run(main())
