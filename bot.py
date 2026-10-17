import os
import asyncio
import logging
import uuid
import shutil
import time
import socket
import ipaddress
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
import yt_dlp

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Retrieve the Telegram Bot Token from environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# Port for the 24/7 keep-alive health-check server (used by Render/Koyeb)
PORT = int(os.environ.get("PORT", 8080))

# Directory to temporarily store downloaded audio files before sending
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Limit concurrent heavy downloads to prevent server RAM/CPU exhaustion
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 4))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

# 1. Per-user cooldown (in seconds) to prevent automated bot abuse and IP bans
USER_COOLDOWN_SECONDS = int(os.environ.get("USER_COOLDOWN_SECONDS", 15))
user_last_request: dict[int, float] = {}

# 2. Maximum allowed audio duration (in seconds) - default 30 mins (1800s)
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", 1800))

# Audio quality presets mapped to FFmpeg MP3 bitrates
QUALITY_PRESETS = {
    "standard": {
        "label": "Standard (128 kbps - Smaller Size)",
        "bitrate": "128",
    },
    "medium": {
        "label": "Medium (192 kbps - Balanced)",
        "bitrate": "192",
    },
    "high": {
        "label": "High (320 kbps - Best Quality)",
        "bitrate": "320",
    },
}


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler to satisfy Render/Koyeb 24/7 health checks."""

    def do_HEAD(self):
        """Handle UptimeRobot's default HEAD requests."""
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        """Handle standard browser/Render GET requests."""
        if self.path in ("/", "/health"):
            self.send_response(200)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"OK - Linko Bot is alive and running 24/7.")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress HTTP access logs to keep console clean
        pass


def start_health_server() -> None:
    """Starts the background HTTP server in a daemon thread."""
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        logger.info(f"24/7 Health-check web server listening on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health-check server: {e}")


def is_safe_public_url(text: str) -> tuple[bool, str]:
    """
    Validates that the string is a well-formed HTTP/HTTPS URL and protects
    the server against SSRF (Server-Side Request Forgery) by blocking local,
    private, loopback, or reserved IP addresses.
    """
    try:
        result = urlparse(text.strip())
        if result.scheme not in ("http", "https") or not result.netloc:
            return False, "Please provide a valid URL starting with `http://` or `https://`."

        hostname = result.hostname
        if not hostname:
            return False, "Could not determine the website hostname."

        # Block localhost or local network domains explicitly
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return False, "Localhost and internal network links are blocked for security."

        # Resolve hostname to IP and verify it is globally routable (not private/internal)
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
                return False, "Internal or private network addresses are not permitted."

        return True, ""
    except socket.gaierror:
        return False, "Could not resolve that website domain. Please check the link for typos."
    except Exception:
        return False, "Invalid URL format."


def BlockingDownloadAudio(url: str, bitrate: str, output_template: str) -> dict:
    """
    Synchronous function executed inside a thread pool to inspect metadata first
    (enforcing duration and live-stream policies) and then convert media to MP3.
    Does not bypass DRM, paywalls, or private authentication.
    """
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": False,  # Respect regional restrictions
        "socket_timeout": 20,
        "max_filesize": 50 * 1024 * 1024,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            }
        ],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        # Step 1: Extract metadata without downloading to verify policy compliance
        info = ydl.extract_info(url, download=False)
        if not info:
            raise ValueError("Unable to retrieve media information.")

        if info.get("is_live"):
            raise ValueError("Live streams cannot be downloaded.")

        duration = info.get("duration") or 0
        if duration > MAX_DURATION_SECONDS:
            max_mins = MAX_DURATION_SECONDS // 60
            raise ValueError(
                f"Media duration ({duration // 60} mins) exceeds the {max_mins}-minute policy limit."
            )

        # Step 2: Proceed with download and MP3 conversion
        info = ydl.extract_info(url, download=True)
        return {
            "title": info.get("title", "Audio Track"),
            "uploader": info.get("uploader", "Linko Bot"),
            "duration": duration,
            "webpage_url": info.get("webpage_url", url),
        }


