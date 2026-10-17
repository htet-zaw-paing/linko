import os
import re
import uuid
import time
import base64
import shutil
import socket
import logging
import asyncio
import ipaddress
import threading
import subprocess
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
import yt_dlp
from telegram import (
    Update,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode, ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ---------------------------------------------------------------------------
# LOGGING & CONFIGURATION
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
PORT = int(os.environ.get("PORT", 8080))

# Optional: Base64-encoded Netscape cookies.txt string set in Render Environment Variables
COOKIES_B64 = os.environ.get("COOKIES_B64", "").strip()
# Optional: Custom Cobalt API URL if you have a preferred instance
CUSTOM_COBALT_API = os.environ.get("COBALT_API_URL", "").strip()

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 6))
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

MAX_FILESIZE_BYTES = 49 * 1024 * 1024  # 49 MB (Telegram Bot API limit is 50 MB)
MAX_DURATION_SECONDS = 1800            # 30 minutes max duration
USER_COOLDOWN_SECONDS = 10             # Cooldown per user

TEMP_DIR = "/tmp/linko_downloads"
os.makedirs(TEMP_DIR, exist_ok=True)

COOKIES_FILE_PATH = "/tmp/linko_cookies.txt"
if COOKIES_B64:
    try:
        with open(COOKIES_FILE_PATH, "wb") as f:
            f.write(base64.b64decode(COOKIES_B64))
        logger.info("Loaded custom cookies.txt from COOKIES_B64 environment variable.")
    except Exception as e:
        logger.warning(f"Failed to decode COOKIES_B64: {e}")

QUALITY_PROFILES = {
    "standard": {
        "label": "Standard (128 kbps • Smallest Size)",
        "bitrate": "128",
        "desc": "Fastest download, saves data (~1 MB/min)",
    },
    "medium": {
        "label": "Medium (192 kbps • Balanced)",
        "bitrate": "192",
        "desc": "Great balance of clarity & size (~1.5 MB/min)",
    },
    "high": {
        "label": "High (320 kbps • Studio Quality)",
        "bitrate": "320",
        "desc": "Maximum MP3 fidelity (~2.4 MB/min)",
    },
}

URL_REGEX = re.compile(r"(https?://[^\s]+)", re.IGNORECASE)
USER_LAST_REQUEST: dict[int, float] = {}


# ---------------------------------------------------------------------------
# 24/7 KEEP-ALIVE WEB SERVER (SUPPORTS BOTH GET AND HEAD FOR UPTIMEROBOT)
# ---------------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK - Linko Bot is alive and running 24/7.")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    logger.info(f"24/7 Health-check web server listening on port {PORT}")
    server.serve_forever()


# ---------------------------------------------------------------------------
# SECURITY & SSRF PROTECTION
# ---------------------------------------------------------------------------
def is_safe_public_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        if hostname.lower() in ("localhost", "127.0.0.1", "::1") or hostname.endswith(".local"):
            return False

        addr_info = socket.getaddrinfo(hostname, None)
        for item in addr_info:
            ip_str = item[4][0]
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
            ):
                return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# FALLBACK ENGINE: COBALT API + FFMPEG TRANSCODING
