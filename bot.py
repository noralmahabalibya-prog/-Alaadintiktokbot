import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from yt_dlp_plugins.extractor.threads import ThreadsIE

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
logging.getLogger("httpx").setLevel(logging.WARNING)
BUILD_VERSION = "2026-09-26.1"

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
        await update.message.reply_text(f"\u0631\u0642\u0645 \u062d\u0633\u0627\u0628\u0643: {update.effective_user.id}")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
        return
    admin_id = os.getenv("ADMIN_USER_ID", "").strip()
    if not admin_id or str(update.effective_user.id) != admin_id:
        await update.message.reply_text("\u0647\u0630\u0627 \u0627\u0644\u0623\u0645\u0631 \u0645\u062a\u0627\u062d \u0644\u0635\u0627\u062d\u0628 \u0627\u0644\u0628\u0648\u062a \u0641\u0642\u0637. \u0644\u0645\u0639\u0631\u0641\u0629 \u0631\u0642\u0645 \u062d\u0633\u0627\u0628\u0643 \u0623\u0631\u0633\u0644 /id.")
        return
    try:
        total, recent = await asyncio.to_thread(get_user_counts)
    except Exception:
        logger.exception("Could not read statistics")
        await update.message.reply_text("\u062a\u0639\u0630\u0631 \u0642\u0631\u0627\u0621\u0629 \u0627\u0644\u0625\u062d\u0635\u0627\u0621\u0627\u062a \u062d\u0627\u0644\u064a\u064b\u0627.")
        return
    await update.message.reply_text(
        f"\u0639\u062f\u062f \u0627\u0644\u0623\u0634\u062e\u0627\u0635 \u0627\u0644\u0630\u064a\u0646 \u0627\u0633\u062a\u062e\u062f\u0645\u0648\u0627 \u0627\u0644\u0628\u0648\u062a \u0645\u0646\u0630 \u0625\u0636\u0627\u0641\u0629 \u0627\u0644\u0639\u062f\u0651\u0627\u062f: {total}\n"
        f"\u0639\u062f\u062f \u0645\u0646 \u0627\u0633\u062a\u062e\u062f\u0645\u0648\u0647 \u062e\u0644\u0627\u0644 \u0622\u062e\u0631 30 \u064a\u0648\u0645\u064b\u0627: {recent}"
    )


def extract_video_url(text: str) -> str | None:
    match = VIDEO_URL_RE.search(text or "")
    if not match:
        return None

    url = match.group(0).rstrip(".,\u061b\u060c!\u061f)]}")
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS and not host.endswith(".tiktok.com"):
        return None
    if host.endswith("instagram.com") and not re.match(r"^/(reel|reels|p|tv)/[^/?#]+", urlparse(url).path, re.IGNORECASE):
        return None
    if host in {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} and not re.match(r"^/(?:[^/]+/status|i/status)/\d+/?$", urlparse(url).path, re.IGNORECASE):
        return None
    if host in {"threads.net", "www.threads.net", "threads.com", "www.threads.com"} and not re.match(r"^/(?:@[^/]+/post/[^/?#]+|t/[^/?#]+|share/[^/?#]+)/?$", urlparse(url).path, re.IGNORECASE):
        return None
    return url


