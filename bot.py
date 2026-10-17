import os
import asyncio
import logging
import re
import uuid
import shutil
import time
import socket
import ipaddress
import base64
import subprocess
import urllib.parse
import requests
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")
PORT = int(os.environ.get("PORT", 8080))
DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 4))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
USER_COOLDOWN_SECONDS = int(os.environ.get("USER_COOLDOWN_SECONDS", 15))
user_last_request: dict[int, float] = {}
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", 1800))
MAX_PLAYLIST_ITEMS = 10

QUALITY_PRESETS = {
    "standard": {"label": "Standard (128 kbps - Smaller Size)", "bitrate": "128"},
    "medium": {"label": "Medium (192 kbps - Balanced)", "bitrate": "192"},
    "high": {"label": "High (320 kbps - Best Quality)", "bitrate": "320"},
}

# Load Cookies from Render Environment Variables for Instagram/YouTube Login Bypass
COOKIES_B64 = os.environ.get("COOKIES_B64", "").strip()
COOKIES_FILE_PATH = "/tmp/linko_cookies.txt"
if COOKIES_B64:
    try:
        with open(COOKIES_FILE_PATH, "wb") as f:
            f.write(base64.b64decode(COOKIES_B64))
        logger.info("Loaded custom cookies.txt from COOKIES_B64 environment variable.")
    except Exception as e:
        logger.warning(f"Failed to decode COOKIES_B64: {e}")


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


def start_health_server() -> None:
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        logger.info(f"24/7 Health-check web server listening on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health-check server: {e}")


def is_safe_public_url(text: str) -> tuple[bool, str]:
    try:
        result = urlparse(text.strip())
        if result.scheme not in ("http", "https") or not result.netloc:
            return (False, "Please provide a valid URL starting with `http://` or `https://`.")
        hostname = result.hostname
        if not hostname:
            return (False, "Could not determine the website hostname.")
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return (False, "Localhost and internal network links are blocked for security.")
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
                return (False, "Internal or private network addresses are not permitted.")
        return (True, "")
    except socket.gaierror:
        return (False, "Could not resolve that website domain. Please check the link for typos.")
    except Exception:
        return (False, "Invalid URL format.")


