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
SUBSCRIBER_REFRESH_INTERVAL = 60  # Poll Rubika getUpdates every 1 minute
REACTION_EDIT_SCHEDULE = [
    (180,  "3m "),
    (300,  "5m "),
    (600,  "10m "),
    (900,  "15m "),
    (1500, "25m "),
    (1800, "30m "),
    (3600, "1H "),
    (7200, "2H "),
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
# MARKDOWN & FORMATTING (Quote + Mono for Rubika)
# ─────────────────────────────────────────────
def md_bold(text):
    return f"*{text}*"

def md_italic(text):
    return f"_{text}_"

def md_mono(text):
    # Using backticks for monospace (Markdown compatible with Rubika parse_mode="Markdown")
    return f"`{text}`"

def md_quote(text):
    # Using > prefix for quote blocks (Markdown compatible)
    lines = text.split('\n')
    return '\n'.join(f"> {line}" for line in lines)

def md_code(text):
    # For multi-line code blocks, use triple backticks
    return f"```\n{text}\n```"

def is_proxy_line(line):
    """Check if a line is a VPN/proxy config line"""
    line = line.strip().lower()
    return any(line.startswith(prefix) for prefix in VPN_PREFIXES)

def format_proxy_text(text):
    """
    Format proxy configs with Quote + Mono formatting.
    Handles bulk IPs sent consecutively or with line breaks.
    """
    if not text:
        return ""
    
    lines = text.splitlines()
    result = []
    proxy_buffer = []

    def flush_proxy_buffer():
        nonlocal proxy_buffer
        if proxy_buffer:
            # Apply Mono formatting to each proxy line, then wrap in Quote block
            mono_lines = [md_mono(line) for line in proxy_buffer]
            quoted_block = md_quote('\n'.join(mono_lines))
            result.append(quoted_block)
            proxy_buffer = []

    for line in lines:
        stripped = line.strip()
        
        if is_proxy_line(stripped):
            # Clean the line and add to buffer
            proxy_buffer.append(stripped)
        else:
            flush_proxy_buffer()
            if stripped:  # Only add non-empty non-proxy lines
                result.append(line)

    flush_proxy_buffer()
    return '\n'.join(result)

# ─────────────────────────────────────────────
# UX
# ─────────────────────────────────────────────
def build_header(channel_name, msg_date):
    date_str = msg_date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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
    """Welcome message for newly accepted users"""
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
    if not message.reactions or not message.reactions.results:
        return ""
    
    reactions = []
    for item in message.reactions.results:
        emoji = getattr(item.reaction, "emoticon", str(item.reaction))
        reactions.append((emoji, item.count))
    
    reactions.sort(key=lambda x: x[1], reverse=True)
    top = reactions[:3]
    
    return "  ".join(f"{emoji} {md_bold(str(count))}" for emoji, count in top)

# ─────────────────────────────────────────────
# FILES
# ─────────────────────────────────────────────
def load_channels():
    if not CHANNELS_FILE.exists():
        logger.error("❌ channels.json not found")
        sys.exit(1)
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        channels = json.load(f)
        logger.info(f"✅ Loaded {len(channels)} channels: {channels}")
        return channels

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            logger.debug(f"📦 Loaded state with {len(state)} entries")
            return state
    logger.info("📦 No state file found, starting fresh")
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.debug(f"💾 State saved to {STATE_FILE}")

def load_subscribers():
    """Load existing subscribers from the private repo file"""
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            subs = set(json.load(f))
            logger.info(f"👥 Loaded {len(subs)} existing subscribers from repo")
            return subs
    logger.info("👥 No subscribers file found, starting with empty set")
    return set()

def save_subscribers(subscribers):
    """Save subscribers to the private repo file"""
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(subscribers), f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Saved {len(subscribers)} subscribers to {SUBSCRIBERS_FILE}")

# ─────────────────────────────────────────────
# RUBIKA API
# ─────────────────────────────────────────────
def rubika_post(method, payload=None, timeout=20):
    """Make POST request to Rubika Bot API"""
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{method}"
    try:
        logger.debug(f"🔗 Calling Rubika API: {method}")
        response = requests.post(url, json=payload or {}, timeout=timeout)
        
        if response.status_code != 200:
            logger.error(f"❌ {method} HTTP {response.status_code}: {response.text[:300]}")
            return None
        
        result = response.json()
        logger.debug(f"✅ {method} response status: {result.get('status')}")
        return result
        
    except Exception as e:
        logger.error(f"❌ {method} request error: {e}")
        return None

def send_text(chat_id, text):
    """Send text message via Rubika Bot API with Markdown parse_mode"""
    logger.info(f"📤 Sending text to chat_id: {chat_id}")
    data = rubika_post("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    })
    
    if not data:
        logger.error(f"❌ Failed to send message to {chat_id}")
        return False, None
    
    if data.get("status") == "OK":
        msg_id = data.get("data", {}).get("message_id")
        logger.info(f"✅ Message sent to {chat_id}, msg_id: {msg_id}")
        return True, msg_id
    
    logger.error(f"❌ sendMessage returned non-OK status: {data}")
    return False, None

def edit_text(chat_id, message_id, text):
    """Edit existing message text via Rubika Bot API"""
    logger.debug(f"✏️ Editing message {message_id} in chat {chat_id}")
    data = rubika_post("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
    })
    success = bool(data and data.get("status") == "OK")
    logger.debug(f"{'✅' if success else '❌'} Edit result: {success}")
    return success

def send_file(chat_id, file_id, caption):
    """Send file via Rubika Bot API"""
    logger.info(f"📎 Sending file {file_id} to chat_id: {chat_id}")
    data = rubika_post("sendFile", {
        "chat_id": chat_id,
        "file_id": file_id,
        "text": caption,
        "parse_mode": "Markdown",
    })
    
    if not data:
        logger.error(f"❌ Failed to send file to {chat_id}")
        return False, None
    
    if data.get("status") == "OK":
        msg_id = data.get("data", {}).get("message_id")
        logger.info(f"✅ File sent to {chat_id}, msg_id: {msg_id}")
        return True, msg_id
    
    logger.error(f"❌ sendFile returned non-OK status: {data}")
    return False, None

def upload_file(file_bytes, filename, file_type):
    """Upload file to Rubika and get file_id"""
    logger.info(f"⬆️ Uploading file: {filename} (type: {file_type})")
    
    req = rubika_post("requestSendFile", {"type": file_type})
    if not req:
        logger.error("❌ requestSendFile failed")
        return None
    
    upload_url = req.get("data", {}).get("upload_url")
    if not upload_url:
        logger.error("❌ upload_url missing from response")
        return None
    
    try:
        response = requests.post(
            upload_url,
            files={"file": (filename, file_bytes)},
            timeout=120,
        )
        
        if response.status_code != 200:
            logger.error(f"❌ Upload HTTP {response.status_code}")
            return None
        
        data = response.json()
        file_id = data.get("data", {}).get("file_id")
        
        if file_id:
            logger.info(f"✅ File uploaded successfully, file_id: {file_id}")
            return file_id
        logger.error("❌ file_id not found in upload response")
        
    except Exception as e:
        logger.error(f"❌ Upload exception: {e}")
    
    return None

# ─────────────────────────────────────────────
# GET UPDATES & SUBSCRIBER MANAGEMENT
# ─────────────────────────────────────────────
def extract_new_subscribers(updates, existing_subscribers):
    """Extract new chat_ids from Rubika updates"""
    new_users = set()
    logger.debug(f"🔍 Processing {len(updates)} updates for new subscribers")
    
    for update in updates:
        chat_id = update.get("chat_id")
        if not chat_id:
            continue
        
        update_type = update.get("type", "")
        
        # Case 1: User started the bot (StartedBot event)
        if update_type == "StartedBot":
            if chat_id not in existing_subscribers:
                logger.info(f"🆕 New subscriber via StartedBot: {chat_id}")
                new_users.add(chat_id)
            continue
        
        # Case 2: User sent /start message or clicked start button
        if update_type == "NewMessage":
            msg = update.get("new_message", {})
            text = str(msg.get("text", "")).strip().lower()
            aux_data = msg.get("aux_data", {})
            button_id = str(aux_data.get("button_id", "")).lower()
            
            if text == "/start" or button_id == "start":
                if chat_id not in existing_subscribers:
                    logger.info(f"🆕 New subscriber via /start: {chat_id}")
                    new_users.add(chat_id)
    
    logger.info(f"📊 Found {len(new_users)} new subscribers in this batch")
    return new_users

def _add_new_subscribers_and_push(new_users, subscribers):
    """
    Add new subscribers, send welcome message, save to repo, and push immediately.
    Includes extensive logging as requested.
    """
    if not new_users:
        logger.debug("ℹ️ No new subscribers to process")
        return
    
    logger.info(f"🚀 Processing {len(new_users)} new subscriber(s)")
    
    for chat_id in sorted(new_users):
        try:
            logger.info(f"👤 Adding new subscriber: {chat_id}")
            
            # Send welcome message: "شما پذیرفته شدید"
            ok, msg_id = send_text(chat_id, build_welcome())
            
            if ok and msg_id:
                subscribers.add(chat_id)
                logger.info(f"✅ Welcome message sent to {chat_id} (msg_id: {msg_id})")
            else:
                logger.warning(f"⚠️ Failed to send welcome to {chat_id}, but still adding to list")
                subscribers.add(chat_id)  # Add anyway to avoid repeated attempts
                
        except Exception as e:
            logger.error(f"❌ Error processing subscriber {chat_id}: {e}")
            continue
    
    # Save updated subscribers list to local repo file
    logger.info(f"💾 Saving {len(subscribers)} total subscribers to {SUBSCRIBERS_FILE}")
    save_subscribers(subscribers)
    
    # Push to remote repo immediately to sync new subscribers
    logger.info("🔄 Pushing updated subscribers to remote repo...")
    push_repo()
    logger.info("✅ Repo push completed")

def fetch_updates(subscribers, state):
    """
    Fetch updates from Rubika Bot API every 1 minute.
    Compares new chat_ids with existing ones and adds new subscribers.
    """
    logger.info("🔄 Fetching updates from Rubika (getUpdates)...")
    
    payload = {"limit": 200, "state": "all"}
    offset_id = state.get("rubika_offset")
    
    if offset_id:
        payload["offset_id"] = offset_id
        logger.debug(f"📍 Using offset_id: {offset_id}")
    
    data = rubika_post("getUpdates", payload, timeout=40)
    
    if not data:
        logger.warning("⚠️ getUpdates returned no data")
        return subscribers, state
    
    updates = data.get("data", {}).get("updates", [])
    next_offset = data.get("data", {}).get("next_offset_id")
    
    logger.info(f"📬 Received {len(updates)} updates from Rubika")
    
    # Extract and process new subscribers
    new_users = extract_new_subscribers(updates, subscribers)
    
    # Always include admin if configured
    if ADMIN_CHAT_ID and ADMIN_CHAT_ID not in subscribers:
        logger.info(f"👮 Adding admin chat_id: {ADMIN_CHAT_ID}")
        subscribers.add(ADMIN_CHAT_ID)
    
    # Add new subscribers and push to repo immediately
    _add_new_subscribers_and_push(new_users, subscribers)
    
    # Update offset for next polling cycle
    if next_offset:
        state["rubika_offset"] = next_offset
        save_state(state)
        logger.debug(f"📍 Updated rubika_offset to: {next_offset}")
    
    logger.info("✅ fetch_updates completed successfully")
    return subscribers, state

# ─────────────────────────────────────────────
# MEDIA
# ─────────────────────────────────────────────
def get_file_type(media):
    """Determine file type for Rubika upload"""
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
    """Broadcast text message to all subscribers"""
    results = []
    logger.info(f"📢 Broadcasting text to {len(subscribers)} subscribers")
    
    for chat_id in list(subscribers):
        try:
            ok, msg_id = send_text(chat_id, text)
            if ok and msg_id:
                results.append({"chat_id": chat_id, "msg_id": msg_id, "full_text": text})
        except Exception as e:
            logger.error(f"❌ Failed to broadcast to {chat_id}: {e}")
    
    logger.info(f"✅ Broadcast completed: {len(results)}/{len(subscribers)} successful")
    return results

def broadcast_file(subscribers, file_id, caption):
    """Broadcast file to all subscribers"""
    results = []
    logger.info(f"📎 Broadcasting file {file_id} to {len(subscribers)} subscribers")
    
    for chat_id in list(subscribers):
        try:
            ok, msg_id = send_file(chat_id, file_id, caption)
            if ok and msg_id:
                results.append({"chat_id": chat_id, "msg_id": msg_id, "full_text": caption})
        except Exception as e:
            logger.error(f"❌ Failed to broadcast file to {chat_id}: {e}")
    
    logger.info(f"✅ File broadcast completed: {len(results)}/{len(subscribers)} successful")
    return results

# ─────────────────────────────────────────────
# REACTIONS
# ─────────────────────────────────────────────
pending_edits = {}

async def delayed_reaction_updates(client, channel_name, tg_msg_id):
    """Periodically update messages with reaction counts"""
    key = (channel_name, tg_msg_id)
    start = time.monotonic()
    
    for seconds, label in REACTION_EDIT_SCHEDULE:
        wait = seconds - (time.monotonic() - start)
        if wait > 0:
            await asyncio.sleep(wait)
        
        entries = pending_edits.get(key)
        if not entries:
            logger.debug(f"⏭️ No pending edits for {key}, stopping reaction updates")
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
                edit_text(entry["chat_id"], entry["msg_id"], new_text)
            
            logger.info(f"⭐ Reaction edit {label}: {channel_name}#{tg_msg_id}")
            
        except Exception as e:
            logger.error(f"❌ Reaction update error: {e}")
    
    pending_edits.pop(key, None)
    logger.debug(f"🧹 Cleaned up pending edits for {key}")

# ─────────────────────────────────────────────
# FORWARD
# ─────────────────────────────────────────────
async def forward_message(client, message, channel_name, state, subscribers):
    """Forward Telegram message to Rubika subscribers with proper formatting"""
    
    # Skip already processed messages
    if message.id <= state.get(channel_name, 0):
        logger.debug(f"⏭️ Skipping already forwarded message {message.id} from {channel_name}")
        return
    
    if not subscribers:
        logger.warning(f"⚠️ No subscribers to forward message {message.id} to")
        return
    
    # Normalize message date to UTC
    msg_date = message.date
    if msg_date.tzinfo is None:
        msg_date = msg_date.replace(tzinfo=timezone.utc)
    
    # ── TEXT MESSAGE ──
    if message.text and not message.media:
        logger.info(f"📝 Forwarding text message {message.id} from {channel_name}")
        
        text = build_message(channel_name, msg_date, message.text)
        deliveries = broadcast_text(subscribers, text)
        
        if deliveries:
            state[channel_name] = message.id
            save_state(state)
            
            # Schedule reaction updates
            key = (channel_name, message.id)
            pending_edits[key] = deliveries
            asyncio.create_task(delayed_reaction_updates(client, channel_name, message.id))
            logger.debug(f"⏰ Scheduled reaction updates for {key}")
        return
    
    # ── MEDIA MESSAGE ──
    if not message.media or not message.file:
        logger.debug(f"⏭️ Skipping message {message.id}: no media or file info")
        return
    
    file_type = get_file_type(message.media)
    max_bytes = MAX_FILE_SIZE_MB.get(file_type, 50) * 1024 * 1024
    
    # Check file size limit
    if message.file.size > max_bytes:
        size_mb = message.file.size / 1024 / 1024
        logger.warning(f"⚠️ File too large: {size_mb:.1f}MB > {max_bytes/1024/1024:.0f}MB limit")
        broadcast_text(subscribers, build_skip_message(file_type, size_mb))
        return
    
    # Download and upload file
    try:
        logger.info(f"⬇️ Downloading media from message {message.id}")
        file_bytes = await client.download_media(message, file=bytes)
    except Exception as e:
        logger.error(f"❌ Download failed for message {message.id}: {e}")
        return
    
    filename = message.file.name or "file.bin"
    file_id = upload_file(file_bytes, filename, file_type)
    
    if not file_id:
        logger.error(f"❌ Upload failed for message {message.id}")
        return
    
    # Build caption with Quote+Mono formatted proxy configs
    caption = build_message(channel_name, msg_date, message.text or "")
    deliveries = broadcast_file(subscribers, file_id, caption)
    
    if deliveries:
        state[channel_name] = message.id
        save_state(state)
        
        key = (channel_name, message.id)
        pending_edits[key] = deliveries
        asyncio.create_task(delayed_reaction_updates(client, channel_name, message.id))
        logger.debug(f"⏰ Scheduled reaction updates for {key}")

# ─────────────────────────────────────────────
# GIT OPERATIONS
# ─────────────────────────────────────────────
def clone_repo():
    """Clone the private data repo with PAT authentication"""
    logger.info(f"🔽 Cloning repo: {DATA_REPO_URL}")
    
    if DATA_REPO_DIR.exists():
        logger.debug("🗑️ Removing existing data_repo directory")
        shutil.rmtree(DATA_REPO_DIR)
    
    # Create credential helper script
    helper = Path("/tmp/git-helper.sh")
    helper.write_text(f"#!/bin/sh\necho 'username=x-access-token'\necho 'password={DATA_REPO_PAT}'\n")
    os.chmod(helper, 0o755)
    
    try:
        subprocess.run(
            ["git", "-c", f"credential.helper={helper}", "clone", "--depth", "1", 
             DATA_REPO_URL, str(DATA_REPO_DIR)],
            check=True, capture_output=True, text=True
        )
        logger.info("✅ Data repo cloned successfully")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Git clone failed: {e.stderr}")
        raise
    finally:
        helper.unlink(missing_ok=True)

def push_repo():
    """Push changes to the private data repo"""
    logger.info("🔄 Preparing to push changes to repo...")
    
    try:
        # Configure git user
        subprocess.run(["git", "config", "user.email", "actions@github.com"], 
                      cwd=DATA_REPO_DIR, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], 
                      cwd=DATA_REPO_DIR, check=True, capture_output=True)
        
        # Stage changes
        subprocess.run(["git", "add", "."], cwd=DATA_REPO_DIR, check=True, capture_output=True)
        
        # Check if there are changes to commit
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], 
                             cwd=DATA_REPO_DIR, capture_output=True)
        
        if diff.returncode != 0:
            # Commit and push
            subprocess.run(["git", "commit", "-m", "update subscribers/state"], 
                          cwd=DATA_REPO_DIR, check=True, capture_output=True)
            subprocess.run(["git", "push"], cwd=DATA_REPO_DIR, check=True, capture_output=True)
            logger.info("✅ Changes pushed to remote repo")
        else:
            logger.debug("ℹ️ No changes to push")
            
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Git push failed: {e.stderr}")
    except Exception as e:
        logger.error(f"❌ Push error: {e}")