def download_video(url: str, folder: str) -> Path:
    output_template = str(Path(folder) / "%(id)s.%(ext)s")
    options = {
        "format": "best[ext=mp4]/best",
        "playlist_items": "1",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": True,
        "socket_timeout": 20,
        "retries": 2,
        "max_filesize": MAX_FILE_BYTES,
    }

    is_threads = (urlparse(url).hostname or "").removeprefix("www.") in {"threads.net", "threads.com"}
    if is_threads:
        # Include silent videos; prefer a muxed MP4 and use ffmpeg for DASH.
        options["format"] = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio/bestvideo[ext=mp4]/best"
        options["merge_output_format"] = "mp4"
    with yt_dlp.YoutubeDL(options) as downloader:
        if is_threads:
            downloader.add_info_extractor(ThreadsIE())
        info = downloader.extract_info(url, download=True, ie_key="Threads" if is_threads else None)
        while info and "entries" in info:
            info = next((entry for entry in info["entries"] if entry), None)
        if not info:
            raise ValueError("\u0644\u0645 \u064a\u062a\u0645 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0641\u064a\u062f\u064a\u0648 \u0641\u064a \u0647\u0630\u0627 \u0627\u0644\u0645\u0646\u0634\u0648\u0631.")
        path = Path(info.get("filepath") or downloader.prepare_filename(info))

    if not path.exists():
        candidates = [p for p in Path(folder).iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}]
        if not candidates:
            raise FileNotFoundError("\u0644\u0645 \u064a\u062a\u0645 \u0625\u0646\u0634\u0627\u0621 \u0645\u0644\u0641 \u0627\u0644\u0641\u064a\u062f\u064a\u0648.")
        path = max(candidates, key=lambda item: item.stat().st_mtime)

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("\u0627\u0644\u0641\u064a\u062f\u064a\u0648 \u0623\u0643\u0628\u0631 \u0645\u0646 \u0627\u0644\u062d\u062f \u0627\u0644\u0630\u064a \u064a\u0633\u062a\u0637\u064a\u0639 \u0627\u0644\u0628\u0648\u062a \u0625\u0631\u0633\u0627\u0644\u0647.")
    return path


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text("Bot version: " + BUILD_VERSION)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message:
        await update.message.reply_text(
            "\u0623\u0631\u0633\u0644 \u0631\u0627\u0628\u0637 \u0641\u064a\u062f\u064a\u0648 \u0639\u0627\u0645 \u0645\u0646 TikTok \u0623\u0648 Instagram \u0623\u0648 X \u0623\u0648 Facebook \u0623\u0648 Threads \u0648\u0633\u0623\u062d\u0627\u0648\u0644 \u062a\u0646\u0632\u064a\u0644\u0647.\n\n"
            "\u0627\u0633\u062a\u062e\u062f\u0645 \u0627\u0644\u0628\u0648\u062a \u0641\u0642\u0637 \u0644\u0644\u0641\u064a\u062f\u064a\u0648\u0647\u0627\u062a \u0627\u0644\u062a\u064a \u062a\u0645\u0644\u0643\u0647\u0627 \u0623\u0648 \u0644\u062f\u064a\u0643 \u0625\u0630\u0646 \u0628\u062a\u0646\u0632\u064a\u0644\u0647\u0627."
        )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.message.text:
        return

    url = extract_video_url(update.message.text)
    if not url:
        await update.message.reply_text("\u0623\u0631\u0633\u0644 \u0631\u0627\u0628\u0637 \u0641\u064a\u062f\u064a\u0648 \u0639\u0627\u0645 \u0645\u0646 TikTok \u0623\u0648 Instagram \u0623\u0648 X \u0623\u0648 Facebook \u0623\u0648 Threads \u064a\u0628\u062f\u0623 \u0628\u0640 https://")
        return

    status = await update.message.reply_text("\u062c\u0627\u0631\u064a \u062a\u062c\u0647\u064a\u0632 \u0627\u0644\u0641\u064a\u062f\u064a\u0648\u2026")
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    try:
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="video_bot_") as temp_dir:
                video_path = await asyncio.to_thread(download_video, url, temp_dir)
                with video_path.open("rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                    )
        try:
            await status.delete()
        except Exception:
            logger.warning("Video sent, but status message could not be deleted")
    except yt_dlp.utils.DownloadError:
        logger.exception("Video download failed")
        await status.edit_text(
            "\u062a\u0639\u0630\u0651\u0631 \u062a\u0646\u0632\u064a\u0644 \u0627\u0644\u0641\u064a\u062f\u064a\u0648. \u062a\u0623\u0643\u062f \u0623\u0646 \u0627\u0644\u0631\u0627\u0628\u0637 \u0639\u0627\u0645 \u0648\u064a\u062d\u062a\u0648\u064a \u0641\u064a\u062f\u064a\u0648. \u0628\u0639\u0636 \u0631\u0648\u0627\u0628\u0637 Instagram \u0648X \u0648Facebook \u0648Threads \u062a\u062a\u0637\u0644\u0628 \u062a\u0633\u062c\u064a\u0644 \u062f\u062e\u0648\u0644 \u0648\u0644\u0627 \u064a\u0633\u062a\u0637\u064a\u0639 \u0627\u0644\u0628\u0648\u062a \u062a\u0646\u0632\u064a\u0644\u0647\u0627."
        )
    except ValueError as exc:
        await status.edit_text(str(exc))
    except Exception:
        logger.exception("Unexpected error")
        await status.edit_text("\u062d\u062f\u062b \u062e\u0637\u0623 \u063a\u064a\u0631 \u0645\u062a\u0648\u0642\u0639. \u062d\u0627\u0648\u0644 \u0645\u0631\u0629 \u0623\u062e\u0631\u0649 \u0644\u0627\u062d\u0642\u064b\u0627.")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("\u0636\u0639 TELEGRAM_BOT_TOKEN \u062f\u0627\u062e\u0644 \u0645\u0644\u0641 .env")

    application = Application.builder().token(token).build()
    # The first group records private users for both commands and ordinary messages.
    application.add_handler(MessageHandler(filters.ALL, remember_user), group=-1)
    application.add_handler(CommandHandler("id", my_id))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("version", version))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