# ---------------------------------------------------------------------------
def try_cobalt_fallback(url: str, bitrate: str, task_dir: str) -> dict:
    """
    If yt-dlp is blocked by datacenter IP filters on YouTube/Instagram,
    attempts extraction via public Cobalt API instances and converts to
    the exact target MP3 bitrate using local FFmpeg.
    """
    endpoints = []
    if CUSTOM_COBALT_API:
        endpoints.append(CUSTOM_COBALT_API)
    endpoints.extend([
        "https://cobalt-api.kwiatekmiki.com",
        "https://cobalt-backend.canine.tools",
        "https://cbl.wuk.sh",
    ])

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0 Safari/537.36",
    }

    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "mp3",
        "audioBitrate": bitrate,
    }

    last_err = "No fallback instances responded."
    for base_api in endpoints:
        try:
            resp = requests.post(base_api.rstrip("/"), json=payload, headers=headers, timeout=15)
            if resp.status_code != 200:
                last_err = f"Fallback HTTP {resp.status_code}"
                continue

            data = resp.json()
            dl_url = data.get("url")
            if not dl_url:
                continue

            raw_file = os.path.join(task_dir, "raw_input_audio")
            with requests.get(dl_url, stream=True, timeout=30) as r:
                r.raise_for_status()
                downloaded = 0
                with open(raw_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            downloaded += len(chunk)
                            if downloaded > MAX_FILESIZE_BYTES:
                                raise ValueError("Audio stream exceeds Telegram's 50 MB limit.")
                            f.write(chunk)

            # Enforce exact user-selected MP3 bitrate using FFmpeg
            final_mp3 = os.path.join(task_dir, "Linko_Audio.mp3")
            cmd = [
                "ffmpeg", "-y", "-i", raw_file,
                "-vn", "-ar", "44100", "-ac", "2",
                "-b:a", f"{bitrate}k",
                final_mp3,
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            file_size = os.path.getsize(final_mp3)
            if file_size > MAX_FILESIZE_BYTES:
                raise ValueError("Converted MP3 exceeds Telegram's 50 MB limit.")

            raw_filename = data.get("filename", "Linko_Audio")
            clean_title = os.path.splitext(raw_filename)[0].replace("_", " ")

            return {
                "filepath": final_mp3,
                "title": clean_title[:100] or "Linko Audio",
                "uploader": "Linko Extractor",
                "duration": 0,
                "filesize": file_size,
            }
        except Exception as e:
            last_err = str(e)
            continue

    raise RuntimeError(f"Fallback failed ({last_err})")


# ---------------------------------------------------------------------------
# PRIMARY ENGINE: YT-DLP WITH ANTI-BOT BYPASS & MOBILE CLIENT SPOOFING
# ---------------------------------------------------------------------------
def download_and_convert_mp3(url: str, bitrate: str, task_dir: str) -> dict:
    output_template = os.path.join(task_dir, "%(title).100s.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "max_filesize": MAX_FILESIZE_BYTES,
        "socket_timeout": 25,
        "retries": 3,
        # Spoof mobile/TV clients so YouTube doesn't block Render's datacenter IP as easily
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android_vr", "tv", "mweb", "web"],
            }
        },
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
        ],
    }

    # Attach cookies if provided via COOKIES_B64 environment variable
    if os.path.exists(COOKIES_FILE_PATH) and os.path.getsize(COOKIES_FILE_PATH) > 0:
        ydl_opts["cookiefile"] = COOKIES_FILE_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                raise ValueError("Could not retrieve media metadata.")

            if info.get("is_live"):
                raise ValueError("Live streams cannot be converted to MP3 files.")

            duration = int(info.get("duration") or 0)
            if duration > MAX_DURATION_SECONDS:
                raise ValueError(
                    f"Media is too long ({duration // 60} mins). Max allowed is {MAX_DURATION_SECONDS // 60} mins."
                )

            title = info.get("title", "Linko_Audio")
            uploader = info.get("uploader", "Unknown Artist")

            mp3_file = None
            for fname in os.listdir(task_dir):
                if fname.lower().endswith(".mp3"):
                    mp3_file = os.path.join(task_dir, fname)
                    break

            if not mp3_file or not os.path.exists(mp3_file):
                raise FileNotFoundError("MP3 conversion failed.")

            file_size = os.path.getsize(mp3_file)
            if file_size > MAX_FILESIZE_BYTES:
                raise ValueError("Converted MP3 exceeds Telegram's 50 MB bot upload limit.")

            return {
                "filepath": mp3_file,
                "title": title,
                "uploader": uploader,
                "duration": duration,
                "filesize": file_size,
            }

    except Exception as ytdlp_err:
        logger.warning(f"Primary yt-dlp failed for {url}: {ytdlp_err}. Trying Cobalt fallback...")
        try:
            return try_cobalt_fallback(url, bitrate, task_dir)
        except Exception as fallback_err:
            # Raise the original yt-dlp error so the user sees what happened
            raise RuntimeError(f"{ytdlp_err}") from fallback_err


