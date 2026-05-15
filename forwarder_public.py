"""
╔══════════════════════════════════════════════════════════════╗
║        Telegram → Rubika VPN Forwarder Bot                  ║
║        Single-file • Production-grade • Feature-complete    ║
╚══════════════════════════════════════════════════════════════╝
"""

import os
import asyncio
import json
import logging
import time
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import aiofiles
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaPhoto, MessageMediaDocument,
    DocumentAttributeVideo, DocumentAttributeAudio,
    DocumentAttributeFilename,
)

# ──────────────────────────────────────────────────────────────
# ENV CONFIG
# ──────────────────────────────────────────────────────────────
API_ID         = int(os.environ["API_ID"])
API_HASH       = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]
DATA_REPO_PAT  = os.environ["DATA_REPO_PAT"]
DATA_REPO_URL  = os.environ["DATA_REPO_URL"]
ADMIN_CHAT_ID  = os.environ.get("ADMIN_CHAT_ID", "").strip()

RUN_DURATION   = 20_400          # ~5 h 40 min
POLL_INTERVAL  = 60              # subscriber poll interval (s)
CHANNELS_FILE  = "channels.json"
DATA_REPO_DIR  = Path("data_repo")
SUBS_FILE      = DATA_REPO_DIR / "subscribers.json"
STATE_FILE     = DATA_REPO_DIR / "state.json"

RUBIKA_BASE    = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}"

# ──────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("forwarder")

# ──────────────────────────────────────────────────────────────
# FILE SIZE LIMITS (bytes)
# ──────────────────────────────────────────────────────────────
SIZE_LIMITS = {
    "Image": 10 * 1024 * 1024,
    "Video": 50 * 1024 * 1024,
    "Voice": 20 * 1024 * 1024,
    "Music": 50 * 1024 * 1024,
    "File":  50 * 1024 * 1024,
    "Gif":   50 * 1024 * 1024,
}

# ──────────────────────────────────────────────────────────────
# VPN / PROXY URI PATTERNS (lines wrapped in mono+quote)
# ──────────────────────────────────────────────────────────────
VPN_SCHEMES = re.compile(
    r"^(vmess://|vless://|ss://|trojan://|tuic://|hysteria2?://|hy2://|"
    r"wireguard://|warp://|socks5?://|http://\d|https://\d|"
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})",
    re.IGNORECASE,
)

# ──────────────────────────────────────────────────────────────
# GLOBAL STATE
# ──────────────────────────────────────────────────────────────
subscribers:   set  = set()
pending_edits: dict = {}      # rubika_msg_id → {chat_id, msg_id, text, schedule}
update_offset: str  = None    # Rubika getUpdates offset


# ══════════════════════════════════════════════════════════════
# GIT / DATA REPO
# ══════════════════════════════════════════════════════════════

def _authenticated_url(url: str) -> str:
    """
    Inject PAT into a GitHub HTTPS URL so git never needs to prompt.
    https://github.com/owner/repo  →  https://x-token:PAT@github.com/owner/repo
    Works in GitHub Actions, Docker, any environment.
    """
    pat = DATA_REPO_PAT
    # Handle both https:// and git+https:// forms
    if "://" in url:
        scheme, rest = url.split("://", 1)
        # Strip any existing credentials (safety)
        if "@" in rest:
            rest = rest.split("@", 1)[1]
        return f"{scheme}://x-token:{pat}@{rest}"
    return url


def git_clone():
    auth_url = _authenticated_url(DATA_REPO_URL)
    log.info("Cloning data repo …")
    result = subprocess.run(
        ["git", "clone", auth_url, str(DATA_REPO_DIR)],
        capture_output=True,
    )
    if result.returncode != 0:
        # Redact PAT from error output before logging
        err = result.stderr.decode(errors="replace").replace(DATA_REPO_PAT, "***")
        raise RuntimeError(f"git clone failed (exit {result.returncode}): {err}")
    log.info("Data repo cloned ✓")


