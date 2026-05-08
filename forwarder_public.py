import os
import json
import time
import asyncio
import logging
import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ---------- Configuration ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

# Private data repo URL (without token) – token is provided via secret
DATA_REPO_URL = os.environ.get("DATA_REPO_URL", "")
DATA_REPO_DIR = Path("data_repo")

CHANNELS_FILE = Path("channels.json")
RUN_DURATION = 20400          # 5h 40m

MAX_FILE_SIZE_MB = {
    "Image": 10, "Video": 50, "File": 50, "Music": 50, "Voice": 10, "Gif": 50,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------- Reaction helpers ----------
def get_top_reactions(message) -> str:
    if not message.reactions or not message.reactions.results:
        return ""
    counts = []
    for r in message.reactions.results:
        emoji = r.reaction.emoticon if hasattr(r.reaction, 'emoticon') else str(r.reaction)
        counts.append((emoji, r.count))
    counts.sort(key=lambda x: x[1], reverse=True)
    top = counts[:3]
    return " ".join(f"{emoji}{count}" for emoji, count in top)


# ---------- Data file helpers (using local copy within data_repo) ----------
_state_file = DATA_REPO_DIR / "state.json"
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
        json.dump(list(subscribers), f, indent=2)


# ---------- Rubika API (unchanged) ----------
def _rubika_post(endpoint: str, payload: dict) -> dict | None:
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error(f"Rubika {endpoint} HTTP {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        logger.error(f"Rubika {endpoint} exception: {e}")
        return None

def _extract_field(data: dict, *paths: str) -> str | None:
    for path in paths:
        parts = path.split(".")
        cur = data
        for part in parts:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        if cur is not None:
            return str(cur)
    return None

def _rubika_file_type(telegram_media) -> str:
    if hasattr(telegram_media, 'photo') and telegram_media.photo:
        return "Image"
    if hasattr(telegram_media, 'video') and telegram_media.video:
        return "Video"
    if hasattr(telegram_media, 'voice') and telegram_media.voice:
        return "Voice"
    if hasattr(telegram_media, 'audio') and telegram_media.audio:
        return "Music"
    if hasattr(telegram_media, 'document') and telegram_media.document:
        mime = getattr(telegram_media.document, 'mime_type', '')
        if mime == "video/mp4" and getattr(telegram_media, 'gif', False):
            return "Gif"
        return "File"
    return "File"

def upload_to_rubika(file_bytes: bytes, filename: str, file_type: str) -> str | None:
    # Step 1 – requestSendFile
    req = _rubika_post("requestSendFile", {"type": file_type})
    if not req:
        return None
    upload_url = _extract_field(req, "data.upload_url", "upload_url", "result.upload_url")
    if not upload_url:
        logger.error(f"requestSendFile no upload_url: {req}")
        return None

    # Step 2 – upload file to storage
    try:
        resp = requests.post(upload_url, files={"file": (filename, file_bytes)}, timeout=60)
        if resp.status_code != 200:
            logger.error(f"Upload to storage failed {resp.status_code}: {resp.text}")
            return None
        data = resp.json()
        file_id = _extract_field(data, "data.file_id", "file_id", "result.file_id")
        if file_id:
            logger.info(f"Uploaded {filename}, file_id={file_id}")
            return file_id
        else:
            logger.error(f"Upload response missing file_id: {data}")
            return None
    except Exception as e:
        logger.error(f"Upload exception: {e}")
        return None


# ---------- Sending helpers ----------
def _build_header(channel_name: str, msg_date: datetime) -> str:
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    return f"=============\n{channel_name}\n{date_str}\n============="

def send_text_to_rubika(chat_id: str, text: str) -> tuple[bool, str | None]:
    data = _rubika_post("sendMessage", {"chat_id": chat_id, "text": text})
    if not data:
        return False, None
    if data.get("status") == "OK" or data.get("ok"):
        msg_id = _extract_field(data, "data.message_id", "message_id", "result.message_id")
        return True, msg_id
    logger.error(f"sendMessage failed for {chat_id}: {data}")
    return False, None

def send_file_to_rubika(chat_id: str, file_id: str, caption: str) -> tuple[bool, str | None]:
    data = _rubika_post("sendFile", {
        "chat_id": chat_id,
        "file_id": file_id,
        "text": caption,
    })
    if not data:
        return False, None
    if data.get("status") == "OK" or data.get("ok"):
        msg_id = _extract_field(data, "data.message_id", "message_id", "result.message_id")
        return True, msg_id
    logger.error(f"sendFile failed for {chat_id}: {data}")
    return False, None

def edit_text_in_rubika(chat_id: str, message_id: str, new_text: str) -> bool:
    data = _rubika_post("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
    })
    return data is not None and (data.get("status") == "OK" or data.get("ok"))


# ---------- Git data sync ----------
def git_clone_data_repo(token: str, repo_url: str):
    if DATA_REPO_DIR.exists():
        shutil.rmtree(DATA_REPO_DIR)

    os.environ["GIT_ASKPASS"] = "/dev/null"
    helper_script = Path("/tmp/git-cred-helper.sh")
    helper_script.write_text(f"#!/bin/sh\necho 'username=x-access-token'\necho 'password={token}'\n")
    os.chmod(helper_script, 0o755)

    try:
        subprocess.run(
            [
                "git",
                "-c", f"credential.helper={helper_script}",
                "clone", "--depth", "1",
                repo_url,
                str(DATA_REPO_DIR),
            ],
            check=True, capture_output=True, text=True,
        )
        logger.info("Data repo cloned successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Clone failed: {e.stderr}")
        raise
    finally:
        helper_script.unlink(missing_ok=True)

def git_push_data_repo():
    """Commit and push local changes back to the private repo."""
    os.chdir(DATA_REPO_DIR)
    try:
        subprocess.run(["git", "config", "user.email", "actions@github.com"], check=True)
        subprocess.run(["git", "config", "user.name", "GitHub Actions"], check=True)
        subprocess.run(["git", "add", "state.json", "subscribers.json"], check=True)
        # Check if there are changes to commit
        diff = subprocess.run(["git", "diff", "--staged", "--quiet"], capture_output=True)
        if diff.returncode != 0:
            subprocess.run(["git", "commit", "-m", "update data"], check=True)
            subprocess.run(["git", "push"], check=True)
            logger.info("Data pushed to private repo.")
        else:
            logger.info("No data changes to push.")
    except subprocess.CalledProcessError as e:
        logger.error(f"Git push failed: {e}")
    finally:
        os.chdir("..")


# ---------- Subscriber management ----------
def fetch_new_subscribers(known_subscribers: set) -> set:
    """Get recent updates and add any chat_id that sent a message."""
    data = _rubika_post("getUpdates", {"limit": 200})
    if not data:
        return known_subscribers

    updates = data.get("data", {}).get("updates", [])
    new_chats = set()
    for update in updates:
        if update.get("type") == "NewMessage":
            chat_id = update.get("chat_id") or update.get("new_message", {}).get("chat_id")
            if chat_id:
                new_chats.add(chat_id)

    all_subscribers = known_subscribers | new_chats
    if all_subscribers != known_subscribers:
        logger.info(f"Found {len(all_subscribers) - len(known_subscribers)} new subscriber(s). Total: {len(all_subscribers)}")
        save_subscribers(all_subscribers)
    return all_subscribers


# ---------- Delayed reaction edits ----------
pending_edits: dict[tuple[str, int], list[dict]] = {}

async def delayed_reaction_updates(client: TelegramClient, channel_name: str, tg_msg_id: int):
    key = (channel_name, tg_msg_id)
    entries = pending_edits.get(key)
    if not entries:
        return

    await asyncio.sleep(300)   # +5 min
    await _apply_reaction_edit(client, channel_name, tg_msg_id, entries, "5 min")

    await asyncio.sleep(600)   # +10 min (total 15 min)
    await _apply_reaction_edit(client, channel_name, tg_msg_id, entries, "15 min")

    pending_edits.pop(key, None)

async def _apply_reaction_edit(client, channel_name, tg_msg_id, entries, label):
    try:
        msg = await client.get_messages(channel_name, ids=tg_msg_id)
        if not msg:
            return
        reaction_str = get_top_reactions(msg)
        if not reaction_str:
            logger.info(f"{label} edit: no reactions yet for {tg_msg_id}")
            return
        reaction_line = f"\n{reaction_str}"
        for entry in entries:
            new_text = entry["full_original_text"] + reaction_line
            if edit_text_in_rubika(entry["chat_id"], entry["rubika_msg_id"], new_text):
                logger.info(f"✅ {label} edit: msg {entry['rubika_msg_id']} updated")
            else:
                logger.error(f"❌ {label} edit failed for {entry['rubika_msg_id']}")
    except Exception as e:
        logger.error(f"Error during {label} edit for {tg_msg_id}: {e}")


# ---------- Core forwarding ----------
async def forward_message(client, message, channel_name, state, subscribers: set, skip_dup=False):
    msg_date = message.date

    if not skip_dup:
        last_id = state.get(channel_name, 0)
        if message.id <= last_id:
            return

    # ---------- TEXT ----------
    if message.text and not message.media:
        header = _build_header(channel_name, msg_date)
        full_text = header + "\n\n" + message.text.replace('`', '')

        key = (channel_name, message.id)
        pending_edits[key] = []
        all_ok = True
        for chat_id in subscribers:
            ok, rubika_id = send_text_to_rubika(chat_id, full_text)
            if ok and rubika_id:
                pending_edits[key].append({
                    "chat_id": chat_id,
                    "rubika_msg_id": rubika_id,
                    "full_original_text": full_text,
                })
            else:
                all_ok = False
        if all_ok:
            state[channel_name] = message.id
            save_state(state)   # save to local copy
            asyncio.ensure_future(delayed_reaction_updates(client, channel_name, message.id))
        return

    # ---------- MEDIA ----------
    if not message.media:
        return

    if not message.file or not message.file.size:
        logger.warning(f"Msg {message.id} no file size, skipping")
        state[channel_name] = message.id
        save_state(state)
        return

    file_type = _rubika_file_type(message.media)
    max_mb = MAX_FILE_SIZE_MB.get(file_type, 50)
    if message.file.size > max_mb * 1024 * 1024:
        size_mb = message.file.size / (1024 * 1024)
        skip_msg = f"⚠️ Large {file_type} ({size_mb:.1f} MB) skipped"
        for chat_id in subscribers:
            send_text_to_rubika(chat_id, skip_msg)
        state[channel_name] = message.id
        save_state(state)
        return

    if file_type == "Image":
        filename = "photo.jpg"
    elif file_type == "Voice":
        filename = "voice.ogg"
    elif file_type == "Music":
        filename = message.file.name or "audio.mp3"
    elif file_type == "Video":
        filename = message.file.name or "video.mp4"
    else:
        filename = message.file.name or "file"

    try:
        file_bytes = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {file_type} ({len(file_bytes)} B) from {channel_name}")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return

    file_id = upload_to_rubika(file_bytes, filename, file_type)
    if not file_id:
        logger.error("Failed to upload to Rubika, skipping media")
        return

    header = _build_header(channel_name, msg_date)
    caption_text = message.text or ""
    full_caption = f"{header}\n\n{caption_text.replace('`', '')}" if caption_text else header

    key = (channel_name, message.id)
    pending_edits[key] = []
    all_ok = True
    for chat_id in subscribers:
        ok, rubika_id = send_file_to_rubika(chat_id, file_id, full_caption)
        if ok and rubika_id:
            pending_edits[key].append({
                "chat_id": chat_id,
                "rubika_msg_id": rubika_id,
                "full_original_text": full_caption,
            })
        else:
            all_ok = False

    if all_ok:
        state[channel_name] = message.id
        save_state(state)
        asyncio.ensure_future(delayed_reaction_updates(client, channel_name, message.id))


# ---------- Startup ----------
async def catch_up(client, channels, state, subscribers):
    if not state:
        logger.info("First run – initialising state without forwarding old messages")
        for channel in channels:
            try:
                msgs = await client.get_messages(channel, limit=1)
                state[channel] = msgs[0].id if msgs and msgs[0] else 0
                logger.info(f"Start marker for {channel} at msg {state[channel]}")
            except Exception as e:
                logger.error(f"Failed to init {channel}: {e}")
        save_state(state)
        return

    logger.info("Checking for missed messages…")
    for channel in channels:
        try:
            msgs = await client.get_messages(channel, limit=10)
            if not msgs:
                continue
            for msg in reversed(msgs):
                if msg.id <= state.get(channel, 0):
                    continue
                if not msg.text and not msg.media:
                    continue
                logger.info(f"Missed msg {msg.id} from {channel}")
                await forward_message(client, msg, channel, state, subscribers)
        except Exception as e:
            logger.error(f"Error catching up {channel}: {e}")


# ---------- Main ----------
async def main():
    # Required env vars
    if not all([API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN]):
        logger.error("Missing required environment variables!")
        sys.exit(1)

    # Clone the private data repo
    token = os.environ.get("DATA_REPO_PAT")
    repo_url = os.environ.get("DATA_REPO_URL")
    if not token or not repo_url:
        logger.error("DATA_REPO_PAT and DATA_REPO_URL secrets must be set!")
        sys.exit(1)

    git_clone_data_repo(token, repo_url)

    channels = load_channels()
    logger.info(f"Monitoring channels: {channels}")

    # Load subscribers from cloned repo
    subscribers = load_subscribers()
    # Refresh subscribers from getUpdates
    subscribers = fetch_new_subscribers(subscribers)
    logger.info(f"Total subscribers: {len(subscribers)}")

    if not subscribers:
        logger.error("No subscribers yet! At least one user must /start the bot.")
        # still continue, the list may grow later

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("Telegram client ready")

    state = load_state()
    await catch_up(client, channels, state, subscribers)

    # Live handler
    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            chat = await event.get_chat()
            await forward_message(client, event.message, chat.title, state, subscribers)
            # Save state after each forward to prevent data loss
            save_state(state)
        except Exception as e:
            logger.error(f"Handler error: {e}")

    # Background task: refresh subscriber list every 5 minutes & autosave state
    async def refresh_subscribers_periodic():
        while True:
            await asyncio.sleep(300)
            nonlocal_subscribers = subscribers
            new_set = fetch_new_subscribers(subscribers)
            subscribers.clear()
            subscribers.update(new_set)
            save_subscribers(subscribers)

    asyncio.ensure_future(refresh_subscribers_periodic())

    logger.info("Now forwarding messages in real‑time (to all subscribers)…")
    start = time.time()

    while True:
        if time.time() - start >= RUN_DURATION:
            logger.info(f"Time limit ({RUN_DURATION/3600:.2f}h) reached, exiting.")
            break
        await asyncio.sleep(30)

    # Before exiting, push updated data to private repo
    save_state(state)
    save_subscribers(subscribers)
    git_push_data_repo()

    await client.disconnect()
    logger.info("Session closed.")


if __name__ == "__main__":
    asyncio.run(main())