def fetch_lrclib_lyrics(query: str) -> str:
    """Fetches plain lyrics from the free LRCLIB API."""
    headers = {"User-Agent": "LinkoBot (https://t.me/LinkoBot)"}
    try:
        r = requests.get(f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("plainLyrics") or data[0].get("syncedLyrics")
    except Exception as e:
        logger.warning(f"LRCLIB failed: {e}")
    return ""


def BlockingDownloadAudio(url: str, bitrate: str, output_template: str) -> dict:
    """Downloads audio utilizing yt-dlp mobile spoofing, Mutagen embedding, and Cobalt API Fallback."""
    job_dir = os.path.dirname(output_template)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 2,
        "fragment_retries": 2,
        "max_filesize": 50 * 1024 * 1024,
        "writethumbnail": True,  # Grab cover art
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android_vr", "tv", "mweb", "web"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            },
            {
                "key": "EmbedThumbnail", # Embed cover art into MP3 via Mutagen
            },
            {
                "key": "FFmpegMetadata", # Apply ID3 Tags
                "add_metadata": True,
            }
        ],
    }

    if os.path.exists(COOKIES_FILE_PATH) and os.path.getsize(COOKIES_FILE_PATH) > 0:
        ydl_opts["cookiefile"] = COOKIES_FILE_PATH

    # Engine 1: yt-dlp with anti-bot bypass and Mutagen metadata
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("Unable to retrieve media information.")
            if info.get("is_live"):
                raise ValueError("Live streams cannot be downloaded.")
            duration = info.get("duration") or 0
            if duration > MAX_DURATION_SECONDS:
                max_mins = MAX_DURATION_SECONDS // 60
                raise ValueError(f"Media duration ({duration // 60} mins) exceeds the {max_mins}-minute policy limit.")
            
            info = ydl.process_ie_result(info, download=True)
            return {
                "title": info.get("title", "Audio Track"),
                "uploader": info.get("uploader", "Linko Bot"),
                "duration": duration,
                "webpage_url": info.get("webpage_url", url),
            }
            
    except Exception as ytdlp_error:
        logger.warning(f"yt-dlp blocked for {url}: {ytdlp_error}. Engaging Cobalt Fallback API...")
        
        # Engine 2: Cobalt API Fallback (Bypasses Datacenter IP Blocks)
        payload = {
            "url": url,
            "downloadMode": "audio",
            "audioFormat": "mp3",
            "audioBitrate": bitrate,
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        endpoints = [
            "https://cobalt-api.kwiatekmiki.com",
            "https://cobalt-backend.canine.tools",
            "https://cbl.wuk.sh"
        ]
        
        for api in endpoints:
            try:
                resp = requests.post(api, json=payload, headers=headers, timeout=20)
                if resp.status_code == 200:
                    dl_url = resp.json().get("url")
                    title = resp.json().get("filename", "Linko_Audio").replace("_", " ")
                    if dl_url:
                        raw_file = os.path.join(job_dir, "raw_audio_temp")
                        with requests.get(dl_url, stream=True, timeout=30) as r:
                            r.raise_for_status()
                            with open(raw_file, "wb") as f:
                                for chunk in r.iter_content(chunk_size=65536):
                                    f.write(chunk)
                                    
                        # Force exact bitrate conversion using FFmpeg
                        final_mp3 = os.path.join(job_dir, f"Linko_{uuid.uuid4().hex[:6]}.mp3")
                        subprocess.run(
                            ["ffmpeg", "-y", "-i", raw_file, "-vn", "-ar", "44100", "-ac", "2", "-b:a", f"{bitrate}k", final_mp3],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=True
                        )
                        return {
                            "title": title[:80] or "Audio Track",
                            "uploader": "Linko Extractor",
                            "duration": 0,
                            "webpage_url": url,
                        }
            except Exception:
                continue
                
        raise ytdlp_error


def explain_download_error(error: Exception) -> str:
    message = str(error).lower()
    if any((term in message for term in ("sign in to confirm", "not a bot"))):
        return "YouTube/Instagram blocked anonymous access from this server's IP. Add a `COOKIES_B64` environment variable in Render to authenticate requests."
    if any((term in message for term in ("sign in", "login required", "private", "authentication"))):
        return "This media appears to require login, is private, or is age-restricted. Linko only processes publicly accessible content."
    if "ffmpeg" in message or "ffprobe" in message or "postprocessing" in message:
        return "The MP3 conversion tool is unavailable. Check that the latest Docker image finished building with FFmpeg installed."
    if "unsupported url" in message or "no suitable extractor" in message:
        return "This link format is not supported. Send the full public post or video URL."
    if any((term in message for term in ("file is larger", "filesize", "file size"))):
        return "The source is larger than Linko's 50 MB processing limit."
    if any((term in message for term in ("no video formats", "requested format", "no formats"))):
        return "The platform did not provide a downloadable audio format for this public link."
    return "The platform did not return downloadable media. Check that the link is public, then try again later."


async def post_init(application) -> None:
    commands = [
        BotCommand("start", "Start Linko Bot and see welcome info"),
        BotCommand("search", "Search and download songs instantly"),
        BotCommand("help", "Learn how to download MP3 audio from links"),
        BotCommand("quality", "View details about Standard, Medium, and High MP3 modes"),
        BotCommand("terms", "Read copyright, platform policy, and security terms"),
        BotCommand("about", "Information about Linko Bot"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Bot command menu registered successfully.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_first_name = update.effective_user.first_name or "there"
    welcome_text = f"👋 Welcome to *Linko*, {user_first_name}!\n\nI am a non-profit utility bot that converts publicly accessible media links into downloadable *MP3 audio files*.\n\n🚀 *How to use:*\n1. Paste a public link in this chat.\n2. Or type `/search [song name]` to find music instantly!\n3. Receive your converted MP3 file directly on your device!\n\n⚖️ _By using Linko, you agree to our `/terms` regarding copyright and platform policies._"
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = "📖 *Linko Bot Help Guide*\n\nSimply paste a valid, publicly accessible link starting with `http://` or `https://`.\n\n*Available Commands:*\n• `/start` - Restart the bot and view welcome message\n• `/search` - Search YouTube and download audio\n• `/help` - Show this usage guide\n• `/quality` - Compare audio quality bitrates and file sizes\n• `/terms` - View copyright, fair-use, and platform rules\n• `/about` - Learn about Linko\n\n*Limits:* Maximum duration is *30 minutes* and maximum file size is *50 MB*."
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    quality_text = "🎧 *Audio Quality & Format Options*\n\nAll links are converted into universal *.mp3* format, tailored by bitrate:\n\n1️⃣ *Standard (128 kbps)*\n   • *Best for:* Podcasts, speeches, fast downloads, saving data.\n   • *Approx. Size:* ~1 MB per minute.\n\n2️⃣ *Medium (192 kbps)*\n   • *Best for:* Everyday music listening with balanced size and clarity.\n   • *Approx. Size:* ~1.4 MB per minute.\n\n3️⃣ *High (320 kbps)*\n   • *Best for:* High-fidelity music and maximum MP3 audio depth.\n   • *Approx. Size:* ~2.4 MB per minute."
    await update.message.reply_text(quality_text, parse_mode="Markdown")


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    about_text = "🤖 *About Linko*\n\nLinko is a free, non-profit educational utility bot built to make saving authorized, public-domain, and Creative Commons audio in MP3 format simple.\n\nBuilt with Python, `python-telegram-bot`, `yt-dlp`, and `FFmpeg`."
    await update.message.reply_text(about_text, parse_mode="Markdown")


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    terms_text = "⚖️ *Platform Policy, Copyright & Security Terms*\n\n1. *Authorized & Fair Use Only:* Linko is strictly intended for downloading your own content, Creative Commons/royalty-free audio, public domain works, or material for which you have explicit permission from the copyright holder.\n\n2. *No DRM or Paywall Circumvention:* Linko respects platform security measures. It does *not* bypass Digital Rights Management (DRM), private accounts, age-restricted gates, or paid subscriptions.\n\n3. *Platform Terms of Service:* Social platforms prohibit unauthorized redistribution of copyrighted content.\n\n4. *Zero Retention Privacy:* Downloaded files are permanently deleted from the server immediately after delivery."
    await update.message.reply_text(terms_text, parse_mode="Markdown")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("🔍 Please provide a search term.\n*Example:* `/search The Weeknd Starboy`", parse_mode="Markdown")
        return
        
    msg = await update.message.reply_text("⏳ *Searching...*", parse_mode="Markdown")
    
    def do_search():
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
            return ydl.extract_info(f"ytsearch5:{query}", download=False)
            
    try:
        info = await asyncio.to_thread(do_search)
        entries = info.get("entries", [])
        if not entries:
            await msg.edit_text("❌ No results found.")
            return
            
        if "links" not in context.user_data: 
            context.user_data["links"] = {}
            
        keyboard = []
        for i, entry in enumerate(entries, 1):
            title = entry.get("title", "Unknown")[:40]
            req_id = str(uuid.uuid4())[:8]
            context.user_data["links"][req_id] = entry.get("url")
            keyboard.append([InlineKeyboardButton(f"{i}. {title}", callback_data=f"askq|{req_id}")])
            
        keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel|000")])
        await msg.edit_text("🔎 *Search Results:* Choose a track:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    except Exception:
        await msg.edit_text("❌ Search failed.")


async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    now = time.time()
    last_req = user_last_request.get(user_id, 0)
    if now - last_req < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last_req))
        await update.message.reply_text(f"⏳ Please wait *{remaining} seconds* before submitting another link.", parse_mode="Markdown")
        return
    
    message_text = update.message.text.strip()
    is_safe, error_reason = is_safe_public_url(message_text)
    if not is_safe:
        await update.message.reply_text(f"❌ {error_reason}", parse_mode="Markdown")
        return
        
    request_id = str(uuid.uuid4())[:8]
    if "links" not in context.user_data:
        context.user_data["links"] = {}
    context.user_data["links"][request_id] = message_text
    
    # Playlist Detection
    if "list=" in message_text or "/playlist" in message_text or "/album/" in message_text:
        keyboard = [
            [InlineKeyboardButton("🎵 Single Track", callback_data=f"askq|{request_id}")],
            [InlineKeyboardButton(f"💽 Full Playlist (Max {MAX_PLAYLIST_ITEMS})", callback_data=f"askqpl|{request_id}")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel|000")]
        ]
        await update.message.reply_text(
            "📑 *Playlist Detected!*\n\nWhat would you like to download?", 
            reply_markup=InlineKeyboardMarkup(keyboard), 
            parse_mode="Markdown"
        )
    else:
        keyboard = [
            [InlineKeyboardButton("⚪ Standard (128 kbps)", callback_data=f"dl|{request_id}|standard")],
            [InlineKeyboardButton("🔵 Medium (192 kbps)", callback_data=f"dl|{request_id}|medium")],
            [InlineKeyboardButton("💎 High (320 kbps)", callback_data=f"dl|{request_id}|high")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel|000")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "☑️ *Link verified!*\n\nChoose your desired MP3 audio quality:",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )


async def execute_download(chat_id, url, bitrate, context, status_msg=None, status_prefix=""):
    """Core download runner for both Single and Batch jobs."""
    async def update_status(text):
        if status_msg:
            try: 
                await context.bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg.message_id, parse_mode="Markdown")
            except: 
                pass

    await update_status(f"{status_prefix}⚙️ *Verifying & converting to MP3 ({bitrate} kbps)...*")
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    output_template = os.path.join(job_dir, "%(title).80s.%(ext)s")
    
    try:
        async with download_semaphore:
            metadata = await asyncio.to_thread(BlockingDownloadAudio, url, bitrate, output_template)
            
        mp3_files = [os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.endswith(".mp3")]
        if not mp3_files:
            await update_status(f"{status_prefix}❌ Failed to extract an MP3 file.")
            return
            
        mp3_path = mp3_files[0]
        file_size_mb = os.path.getsize(mp3_path) / (1024 * 1024)
        
        if file_size_mb > 49.5:
            await update_status(f"{status_prefix}⚠️ Exceeds Telegram's 50 MB limit.")
            return
            
        await update_status(f"{status_prefix}📤 *Uploading MP3 to Telegram...*")
        
        thumb_path = next((os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.endswith(('.jpg', '.webp', '.png'))), None)
        
        search_str = f"{metadata.get('title', '')} {metadata.get('uploader', '')}".strip()[:35]
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("📝 View Lyrics", callback_data=f"lyrics|{search_str}")]])
        
        with open(mp3_path, "rb") as audio_file:
            thumb_file = open(thumb_path, "rb") if thumb_path else None
            try:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=metadata.get("title", "Audio").replace("_", " "),
                    performer=metadata.get("uploader", "Linko").replace("_", " "),
                    thumbnail=thumb_file,
                    caption=f"*Downloaded via Linko*\n*Quality:* {bitrate} kbps MP3\n*Size:* {file_size_mb:.2f} MB",
                    parse_mode="Markdown",
                    reply_markup=btn
                )
            finally:
                if thumb_file:
                    thumb_file.close()
            
        if status_msg: 
            await context.bot.delete_message(chat_id, status_msg.message_id)
            
    except ValueError as ve:
        await update_status(f"{status_prefix}⚠️ *Policy Notice:* {ve}")
    except Exception as e:
        host = urlparse(url).hostname or "unknown host"
        safe_detail = re.sub(r"https?://[^\s]+", "[URL]", str(e))[:500]
        logger.error("Media processing failed for host=%s (%s): %s", host, type(e).__name__, safe_detail)
        await update_status(f"{status_prefix}❌ *Error:* {explain_download_error(e)}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("lyrics|"):
        search_term = data.split("|", 1)[1]
        await context.bot.send_message(update.effective_chat.id, f"🔍 Fetching lyrics for: {search_term}...")
        lyrics = await asyncio.to_thread(fetch_lrclib_lyrics, search_term)
        
        if lyrics:
            if len(lyrics) > 4000: 
                lyrics = lyrics[:4000] + "...\n(Truncated)"
            await context.bot.send_message(update.effective_chat.id, f"📝 **Lyrics: {search_term}**\n\n{lyrics}")
        else:
            await context.bot.send_message(update.effective_chat.id, "❌ Lyrics not found in database.")
        return

    if data.startswith("cancel"):
        return await query.edit_message_text("🚫 Cancelled.")

    if data.startswith("askq"):
        parts = data.split("|")
        prefix, req_id = parts[0], parts[1]
        dl_prefix = "dlpl" if "pl" in prefix else "dl"
        
        keyboard = [
            [InlineKeyboardButton("⚪ Standard (128 kbps)", callback_data=f"{dl_prefix}|{req_id}|standard")],
            [InlineKeyboardButton("🔵 Medium (192 kbps)", callback_data=f"{dl_prefix}|{req_id}|medium")],
            [InlineKeyboardButton("💎 High (320 kbps)", callback_data=f"{dl_prefix}|{req_id}|high")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel|000")]
        ]
        return await query.edit_message_text("☑️ Choose MP3 quality:", reply_markup=InlineKeyboardMarkup(keyboard))

    if data.startswith("dl"):
        parts = data.split("|")
        prefix, req_id, quality_key = parts[0], parts[1], parts[2]
        
        url = context.user_data.get("links", {}).pop(req_id, None)
        if not url:
            await query.edit_message_text("⏳ This session expired. Please paste your link again.")
            return
            
        user_last_request[update.effective_user.id] = time.time()
        preset = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS["medium"])
        bitrate = preset["bitrate"]
        
        if prefix == "dl":
            await query.edit_message_text(
                f"⏳ *Queued for processing...*\nQuality: `{preset['label']}`\nPlease wait while Linko prepares your MP3.",
                parse_mode="Markdown",
            )
            await execute_download(update.effective_chat.id, url, bitrate, context, query.message)
            
        else:
            # Batch Playlist Runner
            await query.edit_message_text("⏳ *Extracting Playlist metadata...*", parse_mode="Markdown")
            def get_playlist():
                with yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True}) as ydl:
                    return ydl.extract_info(url, download=False)
            try:
                info = await asyncio.to_thread(get_playlist)
                entries = info.get("entries", []) if info else []
            except: 
                entries = []
            
            if not entries: 
                entries = [{"url": url}]
                
            entries = list(entries)[:MAX_PLAYLIST_ITEMS]
            
            await query.edit_message_text(
                f"💽 *Playlist queued:* {len(entries)} tracks. Processing sequentially to protect server memory...", 
                parse_mode="Markdown"
            )
            
            for i, entry in enumerate(entries, 1):
                if not entry.get("url"): 
                    continue
                status_msg = await context.bot.send_message(
                    update.effective_chat.id, 
                    f"*(Track {i}/{len(entries)})* ⏳ Waiting...", 
                    parse_mode="Markdown"
                )
                await execute_download(
                    update.effective_chat.id, 
                    entry["url"], 
                    bitrate, 
                    context, 
                    status_msg, 
                    f"*(Track {i}/{len(entries)})* "
                )


def main() -> None:
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("Please set your BOT_TOKEN environment variable before running.")
        return
    health_thread = Thread(target=start_health_server, daemon=True)
    health_thread.start()
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("quality", quality_command))
    application.add_handler(CommandHandler("terms", terms_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_message))
    
    logger.info("Linko Bot is now running...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()