def git_push(message: str = "update"):
    auth_url = _authenticated_url(DATA_REPO_URL)
    try:
        # Embed PAT into remote URL so push authenticates without prompting
        subprocess.run(
            ["git", "-C", str(DATA_REPO_DIR), "remote", "set-url", "origin", auth_url],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(DATA_REPO_DIR), "config", "user.email", "bot@rubika"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(DATA_REPO_DIR), "config", "user.name", "RubikaBot"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(DATA_REPO_DIR), "add", "-A"],
            check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "-C", str(DATA_REPO_DIR), "commit", "-m", message],
            capture_output=True,
        )
        if result.returncode not in (0, 1):
            log.warning("git commit returned %s", result.returncode)
            return
        push_result = subprocess.run(
            ["git", "-C", str(DATA_REPO_DIR), "push"],
            capture_output=True,
        )
        if push_result.returncode != 0:
            err = push_result.stderr.decode(errors="replace").replace(DATA_REPO_PAT, "***")
            log.error("git push failed: %s", err)
        else:
            log.info("git push ✓ — %s", message)
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode(errors="replace").replace(DATA_REPO_PAT, "***")
        log.error("git error: %s", err)


# ══════════════════════════════════════════════════════════════
# SUBSCRIBER PERSISTENCE
# ══════════════════════════════════════════════════════════════

def load_subscribers():
    global subscribers
    if SUBS_FILE.exists():
        try:
            data = json.loads(SUBS_FILE.read_text())
            subscribers = set(str(x) for x in data)
            log.info("Loaded %d subscribers", len(subscribers))
        except Exception as e:
            log.warning("Could not load subscribers: %s", e)
            subscribers = set()
    else:
        subscribers = set()


def save_subscribers():
    SUBS_FILE.write_text(json.dumps(sorted(subscribers), ensure_ascii=False, indent=2))


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(data: dict):
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


# ══════════════════════════════════════════════════════════════
# RUBIKA API
# ══════════════════════════════════════════════════════════════

async def rubika_post(method: str, payload: dict, session: aiohttp.ClientSession,
                      retries: int = 3) -> Optional[dict]:
    url = f"{RUBIKA_BASE}/{method}"
    for attempt in range(1, retries + 1):
        try:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as r:
                if r.status in (429, 502, 503, 504):
                    wait = 2 ** attempt
                    log.warning("%s HTTP %s — retry in %ss", method, r.status, wait)
                    await asyncio.sleep(wait)
                    continue
                data = await r.json(content_type=None)
                if data.get("status") == "auth_error":
                    log.critical("Rubika auth error: %s", data)
                    return None
                if data.get("status") not in ("ok", None):
                    err = data.get("status_det", "")
                    if "transient" in str(err).lower():
                        await asyncio.sleep(2 ** attempt)
                        continue
                    log.warning("%s API error: %s", method, data)
                return data
        except (aiohttp.ClientConnectionError, asyncio.TimeoutError) as e:
            log.warning("%s connection error (attempt %d): %s", method, attempt, e)
            await asyncio.sleep(2 ** attempt)
    return None


async def rubika_send_message(chat_id: str, text: str,
                               session: aiohttp.ClientSession,
                               metadata: Optional[dict] = None) -> Optional[str]:
    payload = {"chat_id": chat_id, "text": text}
    if metadata:
        payload["metadata"] = metadata
    resp = await rubika_post("sendMessage", payload, session)
    if resp and "data" in resp:
        return resp["data"].get("message_id")
    return None


async def rubika_edit_message(chat_id: str, message_id: str, text: str,
                               session: aiohttp.ClientSession):
    await rubika_post("editMessageText",
                      {"chat_id": chat_id, "message_id": message_id, "text": text},
                      session)


async def rubika_upload_file(file_bytes: bytes, file_type: str,
                              session: aiohttp.ClientSession) -> Optional[str]:
    """Upload bytes → returns file_id or None."""
    # Step 1: request upload URL
    resp = await rubika_post("requestSendFile", {"type": file_type}, session)
    if not resp or "data" not in resp:
        log.error("requestSendFile failed")
        return None
    upload_url = resp["data"].get("upload_url")
    if not upload_url:
        log.error("No upload_url in response")
        return None

    # Step 2: POST file bytes
    try:
        form = aiohttp.FormData()
        form.add_field("file", file_bytes, filename="upload",
                       content_type="application/octet-stream")
        async with session.post(upload_url, data=form,
                                 timeout=aiohttp.ClientTimeout(total=120)) as r:
            data = await r.json(content_type=None)
            file_id = data.get("file_id") or (data.get("data") or {}).get("file_id")
            if file_id:
                return file_id
            log.error("Upload response missing file_id: %s", data)
    except Exception as e:
        log.error("File upload error: %s", e)
    return None


async def rubika_send_file(chat_id: str, file_id: str, caption: str,
                            session: aiohttp.ClientSession) -> Optional[str]:
    payload = {"chat_id": chat_id, "file_id": file_id, "text": caption}
    resp = await rubika_post("sendFile", payload, session)
    if resp and "data" in resp:
        return resp["data"].get("message_id")
    return None