async def post_init(application) -> None:
    """
    Automatically registers commands with Telegram so that typing '/' in the chat
    displays an auto-suggest menu for the user.
    """
    commands = [
        BotCommand("start", "Start Linko Bot and see welcome info"),
        BotCommand("help", "Learn how to download MP3 audio from links"),
        BotCommand("quality", "View details about Standard, Medium, and High MP3 modes"),
        BotCommand("terms", "Read copyright, platform policy, and security terms"),
        BotCommand("about", "Information about Linko Bot"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot command menu registered successfully.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /start command."""
    user_first_name = update.effective_user.first_name or "there"
    welcome_text = (
        f"👋 Welcome to *Linko*, {user_first_name}!\n\n"
        "I am a non-profit utility bot that converts publicly accessible media links "
        "into downloadable *MP3 audio files*.\n\n"
        "🚀 *How to use:*\n"
        "1. Paste a public link in this chat.\n"
        "2. Confirm that you have the right to download the content and pick your quality (*Standard*, *Medium*, or *High*).\n"
        "3. Receive your converted MP3 file directly on your device!\n\n"
        "⚖️ _By using Linko, you agree to our `/terms` regarding copyright and platform policies._"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /help command."""
    help_text = (
        "📖 *Linko Bot Help Guide*\n\n"
        "Simply paste a valid, publicly accessible link starting with `http://` or `https://`.\n\n"
        "*Available Commands:*\n"
        "• `/start` - Restart the bot and view welcome message\n"
        "• `/help` - Show this usage guide\n"
        "• `/quality` - Compare audio quality bitrates and file sizes\n"
        "• `/terms` - View copyright, fair-use, and platform rules\n"
        "• `/about` - Learn about Linko\n\n"
        "*Limits:* Maximum duration is *30 minutes* and maximum file size is *50 MB*."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /quality command explaining bitrate differences."""
    quality_text = (
        "🎧 *Audio Quality & Format Options*\n\n"
        "All links are converted into universal *.mp3* format, tailored by bitrate:\n\n"
        "1️⃣ *Standard (128 kbps)*\n"
        "   • *Best for:* Podcasts, speeches, fast downloads, saving data.\n"
        "   • *Approx. Size:* ~1 MB per minute.\n\n"
        "2️⃣ *Medium (192 kbps)*\n"
        "   • *Best for:* Everyday music listening with balanced size and clarity.\n"
        "   • *Approx. Size:* ~1.4 MB per minute.\n\n"
        "3️⃣ *High (320 kbps)*\n"
        "   • *Best for:* High-fidelity music and maximum MP3 audio depth.\n"
        "   • *Approx. Size:* ~2.4 MB per minute."
    )
    await update.message.reply_text(quality_text, parse_mode="Markdown")


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /about command."""
    about_text = (
        "🤖 *About Linko*\n\n"
        "Linko is a free, non-profit educational utility bot built to make saving "
        "authorized, public-domain, and Creative Commons audio in MP3 format simple.\n\n"
        "Built with Python, `python-telegram-bot`, `yt-dlp`, and `FFmpeg`."
    )
    await update.message.reply_text(about_text, parse_mode="Markdown")


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /terms command outlining platform policies and security rules."""
    terms_text = (
        "⚖️ *Platform Policy, Copyright & Security Terms*\n\n"
        "1. *Authorized & Fair Use Only:* Linko is strictly intended for downloading "
        "your own content, Creative Commons/royalty-free audio, public domain works, "
        "or material for which you have explicit permission from the copyright holder.\n\n"
        "2. *No DRM or Paywall Circumvention:* Linko respects platform security measures. "
        "It does *not* bypass Digital Rights Management (DRM), private accounts, "
        "age-restricted gates, or paid subscriptions.\n\n"
        "3. *Platform Terms of Service:* Social platforms (such as YouTube, TikTok, "
        "Instagram, Facebook, and X) prohibit unauthorized redistribution of copyrighted "
        "content. Users are solely responsible for complying with the Terms of Service "
        "of the source website.\n\n"
        "4. *Zero Retention Privacy:* Downloaded files are processed in isolated temporary "
        "folders and permanently deleted from the server immediately after delivery."
    )
    await update.message.reply_text(terms_text, parse_mode="Markdown")


async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Validates incoming text messages for URLs, enforces per-user cooldowns and SSRF
    checks, and displays an inline keyboard to select Standard, Medium, or High MP3 quality.
    """
    user_id = update.effective_user.id
    now = time.time()

    # Check per-user rate limit to protect server and prevent platform IP blocks
    last_req = user_last_request.get(user_id, 0)
    if now - last_req < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last_req))
        await update.message.reply_text(
            f"⏳ Please wait *{remaining} seconds* before submitting another link.",
            parse_mode="Markdown",
        )
        return

    message_text = update.message.text.strip()

    is_safe, error_reason = is_safe_public_url(message_text)
    if not is_safe:
        await update.message.reply_text(f"❌ {error_reason}", parse_mode="Markdown")
        return

    # Store the URL in user_data with a short unique ID so callback data stays under Telegram's 64-byte limit
    request_id = str(uuid.uuid4())[:8]
    if "links" not in context.user_data:
        context.user_data["links"] = {}
    context.user_data["links"][request_id] = message_text

    keyboard = [
        [
            InlineKeyboardButton(
                "🔈 Standard (128 kbps - ~1 MB/min)",
                callback_data=f"dl|{request_id}|standard",
            )
        ],
        [
            InlineKeyboardButton(
                "🔉 Medium (192 kbps - ~1.4 MB/min)",
                callback_data=f"dl|{request_id}|medium",
            )
        ],
        [
            InlineKeyboardButton(
                "🔊 High (320 kbps - ~2.4 MB/min)",
                callback_data=f"dl|{request_id}|high",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data=f"dl|{request_id}|cancel",
            )
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🎵 *Link verified!*\n\n"
        "_By selecting a quality below, you confirm that this content is public and "
        "you have permission to download it under `/terms`._\n\n"
        "Choose your desired MP3 audio quality:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def handle_quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Triggered when a user clicks one of the quality buttons.
    Manages queueing, runs the extraction in a background thread, sends the MP3, and cleans up disk space.
    """
    query = update.callback_query
    await query.answer()

    try:
        _, request_id, quality_key = query.data.split("|")
    except ValueError:
        await query.edit_message_text("⚠️ Invalid selection. Please send the link again.")
        return

    if quality_key == "cancel":
        context.user_data.get("links", {}).pop(request_id, None)
        await query.edit_message_text("🚫 Download request cancelled.")
        return

    url = context.user_data.get("links", {}).get(request_id)
    if not url:
        await query.edit_message_text(
            "⏳ This session expired. Please paste your link again."
        )
        return

    # Record user cooldown timestamp upon starting a download
    user_last_request[update.effective_user.id] = time.time()

    preset = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS["medium"])
    bitrate = preset["bitrate"]

    await query.edit_message_text(
        f"⏳ *Queued for processing...*\nQuality: `{preset['label']}`\nPlease wait while Linko prepares your MP3.",
        parse_mode="Markdown",
    )

    # Create a dedicated temporary folder for this specific request to avoid filename collisions
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    output_template = os.path.join(job_dir, "%(title).80s.%(ext)s")

    try:
        async with download_semaphore:
            await query.edit_message_text(
                f"⚙️ *Verifying & converting to MP3 ({bitrate} kbps)...*",
                parse_mode="Markdown",
            )
            # Offload blocking yt-dlp + FFmpeg work to a background thread
            metadata = await asyncio.to_thread(
                BlockingDownloadAudio, url, bitrate, output_template
            )

        # Find the generated .mp3 file inside the job directory
        mp3_files = [
            os.path.join(job_dir, f)
            for f in os.listdir(job_dir)
            if f.endswith(".mp3")
        ]

        if not mp3_files:
            await query.edit_message_text(
                "❌ Failed to extract an MP3 file from this link. Make sure the link is public and contains audio."
            )
            return

        mp3_path = mp3_files[0]
        file_size_mb = os.path.getsize(mp3_path) / (1024 * 1024)

        if file_size_mb > 49.5:
            await query.edit_message_text(
                f"⚠️ The converted MP3 is `{file_size_mb:.1f} MB`, which exceeds Telegram's 50 MB bot upload limit. Try choosing *Standard (128 kbps)* quality.",
                parse_mode="Markdown",
            )
            return

        await query.edit_message_text("📤 *Uploading MP3 to Telegram...*", parse_mode="Markdown")

        with open(mp3_path, "rb") as audio_file:
            await context.bot.send_audio(
                chat_id=update.effective_chat.id,
                audio=audio_file,
                title=metadata.get("title", "Audio"),
                performer=metadata.get("uploader", "Linko"),
                caption=(
                    f"✅ *Downloaded via Linko*\n"
                    f"🎚 *Quality:* {bitrate} kbps MP3\n"
                    f"📦 *Size:* {file_size_mb:.2f} MB"
                ),
                parse_mode="Markdown",
            )

        await query.delete_message()

    except ValueError as ve:
        # Friendly policy / duration / live-stream error messages
        await query.edit_message_text(f"⚠️ *Policy Notice:* {ve}", parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error processing URL {url}: {e}")
        await query.edit_message_text(
            "❌ *Unable to process this link.*\n\n"
            "Possible reasons:\n"
            "• The post is private, age-restricted, or requires login.\n"
            "• The platform blocked automated extraction (anti-bot protection).\n"
            "• The content is DRM-protected or exceeds the 50 MB limit.",
            parse_mode="Markdown",
        )
    finally:
        # Always clean up temporary files immediately so free hosting disk doesn't fill up
        shutil.rmtree(job_dir, ignore_errors=True)
        context.user_data.get("links", {}).pop(request_id, None)


def main() -> None:
    """Entry point to start the 24/7 health server, configure handlers, and start polling."""
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("Please set your BOT_TOKEN environment variable before running.")
        return

    # Start the 24/7 HTTP keep-alive server in a background daemon thread
    health_thread = Thread(target=start_health_server, daemon=True)
    health_thread.start()

    # Build the async Telegram application with concurrent updates enabled
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("quality", quality_command))
    application.add_handler(CommandHandler("terms", terms_command))
    application.add_handler(CommandHandler("about", about_command))

    # Register callback query handler for quality selection buttons
    application.add_handler(CallbackQueryHandler(handle_quality_callback, pattern=r"^dl\|"))

    # Register message handler for links
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_message)
    )

    logger.info("Linko Bot is now running...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()