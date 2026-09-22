import asyncio
import logging
import os
import re
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

TIKTOK_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
ALLOWED_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
MAX_FILE_BYTES = 49 * 1024 * 1024
DOWNLOAD_SLOTS = asyncio.Semaphore(2)


def extract_tiktok_url(text: str) -> str | None:
    match = TIKTOK_URL_RE.search(text or "")
    if not match:
        return None

    url = match.group(0).rstrip(".,؛،!؟)]}")
    host = (urlparse(url).hostname or "").lower()
    if host not in ALLOWED_HOSTS and not host.endswith(".tiktok.com"):
        return None
    return url


def download_video(url: str, folder: str) -> tuple[Path, str]:
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
        title = (info.get("title") or "TikTok video").strip()

    if not path.exists():
        candidates = [p for p in Path(folder).iterdir() if p.is_file()]
        if not candidates:
            raise FileNotFoundError("لم يتم إنشاء ملف الفيديو.")
        path = max(candidates, key=lambda item: item.stat().st_mtime)

    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("الفيديو أكبر من الحد الذي يستطيع البوت إرساله.")
    return path, title[:900]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if update.message:
        await update.message.reply_text(
            "أرسل رابط فيديو TikTok عامًا وسأحاول تنزيل النسخة الأصلية المتاحة.\n\n"
            "استخدم البوت فقط للفيديوهات التي تملكها أو لديك إذن بتنزيلها."
        )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    del context
    if not update.message or not update.message.text:
        return

    url = extract_tiktok_url(update.message.text)
    if not url:
        await update.message.reply_text("أرسل رابط TikTok صحيحًا يبدأ بـ https://")
        return

    status = await update.message.reply_text("جاري تجهيز الفيديو…")
    await update.message.chat.send_action(ChatAction.UPLOAD_VIDEO)

    try:
        async with DOWNLOAD_SLOTS:
            with tempfile.TemporaryDirectory(prefix="tiktok_bot_") as temp_dir:
                video_path, title = await asyncio.to_thread(download_video, url, temp_dir)
                with video_path.open("rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption=f"{title}\n\nتم التنزيل للاستخدام المصرّح به فقط.",
                        supports_streaming=True,
                        read_timeout=120,
                        write_timeout=120,
                    )
        await status.delete()
    except yt_dlp.utils.DownloadError:
        logger.exception("TikTok download failed")
        await status.edit_text(
            "تعذّر تنزيل الفيديو. تأكد أن الرابط عام وصحيح، ثم حدّث yt-dlp وحاول مجددًا."
        )
    except ValueError as exc:
        await status.edit_text(str(exc))
    except Exception:
        logger.exception("Unexpected error")
        await status.edit_text("حدث خطأ غير متوقع. حاول مرة أخرى لاحقًا.")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("ضع TELEGRAM_BOT_TOKEN داخل ملف .env")

    application = Application.builder().token(token).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