async def broadcast_text(text: str, session: aiohttp.ClientSession) -> list:
    """Send text to all subscribers. Returns list of (chat_id, message_id)."""
    results = []
    for chat_id in list(subscribers):
        mid = await rubika_send_message(chat_id, text, session)
        if mid:
            results.append((chat_id, mid))
        await asyncio.sleep(0.05)
    return results


async def broadcast_file(file_id: str, caption: str,
                          session: aiohttp.ClientSession) -> list:
    results = []
    for chat_id in list(subscribers):
        mid = await rubika_send_file(chat_id, file_id, caption, session)
        if mid:
            results.append((chat_id, mid))
        await asyncio.sleep(0.05)
    return results


# ══════════════════════════════════════════════════════════════
# MESSAGE FORMATTING
# ══════════════════════════════════════════════════════════════

HEADER_LINE = "━━━━━━━━━━━━━━━━━━━━━━━━"

def _channel_display(entity) -> str:
    """Return the best display name for a Telegram channel entity."""
    if entity is None:
        return "📡 کانال"
    title = getattr(entity, "title", None) or getattr(entity, "username", None) or "کانال"
    return title


def format_message(raw_text: str, channel_name: str, timestamp: datetime) -> str:
    """
    Build a beautifully formatted Rubika message.

    Layout:
    ━━━━━━━━━━━━━━━━━━━━━━━
    📡 **ChannelName**
    🕐 _2025-01-01 12:00 UTC_
    ━━━━━━━━━━━━━━━━━━━━━━━

    <body — VPN lines in `monospace` + >quote>

    ━━━━━━━━━━━━━━━━━━━━━━━
    """
    ts_str = timestamp.strftime("%Y-%m-%d  %H:%M UTC")
    header = (
        f"{HEADER_LINE}\n"
        f"📡 **{channel_name}**\n"
        f"🕐 _{ts_str}_\n"
        f"{HEADER_LINE}\n\n"
    )

    if not raw_text:
        return header.rstrip()

    lines = raw_text.splitlines()
    formatted_lines = []

    # Group consecutive VPN lines together for a clean block
    vpn_buffer = []

    def flush_vpn():
        if not vpn_buffer:
            return
        block = "\n".join(vpn_buffer)
        formatted_lines.append(f"`{block}`")
        # Each line also as quote — Rubika uses >line syntax
        for vl in vpn_buffer:
            pass  # already in mono block above
        vpn_buffer.clear()

    for line in lines:
        stripped = line.strip()
        if VPN_SCHEMES.match(stripped):
            vpn_buffer.append(stripped)
        else:
            flush_vpn()
            formatted_lines.append(line)

    flush_vpn()

    body = "\n".join(formatted_lines)
    footer = f"\n\n{HEADER_LINE}"
    return header + body + footer


# ══════════════════════════════════════════════════════════════
# WELCOME MESSAGE
# ══════════════════════════════════════════════════════════════

WELCOME_TEXT = """\
🌐 **به ربات VPN خوش آمدید!**
━━━━━━━━━━━━━━━━━━━━━━━
سلام! 👋 شما با موفقیت عضو شدید.

✅ از این پس بهترین کانفیگ‌های VPN و پروکسی را مستقیماً اینجا دریافت خواهید کرد.

📌 **نحوه استفاده:**
• کانفیگ‌ها با قالب‌بندی ویژه ارسال می‌شوند
• کافیست روی هر لینک ضربه بزنید تا کپی شود
• آپدیت‌ها بلافاصله پس از انتشار ارسال می‌شوند

━━━━━━━━━━━━━━━━━━━━━━━
🔐 _اتصال امن، اینترنت آزاد_ 🚀\
"""


# ══════════════════════════════════════════════════════════════
# SUBSCRIBER POLLING
# ══════════════════════════════════════════════════════════════