# ─────────────────────────────────────────────
# CATCHUP
# ─────────────────────────────────────────────
async def catchup(client, channels, state, subscribers):
    """Catch up on missed messages from Telegram channels"""
    
    if not state:
        logger.info("🚀 First run - setting message markers for all channels")
        for channel in channels:
            try:
                entity = await asyncio.wait_for(client.get_entity(channel), timeout=5.0)
                msgs = await asyncio.wait_for(client.get_messages(entity, limit=1), timeout=5.0)
                state[channel] = msgs[0].id if msgs else 0
                logger.info(f"📍 Marker set for {channel}: {state[channel]}")
            except asyncio.TimeoutError:
                logger.error(f"⏰ Timeout accessing {channel} – skipping")
                state[channel] = 0
            except Exception as e:
                logger.error(f"❌ Error with {channel}: {e}")
                state[channel] = 0
        save_state(state)
        return
    
    # Normal catch-up: fetch recent messages
    logger.info("🔄 Catching up on recent messages...")
    for channel in channels:
        try:
            entity = await asyncio.wait_for(client.get_entity(channel), timeout=5.0)
            msgs = await asyncio.wait_for(client.get_messages(entity, limit=10), timeout=5.0)
            
            for msg in reversed(msgs):  # Process oldest first
                if msg.id <= state.get(channel, 0):
                    continue
                logger.info(f"📬 Catching up message {msg.id} from {channel}")
                await forward_message(client, msg, channel, state, subscribers)
                
        except asyncio.TimeoutError:
            logger.error(f"⏰ Timeout catching up on {channel} – skipping")
        except Exception as e:
            logger.error(f"❌ Catchup error on {channel}: {e}")

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    logger.info("🚀 Starting Public Forwarder...")
    
    # Initialize repo and load data
    clone_repo()
    channels = load_channels()
    state = load_state()
    subscribers = load_subscribers()
    
    logger.info(f"📊 Status: {len(channels)} channels, {len(subscribers)} subscribers")
    
    # Initial poll for new subscribers (immediate)
    logger.info("🔄 Running initial subscriber poll...")
    subscribers, state = fetch_updates(subscribers, state)
    
    # Connect to Telegram
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("✅ Telegram client connected")
    
    # Catch up on missed messages
    await catchup(client, channels, state, subscribers)
    
    # Register message handler for real-time forwarding
    @client.on(events.NewMessage(chats=channels))
    async def new_message(event):
        try:
            chat = await event.get_chat()
            channel_name = chat.title or chat.username or str(chat.id)
            logger.info(f"📨 New message detected from {channel_name} (id: {event.message.id})")
            await forward_message(client, event.message, channel_name, state, subscribers)
        except Exception as e:
            logger.error(f"❌ Handler error: {e}")
    
    # Background task: poll Rubika for new subscribers every 1 minute
    async def poll_subscribers():
        nonlocal subscribers, state
        poll_count = 0
        
        while True:
            poll_count += 1
            logger.info(f"🔄 Poll #{poll_count}: Fetching Rubika updates...")
            
            try:
                subscribers, state = fetch_updates(subscribers, state)
                logger.info(f"✅ Poll #{poll_count} completed - {len(subscribers)} total subscribers")
            except Exception as e:
                logger.error(f"❌ Poll #{poll_count} error: {e}")
            
            await asyncio.sleep(SUBSCRIBER_REFRESH_INTERVAL)
    
    # Start polling task
    asyncio.create_task(poll_subscribers())
    logger.info(f"⏱️ Subscriber polling started (interval: {SUBSCRIBER_REFRESH_INTERVAL}s)")
    
    # Main loop: run for configured duration
    start = time.monotonic()
    logger.info(f"⏰ Running for {RUN_DURATION} seconds...")
    
    while time.monotonic() - start < RUN_DURATION:
        await asyncio.sleep(30)
        logger.debug("❤️ Heartbeat - forwarder still running")
    
    # Cleanup: save state and push final changes
    logger.info("🛑 Run duration reached, saving state and pushing changes...")
    save_state(state)
    save_subscribers(subscribers)
    push_repo()
    
    await client.disconnect()
    logger.info("✅ Public Forwarder finished successfully")

if __name__ == "__main__":
    asyncio.run(main())
