import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters


load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

VIDEO_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
ALLOWED_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com", "instagram.com", "www.instagram.com", "m.instagram.com", "x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com", "facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.watch", "threads.net", "www.threads.net", "threads.com", "www.threads.com"}
MAX_FILE_BYTES = 49 * 1024 * 1024
DOWNLOAD_SLOTS = asyncio.Semaphore(2)
# Place this database on a persistent volume when hosting on an ephemeral server.
DB_PATH = Path(os.getenv("BOT_DB_PATH", "bot_users.sqlite3"))


def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=20)
    connection.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL)")
    return connection


def record_user(user_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with open_db() as db:
        db.execute(
            "INSERT INTO users (user_id, first_seen, last_seen) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
            (user_id, now, now),
        )


def get_user_counts() -> tuple[int, int]:
    since = datetime.now(timezone.utc).timestamp() - 30 * 24 * 3600
    cutoff = datetime.fromtimestamp(since, timezone.utc).isoformat()
    with open_db() as db:
        return db.execute(
            "SELECT COUNT(*), COUNT(CASE WHEN last_seen >= ? THEN 1 END) FROM users",
            (cutoff,),
        ).fetchone()


async def remember_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_user and update.effective_chat and update.effective_chat.type == "private":
        try:
            await asyncio.to_thread(record_user, update.effective_user.id)
        except Exception:
            logger.exception("Could not record user")


async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.effective_chat and update.effective_chat.type == "private" and update.effective_user and update.message:
        await update.message.reply_text(f"Ø±ÙÙ Ø­Ø³Ø§Ø¨Ù: {update.effective_user.id}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return
    admin_id = os.getenv("ADMIN_USER_ID", "").strip()
    if not admin_id or str(update.effective_user.id) != admin_id:
        await update.message.reply_text("ÙØ°Ø§ Ø§ÙØ£ÙØ± ÙØªØ§Ø­ ÙØµØ§Ø­Ø¨ Ø§ÙØ¨ÙØª ÙÙØ·. ÙÙØ¹Ø±ÙØ© Ø±ÙÙ Ø­Ø³Ø§Ø¨Ù Ø£Ø±Ø³Ù /id.")
        return
    try:
        total, recent = await asyncio.to_thread(get_user_counts)
    except Exception:
        logger.exception("Could not read statistics")
        await update.message.reply_text("ØªØ¹Ø°Ø± ÙØ±Ø§Ø¡Ø© Ø§ÙØ¥Ø­ØµØ§Ø¡Ø§Øª Ø­Ø§ÙÙÙØ§.")
        return
    await update.message.reply_text(
        f"Ø¹Ø¯Ø¯ Ø§ÙØ£Ø´Ø®Ø§Øµ Ø§ÙØ°ÙÙ Ø§Ø³ØªØ®Ø¯ÙÙØ§ Ø§ÙØ¨ÙØª ÙÙØ° Ø¥Ø¶Ø§ÙØ© Ø§ÙØ¹Ø¯ÙØ§Ø¯: {total}\n"
        f"Ø¹Ø¯Ø¯ ÙÙ Ø§Ø³ØªØ®Ø¯ÙÙÙ Ø®ÙØ§Ù Ø¢Ø®Ø± 30 ÙÙÙÙØ§: {recent}"
    )


def extract_video_url(text: str) -> str | None:
    match = VIDEO_URL_RE.search(text or "")
    if not match:
        return None

    url = match.group(0).rstrip(".,ØØ!Ø)]}")
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS and not host.endswith(".tiktok.com"):
        return None
    if host.endswith("instagram.com") and not re.match(r"^/(reel|reels|p|tv)/[^/?#]+", urlparse(url).path, re.IGNORECASE):
        return None
    if host in {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} and not re.match(r"^/(?:[^/]+/status|i/status)/\d+/?$", urlparse(url).path, re.IGNORECASE):
        return None
    if host in {"threads.net", "www.threads.net", "threads.com", "www.threads.com"} and not re.match(r"^/(?:@[^/]+/post/[^/?#]+|share/[^/?#]+)/?$", urlparse(url).path, re.IGNORECASE):
        return None
    return url


def download_video(url: str, folder: str) -> Path:
    output_template = str(Path(folder) / "%(id)s.%(ext)s")
    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "retries": 2,
        "max_filesize": MAX_FILE_BYTES,
    }

    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        path = Path(downloader.prepare_filename(info))

    if not path.exists():
        candidates = [p for p in Path(folder).iterdir() if p.is_file()]
        if not candidates:
            raise FileNotFoundError("ÙÙ ÙØªÙ Ø¥ÙØ´Ø§Ø¡ ÙÙÙ Ø§ÙÙÙØ¯ÙÙ.")
        path = max(candidates, key=lambda item: item.stat().st_mtime)

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Ø§ÙÙÙØ¯ÙÙ Ø£ÙØ¨Ø± ÙÙ Ø§ÙØ­Ø¯ Ø§ÙØ°Ù ÙØ³ØªØ·ÙØ¹ Ø§ÙØ¨ÙØª Ø¥Ø±Ø³Ø§ÙÙ.")
    return path


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message:
        await update.message.reply_text(
            "Ø£Ø±Ø³Ù Ø±Ø§Ø¨Ø· ÙÙØ¯ÙÙ Ø¹Ø§Ù ÙÙ TikTok Ø£Ù Instagram Ø£Ù X Ø£Ù Facebook Ø£Ù Threads ÙØ³Ø£Ø­Ø§ÙÙ ØªÙØ²ÙÙÙ.\n\n"
            "Ø§Ø³ØªØ®Ø¯Ù Ø§ÙØ¨ÙØª ÙÙØ· ÙÙÙÙØ¯ÙÙÙØ§Øª Ø§ÙØªÙ ØªÙÙÙÙØ§ Ø£Ù ÙØ¯ÙÙ Ø¥Ø°Ù Ø¨ØªÙØ²ÙÙÙØ§."
        )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.message.text:
        return

    url = extract_video_url(update.message.text)
    if not url:
        await update.message.reply_text("Ø£Ø±Ø³Ù Ø±Ø§Ø¨Ø· ÙÙØ¯ÙÙ Ø¹Ø§Ù ÙÙ TikTok Ø£Ù Instagram Ø£Ù X Ø£Ù Facebook Ø£Ù Threads ÙØ¨Ø¯Ø£ Ø¨Ù https://")
        return

    status = await update.message.reply_text("Ø¬Ø§Ø±Ù ØªØ¬ÙÙØ² Ø§ÙÙÙØ¯ÙÙâ¦")
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    try:
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="video_bot_") as temp_dir:
                video_path = await asyncio.to_thread(download_video, url, temp_dir)
                with video_path.open("rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption="ØªÙ ØªÙØ²ÙÙ Ø§ÙÙÙØ¯ÙÙ ÙÙØ§Ø³ØªØ®Ø¯Ø§Ù Ø§ÙÙØµØ±ÙØ­ Ø¨Ù ÙÙØ·.",
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                    )
        await status.delete()
    except yt_dlp.utils.DownloadError:
        logger.exception("Video download failed")
        await status.edit_text(
            "ØªØ¹Ø°ÙØ± ØªÙØ²ÙÙ Ø§ÙÙÙØ¯ÙÙ. ØªØ£ÙØ¯ Ø£Ù Ø§ÙØ±Ø§Ø¨Ø· Ø¹Ø§Ù ÙÙØ­ØªÙÙ ÙÙØ¯ÙÙ. Ø¨Ø¹Ø¶ Ø±ÙØ§Ø¨Ø· Instagram ÙX ÙFacebook ÙThreads ØªØªØ·ÙØ¨ ØªØ³Ø¬ÙÙ Ø¯Ø®ÙÙ ÙÙØ§ ÙØ³ØªØ·ÙØ¹ Ø§ÙØ¨ÙØª ØªÙØ²ÙÙÙØ§."
        )
    except ValueError as exc:
        await status.edit_text(str(exc))
    except Exception:
        logger.exception("Unexpected error")
        await status.edit_text("Ø­Ø¯Ø« Ø®Ø·Ø£ ØºÙØ± ÙØªÙÙØ¹. Ø­Ø§ÙÙ ÙØ±Ø© Ø£Ø®Ø±Ù ÙØ§Ø­ÙÙØ§.")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Ø¶Ø¹ TELEGRAM_BOT_TOKEN Ø¯Ø§Ø®Ù ÙÙÙ .env")

    application = Application.builder().token(token).build()
    # The first group records private users for both commands and ordinary messages.
    application.add_handler(MessageHandler(filters.ALL, remember_user), group=-1)
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