# ---------------------------------------------------------------------------
# TELEGRAM COMMAND HANDLERS
# ---------------------------------------------------------------------------
async def post_init(application) -> None:
    commands = [
        BotCommand("start", "Start Linko & see how to download MP3s"),
        BotCommand("quality", "Set default MP3 quality (Standard, Medium, High)"),
        BotCommand("help", "Supported platforms, sizes & usage guide"),
        BotCommand("terms", "Platform policies, copyright & fair-use notice"),
        BotCommand("about", "About Linko Bot"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot command menu registered successfully.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    default_q = context.user_data.get("default_quality", "medium")
    q_label = QUALITY_PROFILES[default_q]["label"]

    text = (
        "🎧 *Welcome to Linko!* \n\n"
        "I am a free, non-profit audio utility bot. Paste a public link from "
        "*YouTube, TikTok, Instagram, Facebook, X (Twitter), SoundCloud*, and more, "
        "and I will convert it into an MP3 file for your device.\n\n"
        "✨ *How to use:*\n"
        "1️⃣ Paste any supported public video/audio link here.\n"
        "2️⃣ Choose your desired audio quality (*Standard 128k*, *Medium 192k*, or *High 320k*).\n"
        "3️⃣ Download and save the MP3 directly to your device!\n\n"
        f"⚙️ *Your Default Quality:* `{q_label}`\n"
        "Type `/` to view all available commands at any time."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📖 *Linko Help & Audio Quality Guide*\n\n"
        "*Audio Quality Tiers:*\n"
        "• 🔉 *Standard (128 kbps):* Smallest file size (~1 MB/min), ideal for voice & podcasts.\n"
        "• 🔊 *Medium (192 kbps):* Balanced audio quality and file size (~1.5 MB/min).\n"
        "• 🎵 *High (320 kbps):* Maximum MP3 studio fidelity (~2.4 MB/min), larger file size.\n\n"
        "*Commands:*\n"
        "• `/start` — Welcome message & status\n"
        "• `/quality` — Change your default MP3 quality\n"
        "• `/terms` — Copyright, fair-use & security policies\n"
        "• `/about` — About Linko\n\n"
        "⚠️ *Limits:* Max duration is 30 minutes and max output file size is 50 MB."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "⚖️ *Terms of Use, Copyright & Platform Policy*\n\n"
        "1. *Personal & Authorized Use Only:* Linko is an educational, non-profit tool designed "
        "for downloading user-owned content, Creative Commons/royalty-free media, or public domain audio.\n"
        "2. *Respect Copyright & Platform ToS:* Users are solely responsible for ensuring they have "
        "the legal right to download and convert the media they submit.\n"
        "3. *Zero Data Retention:* Linko deletes all temporary files immediately after sending them to your chat."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 *About Linko*\n\n"
        "• *Name:* Linko\n"
        "• *Purpose:* Free, non-profit convenience bot to convert public media links into MP3 audio.\n"
        "• *Cost:* 100% Free Forever.\n"
        "• *Architecture:* Asynchronous Python + Multi-Engine Extractor + FFmpeg."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    current = context.user_data.get("default_quality", "medium")
    buttons = []
    for key, prof in QUALITY_PROFILES.items():
        prefix = "✅ " if key == current else ""
        buttons.append(
            [InlineKeyboardButton(f"{prefix}{prof['label']}", callback_data=f"setq|{key}")]
        )

    await update.message.reply_text(
        "🎚 *Select your default MP3 audio quality:*",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# LINK HANDLING & CALLBACK QUERIES
# ---------------------------------------------------------------------------
async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    now = time.time()

    last_req = USER_LAST_REQUEST.get(user_id, 0)
    if now - last_req < USER_COOLDOWN_SECONDS:
        wait_sec = int(USER_COOLDOWN_SECONDS - (now - last_req)) + 1
        await update.message.reply_text(
            f"⏳ Please wait *{wait_sec}s* before sending another link.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    match = URL_REGEX.search(update.message.text)
    if not match:
        await update.message.reply_text(
            "🔗 Please paste a valid `http://` or `https://` link from YouTube, TikTok, Instagram, Facebook, or X.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    url = match.group(1)

    loop = asyncio.get_running_loop()
    is_safe = await loop.run_in_executor(None, is_safe_public_url, url)
    if not is_safe:
        await update.message.reply_text("🚫 Invalid or restricted URL.")
        return

    pending_links = context.user_data.setdefault("pending_links", {})
    if len(pending_links) > 20:
        pending_links.clear()

    url_id = uuid.uuid4().hex[:8]
    pending_links[url_id] = url

    default_q = context.user_data.get("default_quality", "medium")
    buttons = []
    for key, prof in QUALITY_PROFILES.items():
        star = "⭐ " if key == default_q else "🎵 "
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{star}{prof['label']}",
                    callback_data=f"dl|{key}|{url_id}",
                )
            ]
        )

    await update.message.reply_text(
        "🎶 *Link detected!* Choose your MP3 audio quality and file size:\n\n"
        "• *Standard (128k):* Smallest file size\n"
        "• *Medium (192k):* Balanced quality & size\n"
        "• *High (320k):* Best audio quality (largest file)\n\n"
        "_By downloading, you confirm you have rights to this content (/terms)._",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.MARKDOWN,
        disable_web_page_preview=True,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    if data.startswith("setq|"):
        _, q_key = data.split("|", 1)
        if q_key in QUALITY_PROFILES:
            context.user_data["default_quality"] = q_key
            await query.edit_message_text(
                f"✅ Default audio quality updated to:\n*{QUALITY_PROFILES[q_key]['label']}*",
                parse_mode=ParseMode.MARKDOWN,
            )
        return

    if data.startswith("dl|"):
        parts = data.split("|")
        if len(parts) != 3:
            return
        _, q_key, url_id = parts

        pending_links = context.user_data.get("pending_links", {})
        url = pending_links.get(url_id)
        if not url:
            await query.edit_message_text(
                "⚠️ This link session expired. Please paste the link again."
            )
            return

        profile = QUALITY_PROFILES.get(q_key, QUALITY_PROFILES["medium"])
        bitrate = profile["bitrate"]

        user_id = update.effective_user.id
        USER_LAST_REQUEST[user_id] = time.time()

        await query.edit_message_text(
            f"⏳ *Queued & Processing...*\n"
            f"• *Format:* MP3 ({bitrate} kbps)\n"
            f"• *Status:* Extracting & converting audio...",
            parse_mode=ParseMode.MARKDOWN,
        )

        task_dir = os.path.join(TEMP_DIR, f"task_{uuid.uuid4().hex}")
        os.makedirs(task_dir, exist_ok=True)

        try:
            async with DOWNLOAD_SEMAPHORE:
                await context.bot.send_chat_action(
                    chat_id=update.effective_chat.id, action=ChatAction.RECORD_VOICE
                )
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    None, download_and_convert_mp3, url, bitrate, task_dir
                )

            size_mb = result["filesize"] / (1024 * 1024)
            await query.edit_message_text(
                f"📤 *Uploading MP3 ({size_mb:.2f} MB)...*",
                parse_mode=ParseMode.MARKDOWN,
            )

            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_VOICE
            )

            caption = (
                f"🎵 *{result['title']}*\n"
                f"🎚 *Quality:* {profile['label']}\n"
                f"📦 *Size:* {size_mb:.2f} MB\n"
                f"🤖 _Downloaded via @{(await context.bot.get_me()).username}_"
            )

            with open(result["filepath"], "rb") as audio_stream:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio_stream,
                    title=result["title"],
                    performer=result["uploader"],
                    duration=result["duration"],
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                )

            await query.delete_message()

        except Exception as exc:
            logger.warning(f"Download error for URL {url}: {exc}")
            # Clean ANSI color codes from yt-dlp error strings
            clean_err = re.sub(r"\x1b\[[0-9;]*m", "", str(exc))
            short_reason = clean_err.splitlines()[-1][:180] if clean_err else "Unknown error"

            if "Sign in to confirm" in clean_err or "bot" in clean_err.lower() or "login required" in clean_err.lower():
                friendly = (
                    "❌ *Platform Anti-Bot Block Detected*\n\n"
                    "YouTube/Instagram temporarily blocked anonymous access from this cloud server's IP.\n"
                    "💡 *Fix for Bot Owner:* Add a `COOKIES_B64` environment variable in Render to authenticate requests.\n\n"
                    f"`Details: {short_reason}`"
                )
            else:
                friendly = (
                    "❌ *Could not extract audio from this link.*\n\n"
                    f"`Reason: {short_reason}`"
                )
            await query.edit_message_text(friendly)

        finally:
            shutil.rmtree(task_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# MAIN ENTRYPOINT
# ---------------------------------------------------------------------------
def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError("BOT_TOKEN environment variable is not set!")

    health_thread = threading.Thread(target=start_health_server, daemon=True)
    health_thread.start()

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quality", quality_command))
    app.add_handler(CommandHandler("terms", terms_command))
    app.add_handler(CommandHandler("about", about_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_message))

    logger.info("Linko Bot is now running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()