async def poll_subscribers(session: aiohttp.ClientSession):
    global update_offset, subscribers
    payload = {"limit": 100}
    if update_offset:
        payload["offset_id"] = update_offset

    resp = await rubika_post("getUpdates", payload, session)
    if not resp or "data" not in resp:
        return

    data   = resp["data"]
    updates = data.get("updates", [])
    update_offset = data.get("next_offset_id", update_offset)

    changed = False
    for upd in updates:
        upd_type = upd.get("type", "")
        chat_id  = upd.get("chat_id", "")
        if not chat_id:
            continue

        is_new = False
        if upd_type == "StartedBot":
            is_new = True
        elif upd_type == "NewMessage":
            msg = upd.get("new_message", {})
            txt = (msg.get("text") or "").strip().lower()
            btn = (msg.get("aux_data") or {}).get("button_id", "")
            if txt in ("/start", "start") or btn == "start":
                is_new = True

        if is_new and chat_id not in subscribers:
            log.info("New subscriber: %s", chat_id)
            subscribers.add(chat_id)
            changed = True
            await rubika_send_message(chat_id, WELCOME_TEXT, session)

    if changed:
        save_subscribers()
        git_push("add subscriber(s)")


# ══════════════════════════════════════════════════════════════
# REACTION UPDATES
# ══════════════════════════════════════════════════════════════

REACTION_SCHEDULE = [3*60, 5*60, 10*60, 15*60, 25*60, 40*60, 60*60, 90*60, 120*60]


async def schedule_reaction_update(tg_client: TelegramClient,
                                    rubika_pairs: list,
                                    tg_channel, tg_msg_id: int,
                                    original_text: str,
                                    session: aiohttp.ClientSession):
    """Background task that edits Rubika messages with reaction counts."""
    send_time = time.monotonic()
    key = f"{tg_channel}_{tg_msg_id}"

    for delay in REACTION_SCHEDULE:
        await asyncio.sleep(delay - (time.monotonic() - send_time))
        try:
            msgs = await tg_client.get_messages(tg_channel, ids=[tg_msg_id])
            if not msgs or not msgs[0]:
                break
            msg = msgs[0]
            reactions = getattr(msg, "reactions", None)
            if reactions:
                top = sorted(
                    reactions.results or [],
                    key=lambda r: r.count, reverse=True
                )[:3]
                reaction_str = "  ".join(
                    f"{getattr(r.reaction, 'emoticon', '👍')} {r.count}"
                    for r in top
                )
                new_text = original_text + f"\n\n{reaction_str}"
            else:
                new_text = original_text

            for chat_id, msg_id in rubika_pairs:
                await rubika_edit_message(chat_id, msg_id, new_text, session)
        except Exception as e:
            log.debug("Reaction update error: %s", e)
            break


# ══════════════════════════════════════════════════════════════
# TELEGRAM MEDIA → RUBIKA
# ══════════════════════════════════════════════════════════════

def _detect_file_type(media) -> str:
    """Return Rubika FileTypeEnum string."""
    if isinstance(media, MessageMediaPhoto):
        return "Image"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        for attr in doc.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                if getattr(attr, "round_message", False):
                    return "Video"
                return "Video"
            if isinstance(attr, DocumentAttributeAudio):
                if getattr(attr, "voice", False):
                    return "Voice"
                return "Music"
        mime = getattr(doc, "mime_type", "")
        if mime.startswith("image/gif") or mime == "video/mp4":
            return "Gif"
        return "File"
    return "File"


async def forward_media(tg_client: TelegramClient, tg_msg,
                         caption: str, session: aiohttp.ClientSession) -> list:
    """Download from Telegram, upload to Rubika, broadcast. Returns sent pairs."""
    media     = tg_msg.media
    file_type = _detect_file_type(media)
    limit     = SIZE_LIMITS.get(file_type, SIZE_LIMITS["File"])

    # Check size before downloading
    if isinstance(media, MessageMediaDocument):
        size = getattr(media.document, "size", 0) or 0
        if size > limit:
            note = (
                f"⚠️ فایل دریافتی ({file_type}) بزرگ‌تر از حد مجاز "
                f"({limit // 1024 // 1024} MB) است و ارسال نشد.\n\n"
                + caption
            )
            return await broadcast_text(note, session)

    log.info("Downloading %s …", file_type)
    try:
        file_bytes = await tg_client.download_media(tg_msg, bytes)
    except Exception as e:
        log.error("Telegram download failed: %s", e)
        return []

    if len(file_bytes) > limit:
        note = (
            f"⚠️ فایل ({file_type}) پس از دانلود از حد مجاز عبور کرد و ارسال نشد.\n\n"
            + caption
        )
        return await broadcast_text(note, session)

    log.info("Uploading %d bytes as %s …", len(file_bytes), file_type)
    file_id = await rubika_upload_file(file_bytes, file_type, session)
    if not file_id:
        log.error("Upload failed — skipping media")
        return []

    pairs = await broadcast_file(file_id, caption, session)
    log.info("Sent media %s to %d subscribers", file_type, len(pairs))
    return pairs


