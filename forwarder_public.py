import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import git
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ===================== CONFIG =====================
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]
DATA_REPO_PAT = os.environ["DATA_REPO_PAT"]
DATA_REPO_URL = os.environ["DATA_REPO_URL"]
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "").strip()

RUN_DURATION = int(os.environ.get("RUN_DURATION", 20400))  # ~5h 40m

DATA_DIR = Path("data_repo")
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
CHANNELS_FILE = Path("channels.json")

# ===================== LOGGING =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# ===================== RUBIKA API HELPER =====================
RUBIKA_BASE = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}"

async def rubika_request(method: str, payload: dict = None, retries: int = 3):
    if payload is None:
        payload = {}
    url = f"{RUBIKA_BASE}/{method}"

    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=40) as resp:
                    try:
                        data = await resp.json()
                    except:
                        data = {"ok": False, "description": await resp.text()}

                    if not data.get("ok"):
                        logger.warning(f"Rubika {method} failed: {data.get('description')}")
                    return data
        except Exception as e:
            if attempt == retries - 1:
                logger.error(f"Rubika {method} failed after {retries} attempts: {e}")
                return {"ok": False, "description": str(e)}
            await asyncio.sleep(2 ** attempt * 1.5)
    return {"ok": False, "description": "Max retries exceeded"}


# ===================== MESSAGE FORMATTING =====================
def is_proxy_line(line: str) -> bool:
    stripped = line.strip().lower()
    schemes = ("vmess://", "vless://", "ss://", "trojan://", "socks5://", "http://", "https://")
    ip_pattern = re.compile(r'^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?$')
    return any(stripped.startswith(s) for s in schemes) or bool(ip_pattern.match(stripped))


def format_message_for_rubika(text: str, channel_name: str):
    lines = text.splitlines()
    formatted_parts = []
    meta_data_parts = []
    current_index = 0

    # Header
    header = f"**{channel_name}**\n\n"
    formatted_parts.append(header)
    current_index += len(header)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    ts = f"_{timestamp}_\n\n"
    formatted_parts.append(ts)
    current_index += len(ts)

    for line in lines:
        stripped = line.strip()
        if not stripped and not formatted_parts[-1].endswith("\n\n"):
            formatted_parts.append("\n")
            current_index += 1
            continue

        if is_proxy_line(line):
            proxy_text = f"`{stripped}`\n"
            start = current_index
            formatted_parts.append(proxy_text)
            
            meta_data_parts.append({"type": "Monospace", "from_index": start, "length": len(stripped)})
            meta_data_parts.append({"type": "Blockquote", "from_index": start, "length": len(proxy_text.strip())})
            current_index += len(proxy_text)
        else:
            formatted_parts.append(line + "\n")
            current_index += len(line) + 1

    full_text = "".join(formatted_parts).strip()
    metadata = {"meta_data_parts": meta_data_parts} if meta_data_parts else None
    return full_text, metadata


# ===================== GIT OPERATIONS =====================
def setup_data_repository():
    if DATA_DIR.exists():
        repo = git.Repo(DATA_DIR)
        repo.remotes.origin.pull()
        logger.info("✅ Data repository pulled")
    else:
        auth_url = DATA_REPO_URL.replace("https://", f"https://{DATA_REPO_PAT}@")
        git.Repo.clone_from(auth_url, DATA_DIR)
        logger.info("✅ Data repository cloned")


def save_and_push_subscribers(subscribers: list):
    SUBSCRIBERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(subscribers, f, ensure_ascii=False, indent=2)

    try:
        repo = git.Repo(DATA_DIR)
        repo.index.add([SUBSCRIBERS_FILE.name])
        repo.index.commit(f"Update subscribers - {len(subscribers)} users")
        repo.remote().push()
        logger.info(f"✅ Subscribers pushed ({len(subscribers)} total)")
    except Exception as e:
        logger.error(f"Git push failed: {e}")


# ===================== MAIN =====================
async def main():
    setup_data_repository()

    # Load subscribers
    if SUBSCRIBERS_FILE.exists():
        with open(SUBSCRIBERS_FILE, encoding="utf-8") as f:
            subscribers = json.load(f)
    else:
        subscribers = []

    sub_set = set(str(s) for s in subscribers)  # ensure string
    logger.info(f"Loaded {len(sub_set)} subscribers")

    # Load channels
    with open(CHANNELS_FILE, encoding="utf-8") as f:
        channels = json.load(f)
    logger.info(f"Monitoring {len(channels)} Telegram channels")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)

    # Test Rubika Bot
    test = await rubika_request("getMe")
    if test.get("ok"):
        logger.info("✅ Rubika Bot connected successfully")
    else:
        logger.error("❌ Rubika Bot token seems invalid!")

    @client.on(events.NewMessage(chats=channels))
    async def new_message_handler(event):
        if not sub_set:
            return

        chat = await event.get_chat()
        channel_name = getattr(chat, 'title', None) or getattr(chat, 'username', 'Unknown Channel')

        raw_text = event.message.message or ""
        formatted_text, metadata = format_message_for_rubika(raw_text, channel_name)

        for chat_id in list(sub_set):
            try:
                await rubika_request("sendMessage", {
                    "chat_id": chat_id,
                    "text": formatted_text,
                    "metadata": metadata
                })
            except Exception as e:
                logger.error(f"Failed to forward to {chat_id}: {e}")

    # Subscriber Polling
    async def poll_new_subscribers():
        nonlocal sub_set
        offset_id = None

        while True:
            try:
                payload = {"offset_id": offset_id} if offset_id else {}
                data = await rubika_request("getUpdates", payload)

                if data.get("ok") and isinstance(data.get("result"), list):
                    for update in data["result"]:
                        if "update_id" in update:
                            offset_id = update.get("update_id")

                        if "update" in update and "new_message" in update["update"]:
                            msg = update["update"]["new_message"]
                            chat_id = str(msg.get("chat_id"))
                            text = str(msg.get("text", "")).strip().lower()

                            if text == "/start" or update["update"].get("action") == "StartedBot":
                                if chat_id not in sub_set:
                                    sub_set.add(chat_id)
                                    save_and_push_subscribers(list(sub_set))

                                    welcome = (
                                        "**✅ خوش آمدید!**\n\n"
                                        "این ربات پیام‌های کانال‌های پروکسی و VPN را به‌صورت خودکار برای شما فوروارد می‌کند.\n\n"
                                        "لینک‌ها به صورت آماده برای کپی نمایش داده می‌شوند."
                                    )
                                    await rubika_request("sendMessage", {"chat_id": chat_id, "text": welcome})
                                    logger.info(f"✅ New subscriber added: {chat_id}")
            except Exception as e:
                logger.error(f"Polling error: {e}")

            await asyncio.sleep(45)

    async with client:
        logger.info("🚀 Telegram client started successfully")

        # Start tasks
        tasks = [
            asyncio.create_task(poll_new_subscribers()),
        ]

        try:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=RUN_DURATION)
        except asyncio.TimeoutError:
            logger.info("⏰ Runtime limit reached. Shutting down...")
        finally:
            save_and_push_subscribers(list(sub_set))
            await client.disconnect()
            logger.info("👋 Bot stopped gracefully.")


if __name__ == "__main__":
    asyncio.run(main())
