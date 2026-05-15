#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Public Forwarder for Rubika Bot
- Forwards Telegram channel messages to Rubika subscribers
- Detects new subscribers via getUpdates every 60 seconds
- Formats proxy configs with Quote + Mono for Rubika Markdown
- Syncs subscribers.json to private repo immediately on new user
"""

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
# ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]
DATA_REPO_PAT = os.environ["DATA_REPO_PAT"]
DATA_REPO_URL = os.environ["DATA_REPO_URL"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
RUN_DURATION = 20400  # 5h 40min runtime
SUBSCRIBER_REFRESH_INTERVAL = 60  # Poll Rubika every 60 seconds

REACTION_EDIT_SCHEDULE = [
    (180, "3m "), (300, "5m "), (600, "10m "), (900, "15m "),
    (1500, "25m "), (1800, "30m "), (3600, "1H "), (7200, "2H "),
]

MAX_FILE_SIZE_MB = {
    "Image": 10, "Video": 50, "File": 50,
    "Music": 50, "Voice": 10, "Gif": 50,
}

VPN_PREFIXES = (
    "vmess://", "vless://", "trojan://", "ss://", "ssr://",
    "hysteria://", "hysteria2://", "tuic://", "wireguard://", "socks5://",
)

# ─────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────
DATA_REPO_DIR = Path("data_repo")
STATE_FILE = DATA_REPO_DIR / "state.json"
SUBSCRIBERS_FILE = DATA_REPO_DIR / "subscribers.json"
CHANNELS_FILE = Path("channels.json")

# ─────────────────────────────────────────────
# LOGGING SETUP
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("forwarder")

# ─────────────────────────────────────────────
# MARKDOWN FORMATTING (Quote + Mono for Rubika)
# ─────────────────────────────────────────────
def md_bold(text: str) -> str:
    return f"*{text}*"

def md_italic(text: str) -> str:
    return f"_{text}_"

def md_mono(text: str) -> str:
    escaped = str(text).replace("`", "\\`")
    return f"`{escaped}`"

def md_quote(text: str) -> str:
    lines = str(text).split('\n')
    return '\n'.join(f"> {line}" for line in lines if line.strip())

def md_code_block(text: str) -> str:
    escaped = str(text).replace("```", "\\`\\`\\`")
    return f"```\n{escaped}\n```"

def is_proxy_line(line: str) -> bool:
    line = line.strip().lower()
    return any(line.startswith(prefix) for prefix in VPN_PREFIXES)

def format_proxy_text(text: str) -> str:
    """Format proxy configs with Quote + Mono. Handles bulk IPs with line breaks."""
    if not text:
        return ""
    
    lines = text.splitlines()
    result = []
    proxy_buffer = []

    def flush_proxy_buffer():
        nonlocal proxy_buffer
        if proxy_buffer:
            mono_lines = [md_mono(line) for line in proxy_buffer]
            quoted_block = md_quote('\n'.join(mono_lines))
            result.append(quoted_block)
            proxy_buffer = []

    for line in lines:
        stripped = line.strip()
        if is_proxy_line(stripped):
            proxy_buffer.append(stripped)
        else:
            flush_proxy_buffer()
            if stripped:
                result.append(line)
    
    flush_proxy_buffer()
    return '\n\n'.join(result) if result else ""

# ─────────────────────────────────────────────
# UX MESSAGES (Improved)
# ─────────────────────────────────────────────
def build_header(channel_name: str, msg_date) -> str:
    date_str = msg_date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📡  {md_bold(channel_name)}\n"
        f"🕐  {md_italic(date_str)}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

def build_message(channel_name: str, msg_date, body: str) -> str:
    header = build_header(channel_name, msg_date)
    formatted_body = format_proxy_text(body)
    if formatted_body:
        return f"{header}\n\n{formatted_body}"
    return header

def build_welcome() -> str:
    """Beautiful welcome message for newly accepted users."""
    return (
        "✨ *شما پذیرفته شدید!* ✨\n\n"
        "✅ کانفیگ‌های جدید به‌صورت *خودکار* برایتان ارسال می‌شود.\n"
        "📋 لینک‌های پروکسی داخل بخش *قابل‌کپی* قرار می‌گیرند.\n"
        "🔄 برای دریافت کانفیگ‌های قدیمی، مجدد /start بزنید.\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 *راهنمای سریع:*\n"
        f"• کانفیگ‌ها با فرمت {md_mono('mono')} ارسال می‌شوند تا راحت کپی کنید.\n"
        "• اگر پیامی را از دست دادید، مجدد /start بزنید.\n"
        "• برای پشتیبانی، به ادمین پیام دهید.\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🎉 *موفق باشید!*"
    )

def build_skip_message(file_type: str, size_mb: float) -> str:
    return (
        f"⚠️ {md_bold('فایل بزرگ رد شد')}\n\n"
        f"> نوع: {md_mono(file_type)}\n"
        f"> حجم: {md_mono(f'{size_mb:.1f} MB')}\n\n"
        "فایل‌های بزرگ‌تر از حد مجاز ارسال نمی‌شوند."
    )

def get_top_reactions(message) -> str:
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
# FILE OPERATIONS
# ─────────────────────────────────────────────
def load_channels() -> list:
    if not CHANNELS_FILE.exists():
        logger.error("❌ channels.json not found")
        sys.exit(1)
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        channels = [ch.strip() for ch in json.load(f) if ch.strip()]
        logger.info(f"✅ Loaded {len(channels)} channels: {channels}")
        return channels

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            logger.debug(f"📦 Loaded state with {len(state)} entries")
            return state
    logger.info("📦 No state file found, starting fresh")
    return {}

def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.debug(f"💾 State saved to {STATE_FILE}")

def load_subscribers() -> set:
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            subs = set(json.load(f))
            logger.info(f"👥 Loaded {len(subs)} existing subscribers")
            return subs
    logger.info("👥 No subscribers file found, starting empty")
    return set()

def save_subscribers(subscribers: set):
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(subscribers), f, ensure_ascii=False, indent=2)
    logger.info(f"💾 Saved {len(subscribers)} subscribers to {SUBSCRIBERS_FILE}")

# ─────────────────────────────────────────────
# RUBIKA API CLIENT
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
# RUBIKA API CLIENT - FIXED WITH BETTER ERROR HANDLING
# ─────────────────────────────────────────────
def rubika_post(method: str, payload: dict = None, timeout: int = 20, max_retries: int = 3) -> dict:
    """
    Make POST request to Rubika Bot API with retry logic and detailed error logging.
    """
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{method}"
    
    for attempt in range(max_retries):
        try:
            logger.debug(f"🔗 Calling Rubika API: {method} (attempt {attempt+1}/{max_retries})")
            logger.debug(f"📦 Payload: {json.dumps(payload or {}, indent=2)[:500]}")
            
            response = requests.post(url, json=payload or {}, timeout=timeout)
            
            # Log raw response for debugging
            response_text = response.text[:1000] if response.text else "(empty)"
            logger.debug(f"📥 Raw response (HTTP {response.status_code}): {response_text}")
            
            if response.status_code != 200:
                logger.error(f"❌ {method} HTTP {response.status_code}: {response_text}")
                if response.status_code in (429, 502, 503, 504):
                    # Retry on rate limit or server error
                    wait_time = 2 ** attempt  # Exponential backoff: 2s, 4s, 8s
                    logger.warning(f"⏳ Retrying in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                return None
            
            # Parse JSON response
            try:
                result = response.json()
            except json.JSONDecodeError as e:
                logger.error(f"❌ {method} invalid JSON: {e} | Response: {response_text}")
                return None
            
            logger.debug(f"✅ {method} parsed response status: {result.get('status')}")
            
            # Check for API-level errors (not HTTP errors)
            if result.get("status") != "OK":
                error_data = result.get("data", {})
                error_msg = error_data.get("error", error_data.get("message", "Unknown API error"))
                error_code = error_data.get("code", "N/A")
                logger.error(f"❌ {method} API error [{error_code}]: {error_msg}")
                
                # Don't retry on auth errors (401, invalid token)
                if "auth" in error_msg.lower() or "token" in error_msg.lower() or error_code == 401:
                    logger.critical("🔑 Authentication failed - check RUBIKA_BOT_TOKEN!")
                    return None
                
                # Retry on transient API errors
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.warning(f"⏳ Retrying transient error in {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                
                return None
            
            return result
            
        except requests.exceptions.Timeout:
            logger.warning(f"⏰ {method} timeout on attempt {attempt+1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"🔌 {method} connection error: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None
        except Exception as e:
            logger.error(f"❌ {method} unexpected error: {type(e).__name__}: {e}", exc_info=True)
            return None
    
    logger.error(f"❌ {method} failed after {max_retries} attempts")
    return None
        
        result = response.json()
        logger.debug(f"✅ {method} response status: {result.get('status')}")
        return result
        
    except requests.exceptions.Timeout:
        logger.error(f"❌ {method} timeout after {timeout}s")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ {method} request error: {e}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"❌ {method} invalid JSON response: {e}")
        return None

def send_text(chat_id: str, text: str) -> tuple[bool, str]:
    """Send text message via Rubika Bot API with Markdown parsing."""
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
    
    error_msg = data.get("data", {}).get("error", "Unknown error")
    logger.error(f"❌ sendMessage failed for {chat_id}: {error_msg}")
    return False, None

def edit_text(chat_id: str, message_id: str, text: str) -> bool:
    """Edit existing message text via Rubika Bot API."""
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

def send_file(chat_id: str, file_id: str, caption: str) -> tuple[bool, str]:
    """Send file via Rubika Bot API."""
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
    
    logger.error(f"❌ sendFile failed for {chat_id}: {data}")
    return False, None

def upload_file(file_bytes: bytes, filename: str, file_type: str) -> str:
    """Upload file to Rubika and get file_id."""
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
# SIMPLE SUBSCRIBER DETECTION ALGORITHM (0-100)
# ─────────────────────────────────────────────
def process_rubika_updates_for_subscribers(
    existing_subscribers: set,
    api_response: dict
) -> tuple[set, list[str], str]:
    """
    SIMPLE ALGORITHM:
    1. Parse raw API response from getUpdates
    2. Extract ALL chat_ids from updates array
    3. Filter for "new user" events (StartedBot OR /start message)
    4. Compare with existing_subscribers set
    5. Return: (updated_subscribers, new_chat_ids, next_offset_id)
    
    This function does ONE thing and does it clearly.
    """
    new_chat_ids = []
    next_offset = api_response.get("data", {}).get("next_offset_id", "")
    
    # Get updates array - handle missing/malformed response gracefully
    updates = api_response.get("data", {}).get("updates", [])
    
    if not updates:
        logger.debug("ℹ️ No updates in API response")
        return existing_subscribers, new_chat_ids, next_offset
    
    logger.info(f"🔍 Processing {len(updates)} updates from Rubika API")
    
    # Iterate through EVERY update - no skipping, no complex filtering
    for idx, update in enumerate(updates):
        chat_id = update.get("chat_id")
        update_type = update.get("type", "")
        update_time = update.get("update_time", 0)
        
        # Skip if no chat_id - invalid update
        if not chat_id:
            logger.debug(f"⏭️ Update #{idx}: No chat_id, skipping")
            continue
        
        # Determine if this update indicates a NEW subscriber
        is_new_user_event = False
        
        # CASE 1: StartedBot event - user just started the bot
        if update_type == "StartedBot":
            logger.debug(f"📥 Update #{idx}: StartedBot event for chat_id={chat_id}")
            is_new_user_event = True
        
        # CASE 2: NewMessage with /start text or start button
        elif update_type == "NewMessage":
            new_msg = update.get("new_message", {}) or {}
            text = str(new_msg.get("text", "")).strip().lower()
            aux_data = new_msg.get("aux_data", {}) or {}
            button_id = str(aux_data.get("button_id", "")).strip().lower()
            
            if text == "/start" or button_id == "start":
                logger.debug(f"📥 Update #{idx}: /start or start button for chat_id={chat_id}")
                is_new_user_event = True
        
        # If this is a new user event AND chat_id not already known → NEW SUBSCRIBER
        if is_new_user_event and chat_id not in existing_subscribers:
            logger.info(f"🆕 NEW SUBSCRIBER DETECTED: chat_id={chat_id} (type={update_type}, time={update_time})")
            new_chat_ids.append(chat_id)
            existing_subscribers.add(chat_id)  # Add immediately to avoid duplicates in same batch
        elif is_new_user_event:
            logger.debug(f"✅ Known subscriber: chat_id={chat_id} (already in set)")
    
    logger.info(f"📊 Batch complete: {len(new_chat_ids)} new subscribers found, {len(existing_subscribers)} total")
    return existing_subscribers, new_chat_ids, next_offset


def handle_new_subscribers(new_chat_ids: list[str], subscribers: set) -> bool:
    """
    Handle newly detected subscribers:
    1. Send welcome message to each
    2. Save updated list to repo file
    3. Push to remote repo
    Returns True if any subscribers were added.
    """
    if not new_chat_ids:
        logger.debug("ℹ️ No new subscribers to handle")
        return False
    
    logger.info(f"🚀 Handling {len(new_chat_ids)} new subscriber(s)")
    success_count = 0
    
    for chat_id in new_chat_ids:
        try:
            logger.info(f"👤 Processing new subscriber: {chat_id}")
            
            # Send beautiful welcome message
            welcome_ok, welcome_msg_id = send_text(chat_id, build_welcome())
            
            if welcome_ok and welcome_msg_id:
                logger.info(f"✅ Welcome message sent to {chat_id} (msg_id: {welcome_msg_id})")
                success_count += 1
            else:
                logger.warning(f"⚠️ Failed to send welcome to {chat_id}, but keeping in subscriber list")
                # Still count as handled - we don't want to retry failed welcomes
                
        except Exception as e:
            logger.error(f"❌ Error handling subscriber {chat_id}: {e}", exc_info=True)
            continue
    
    # Save to local repo file
    logger.info(f"💾 Saving {len(subscribers)} subscribers to {SUBSCRIBERS_FILE}")
    save_subscribers(subscribers)
    
    # Push to remote repo immediately
    logger.info("🔄 Pushing updated subscribers to remote repo...")
    push_success = push_repo()
    
    if push_success:
        logger.info("✅ Repo push completed successfully")
    else:
        logger.warning("⚠️ Repo push had issues, but subscribers saved locally")
    
    logger.info(f"🎉 Subscriber handling complete: {success_count}/{len(new_chat_ids)} welcomes sent")
    return True


# ─────────────────────────────────────────────
# FETCH & PROCESS SUBSCRIBERS - WITH DEBUG LOGGING
# ─────────────────────────────────────────────
def fetch_and_process_subscribers(subscribers: set, state: dict) -> tuple[set, dict]:
    """
    Fetch updates from Rubika and process new subscribers.
    Now includes detailed debugging for API issues.
    """
    logger.info("🔄 Fetching updates from Rubika getUpdates...")
    
    # Build request payload - SIMPLE, no offset for subscriber detection
    payload = {
        "limit": 200,
        "state": "all",
    }
    
    # Make API call with retry logic
    api_response = rubika_post("getUpdates", payload, timeout=40, max_retries=3)
    
    # Handle API errors with detailed logging
    if not api_response:
        logger.error("❌ getUpdates API call failed completely - check token, network, or API status")
        # Return unchanged - don't crash the bot
        return subscribers, state
    
    # Log the full response structure for debugging
    logger.debug(f"🔍 Full API response keys: {list(api_response.keys())}")
    if "data" in api_response:
        logger.debug(f"🔍 Data keys: {list(api_response.get('data', {}).keys())}")
    
    # Process updates with our simple algorithm
    updated_subscribers, new_chat_ids, next_offset = process_rubika_updates_for_subscribers(
        existing_subscribers=subscribers,
        api_response=api_response
    )
    
    # Handle any new subscribers found
    if new_chat_ids:
        logger.info(f"🎯 Found {len(new_chat_ids)} new subscriber(s) to process")
        handle_new_subscribers(new_chat_ids, updated_subscribers)
    else:
        logger.debug("ℹ️ No new subscribers in this batch")
    
    # Update state with next_offset for general tracking
    if next_offset:
        state["rubika_offset"] = next_offset
        save_state(state)
        logger.debug(f"📍 Saved next_offset_id: {next_offset}")
    
    logger.info("✅ fetch_and_process_subscribers completed")
    return updated_subscribers, state

# Alias for backward compatibility
def fetch_updates(subscribers, state):
    """Wrapper that calls the new simple subscriber processing logic."""
    return fetch_and_process_subscribers(subscribers, state)

# ─────────────────────────────────────────────
# MEDIA HANDLING
# ─────────────────────────────────────────────
def get_file_type(media) -> str:
    """Determine file type for Rubika upload."""
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
# BROADCAST FUNCTIONS
# ─────────────────────────────────────────────
def broadcast_text(subscribers: set, text: str) -> list:
    """Broadcast text message to all subscribers."""
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

def broadcast_file(subscribers: set, file_id: str, caption: str) -> list:
    """Broadcast file to all subscribers."""
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
# REACTION UPDATES
# ─────────────────────────────────────────────
pending_edits = {}

async def delayed_reaction_updates(client, channel_name: str, tg_msg_id: int):
    """Periodically update messages with reaction counts."""
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
# MESSAGE FORWARDING
# ─────────────────────────────────────────────
async def forward_message(client, message, channel_name: str, state: dict, subscribers: set):
    """Forward Telegram message to Rubika subscribers with proper formatting."""
    
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
def clone_repo() -> bool:
    """Clone the private data repo with PAT authentication."""
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
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Git clone failed: {e.stderr}")
        return False
    finally:
        helper.unlink(missing_ok=True)

def push_repo() -> bool:
    """Push changes to the private data repo."""
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
            return True
        else:
            logger.debug("ℹ️ No changes to push")
            return True
            
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Git push failed: {e.stderr}")
        return False
    except Exception as e:
        logger.error(f"❌ Push error: {e}")
        return False

# ─────────────────────────────────────────────
# CATCHUP ON MISSED MESSAGES
# ─────────────────────────────────────────────
async def catchup(client, channels: list, state: dict, subscribers: set):
    """Catch up on missed messages from Telegram channels."""
    
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
# MAIN ENTRY POINT
# ─────────────────────────────────────────────
async def main():
    logger.info("🚀 Starting Public Forwarder...")
    
    # Initialize repo and load data
    if not clone_repo():
        logger.error("❌ Failed to clone repo, exiting")
        sys.exit(1)
    
    channels = load_channels()
    state = load_state()
    subscribers = load_subscribers()
    
    logger.info(f"📊 Status: {len(channels)} channels, {len(subscribers)} subscribers")
    
    # Initial poll for new subscribers (immediate) - uses simple algorithm
    logger.info("🔄 Running initial subscriber poll...")
    subscribers, state = fetch_and_process_subscribers(subscribers, state)
    
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
            logger.info(f"🔄 Poll #{poll_count}: Starting subscriber check...")
            
            try:
                subscribers, state = fetch_and_process_subscribers(subscribers, state)
                logger.info(f"✅ Poll #{poll_count} complete - {len(subscribers)} total subscribers")
            except Exception as e:
                logger.error(f"❌ Poll #{poll_count} CRASHED: {e}", exc_info=True)
                await asyncio.sleep(10)  # Brief pause before retry
            
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