# ══════════════════════════════════════════════════════════════
# CATCH-UP (missed messages on startup)
# ══════════════════════════════════════════════════════════════

async def catch_up(tg_client: TelegramClient, channels: list,
                   state: dict, session: aiohttp.ClientSession):
    log.info("Running catch-up for %d channels …", len(channels))
    for ch in channels:
        try:
            entity  = await tg_client.get_entity(ch)
            ch_name = _channel_display(entity)
            last_id = state.get(str(ch), 0)
            msgs    = await tg_client.get_messages(entity, limit=10)
            msgs    = sorted(msgs, key=lambda m: m.id)
            for msg in msgs:
                if msg.id <= last_id:
                    continue
                await handle_tg_message(tg_client, msg, entity, ch_name, session)
                state[str(ch)] = msg.id
        except Exception as e:
            log.warning("Catch-up failed for %s: %s", ch, e)
    save_state(state)
    log.info("Catch-up complete ✓")


# ══════════════════════════════════════════════════════════════
# CORE MESSAGE HANDLER
# ══════════════════════════════════════════════════════════════

async def handle_tg_message(tg_client: TelegramClient, msg,
                              entity, channel_name: str,
                              session: aiohttp.ClientSession):
    if not subscribers:
        return

    ts       = msg.date.astimezone(timezone.utc)
    raw_text = msg.text or msg.message or ""
    caption  = format_message(raw_text, channel_name, ts)

    if msg.media:
        pairs = await forward_media(tg_client, msg, caption, session)
    else:
        if not raw_text.strip():
            return
        pairs = await broadcast_text(caption, session)
        log.info("Forwarded text to %d subscribers", len(pairs))

    # Schedule reaction updates in background
    if pairs and (msg.media is None):
        asyncio.create_task(
            schedule_reaction_update(
                tg_client, pairs, entity, msg.id, caption, session
            )
        )


# ══════════════════════════════════════════════════════════════
# ADMIN NOTIFICATION
# ══════════════════════════════════════════════════════════════

async def notify_admin(text: str, session: aiohttp.ClientSession):
    if ADMIN_CHAT_ID:
        await rubika_send_message(ADMIN_CHAT_ID, text, session)


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

async def main():
    # ── Git clone data repo ──
    if DATA_REPO_DIR.exists():
        log.info("data_repo already exists, skipping clone")
    else:
        git_clone()

    load_subscribers()
    state = load_state()

    # ── Load channels ──
    channels_path = Path(CHANNELS_FILE)
    if not channels_path.exists():
        log.critical("channels.json not found!")
        sys.exit(1)
    channels: list = json.loads(channels_path.read_text())
    log.info("Monitoring %d channels: %s", len(channels), channels)

    # ── Start Telegram client ──
    tg_client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await tg_client.start()
    log.info("Telegram client connected ✓")

    async with aiohttp.ClientSession() as session:
        await notify_admin("🟢 ربات فوروارد راه‌اندازی شد.", session)

        # ── Catch-up ──
        await catch_up(tg_client, channels, state, session)

        # ── Register Telegram event handler ──
        resolved_entities = {}
        for ch in channels:
            try:
                ent = await tg_client.get_entity(ch)
                resolved_entities[ent.id] = ent
            except Exception as e:
                log.warning("Could not resolve %s: %s", ch, e)

        @tg_client.on(events.NewMessage(chats=list(resolved_entities.keys())))
        async def on_new_message(event):
            msg    = event.message
            entity = resolved_entities.get(event.chat_id)
            ch_name = _channel_display(entity)
            state[str(event.chat_id)] = msg.id
            save_state(state)
            await handle_tg_message(tg_client, msg, entity, ch_name, session)

        # ── Background: subscriber polling ──
        async def subscriber_loop():
            while True:
                try:
                    await poll_subscribers(session)
                except Exception as e:
                    log.error("Subscriber poll error: %s", e)
                await asyncio.sleep(POLL_INTERVAL)

        poll_task = asyncio.create_task(subscriber_loop())

        # ── Run until RUN_DURATION ──
        log.info("Bot running for %d seconds …", RUN_DURATION)
        try:
            await asyncio.sleep(RUN_DURATION)
        except asyncio.CancelledError:
            pass
        finally:
            poll_task.cancel()
            log.info("Shutting down …")
            save_subscribers()
            save_state(state)
            git_push("final state on shutdown")
            await tg_client.disconnect()
            await notify_admin("🔴 ربات فوروارد متوقف شد.", session)
            log.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(main())
