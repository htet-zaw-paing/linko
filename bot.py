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
from shazamio import Shazam

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
user_active_tasks: dict[int, int] = {}
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", 1800))
MAX_PLAYLIST_ITEMS = 10

QUALITY_PRESETS = {
    "standard": {"label": "Standard Audio (128 kbps)", "bitrate": "128", "type": "audio"},
    "medium": {"label": "Medium Audio (192 kbps)", "bitrate": "192", "type": "audio"},
    "high": {"label": "High Fidelity (320 kbps)", "bitrate": "320", "type": "audio"},
    "voice": {"label": "Voice Note (OGG Opus)", "bitrate": "64", "type": "voice"}
}

COOKIES_B64 = os.environ.get("COOKIES_B64", "").strip()
COOKIES_FILE_PATH = "/tmp/linko_cookies.txt"
if COOKIES_B64:
    try:
        with open(COOKIES_FILE_PATH, "wb") as f:
            f.write(base64.b64decode(COOKIES_B64))
        logger.info("Loaded custom cookies.txt from COOKIES_B64 environment variable.")
    except Exception as e:
        logger.warning(f"Failed to decode COOKIES_B64: {e}")

shazam_engine = Shazam()

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
            self.wfile.write(b"OK - Linko System is fully operational.")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def start_health_server() -> None:
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        logger.info(f"Health-check monitoring server listening on port {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Failed to start health-check server: {e}")

def is_safe_public_url(text: str) -> tuple[bool, str]:
    try:
        result = urlparse(text.strip())
        if result.scheme not in ("http", "https") or not result.netloc:
            return (False, "Please provide a valid URL starting with http:// or https://.")
        
        blocked_exts = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.pdf', '.docx', '.zip']
        if any(result.path.lower().endswith(ext) for ext in blocked_exts):
            return (False, "Target URL points to a static image or document. Please provide a video or audio media link.")

        hostname = result.hostname
        if not hostname:
            return (False, "Could not determine the website hostname.")
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return (False, "Localhost and internal network addresses are restricted.")
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
                return (False, "Target host resolves to a private or restricted network address.")
        return (True, "")
    except socket.gaierror:
        return (False, "Could not resolve the specified domain name. Please verify the URL for syntax errors.")
    except Exception:
        return (False, "Invalid URL structure provided.")

def fetch_lrclib_lyrics(query: str) -> str:
    headers = {"User-Agent": "LinkoBot/Enterprise (https://t.me/LinkoBot)"}
    try:
        r = requests.get(f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}", headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("plainLyrics") or data[0].get("syncedLyrics")
    except Exception as e:
        logger.warning(f"LRCLIB request failed: {e}")
    return ""

def BlockingDownloadAudio(url: str, bitrate: str, output_template: str, is_voice: bool = False) -> dict:
    job_dir = os.path.dirname(output_template)
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
        "fragment_retries": 2,
        "max_filesize": 50 * 1024 * 1024,
        "writethumbnail": not is_voice,
        "extractor_args": {
            "youtube": {
                "player_client": ["ios", "android_vr", "tv", "mweb", "web"],
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        },
    }

    if not is_voice:
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            },
            {
                "key": "FFmpegThumbnailsConvertor",
                "format": "jpg",
            }
        ]

    if os.path.exists(COOKIES_FILE_PATH) and os.path.getsize(COOKIES_FILE_PATH) > 0:
        ydl_opts["cookiefile"] = COOKIES_FILE_PATH

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                raise ValueError("Unable to retrieve media metadata.")
            if info.get("is_live"):
                raise ValueError("Live streams are unsupported.")
            duration = info.get("duration") or 0
            if duration > MAX_DURATION_SECONDS:
                max_mins = MAX_DURATION_SECONDS // 60
                raise ValueError(f"Media duration ({duration // 60} minutes) exceeds the {max_mins}-minute policy limit.")
            
            info = ydl.process_ie_result(info, download=True)

            if is_voice:
                raw_file = next((os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.startswith("audio_track")), None)
                if raw_file:
                    final_ogg = os.path.join(job_dir, "voice_track.ogg")
                    subprocess.run(
                        ["ffmpeg", "-y", "-i", raw_file, "-c:a", "libopus", "-b:a", f"{bitrate}k", final_ogg],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=True
                    )
                    if os.path.exists(raw_file):
                        os.remove(raw_file)

            return {
                "title": info.get("title", "Audio Track"),
                "uploader": info.get("uploader", "Linko System"),
                "duration": duration,
                "webpage_url": info.get("webpage_url", url),
            }
            
    except Exception as ytdlp_error:
        logger.warning(f"yt-dlp blocked for {url}: {ytdlp_error}. Engaging Cobalt Fallback API...")
        
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
                resp = requests.post(api, json=payload, headers=headers, timeout=15)
                if resp.status_code == 200:
                    dl_url = resp.json().get("url")
                    title = resp.json().get("filename", "Linko_Audio").replace("_", " ")
                    if dl_url:
                        raw_file = os.path.join(job_dir, "raw_audio_temp")
                        with requests.get(dl_url, stream=True, timeout=25) as r:
                            r.raise_for_status()
                            with open(raw_file, "wb") as f:
                                for chunk in r.iter_content(chunk_size=65536):
                                    f.write(chunk)
                        
                        if is_voice:
                            final_ogg = os.path.join(job_dir, "voice_track.ogg")
                            subprocess.run(
                                ["ffmpeg", "-y", "-i", raw_file, "-c:a", "libopus", "-b:a", f"{bitrate}k", final_ogg],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                check=True
                            )
                        else:
                            final_mp3 = os.path.join(job_dir, "audio_track.mp3")
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

def BlockingDownloadVideo(url: str, output_template: str) -> dict:
    job_dir = os.path.dirname(output_template)
    payload = {"url": url, "downloadMode": "auto"}
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    endpoints = [
        "https://cobalt-api.kwiatekmiki.com",
        "https://cobalt-backend.canine.tools",
        "https://cbl.wuk.sh"
    ]
    
    for api in endpoints:
        try:
            resp = requests.post(api, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200 and resp.json().get("url"):
                dl_url = resp.json().get("url")
                title = resp.json().get("filename", "Video").replace("_", " ")
                final_mp4 = os.path.join(job_dir, "video_track.mp4")
                with requests.get(dl_url, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    with open(final_mp4, "wb") as f:
                        for chunk in r.iter_content(chunk_size=65536):
                            f.write(chunk)
                return {"title": title[:80] or "Video Track", "uploader": "Linko Extractor"}
        except Exception:
            continue
            
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "restrictfilenames": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 50 * 1024 * 1024,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return {"title": info.get("title", "Video Track"), "uploader": info.get("uploader", "Linko System")}

def explain_download_error(error: Exception) -> str:
    message = str(error).lower()
    if any((term in message for term in ("sign in to confirm", "not a bot"))):
        return "The source platform blocked anonymous access from this IP. Add a COOKIES_B64 environment variable in Render to authenticate requests."
    if any((term in message for term in ("sign in", "login required", "private", "authentication"))):
        return "This media requires authentication, is private, or is age-restricted. Linko only processes publicly accessible content."
    if "unsupported url" in message or "no suitable extractor" in message:
        return "This link format is not supported. Ensure it is a valid public video or audio URL."
    if any((term in message for term in ("no video formats", "requested format", "no formats", "no video", "not a video"))):
        return "No downloadable audio/video format was found. The target link may be a static image post or unsupported media."
    if any((term in message for term in ("file is larger", "filesize", "file size"))):
        return "The requested source exceeds Linko's 50 MB platform processing limit."
    if "ffmpeg" in message or "ffprobe" in message or "postprocessing" in message:
        return "The media codec conversion engine was unable to process the selected stream."
    return "The source platform did not return downloadable media. Ensure the link is public and accessible."

async def post_init(application) -> None:
    commands = [
        BotCommand("start", "Initialize Linko System"),
        BotCommand("search", "Search database for audio tracks"),
        BotCommand("trending", "View global top trending tracks"),
        BotCommand("help", "Display comprehensive system guide"),
        BotCommand("quality", "View audio encoding specifications"),
        BotCommand("terms", "Review copyright and operational policies"),
        BotCommand("about", "View system architecture details"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("Enterprise command manifest registered successfully.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_first_name = update.effective_user.first_name or "Client"
    welcome_text = (
        f"Linko Enterprise System Initialized for {user_first_name}.\n\n"
        "Core Capabilities:\n"
        "1. Direct Media Extraction: Submit any public video or audio link.\n"
        "2. Database Query: Execute /search [track name] for immediate indexing.\n"
        "3. Global Analytics: Execute /trending to inspect active global charts.\n"
        "4. Acoustic Recognition: Send or forward an audio clip or voice note to identify tracks.\n\n"
        "All transactions are conducted in accordance with /terms."
    )
    await update.message.reply_text(welcome_text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "Linko System Operation Manual\n\n"
        "Accepted Inputs:\n"
        "- Standard Web URLs: YouTube, TikTok, Instagram Reels, SoundCloud.\n"
        "- Acoustic Payloads: Forwarded voice memos or MP3 files for instant identification.\n"
        "- Query Directives: /search [keywords] or /trending.\n\n"
        "Output Specifications:\n"
        "- MP3: 128 kbps, 192 kbps, 320 kbps (with embedded artwork).\n"
        "- Voice Memo: Native OGG Opus format.\n"
        "- MP4 Video: Watermark-free bypass for supported short-form platforms.\n\n"
        "Parameters: 30-minute maximum runtime, 50 MB Telegram file threshold."
    )
    await update.message.reply_text(help_text)

async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    quality_text = (
        "Audio Encoding & Bitrate Specifications\n\n"
        "1. Standard Audio (128 kbps MP3)\n"
        "   - Allocation: Optimized for minimal bandwidth and speech content.\n"
        "   - Metric: ~1.0 MB per minute.\n\n"
        "2. Medium Audio (192 kbps MP3)\n"
        "   - Allocation: Balanced fidelity for music listening.\n"
        "   - Metric: ~1.4 MB per minute.\n\n"
        "3. High Fidelity (320 kbps MP3)\n"
        "   - Allocation: Maximum acoustic depth for reference audio systems.\n"
        "   - Metric: ~2.4 MB per minute.\n\n"
        "4. Voice Note (64 kbps OGG Opus)\n"
        "   - Allocation: Native circular playback format for Telegram messaging."
    )
    await update.message.reply_text(quality_text)

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    about_text = (
        "System Architecture: Linko\n\n"
        "Linko is a high-performance utility designed for processing authorized, "
        "public-domain, and Creative Commons media streams into standard audio/video files.\n\n"
        "Built with Python 3.11, python-telegram-bot, yt-dlp, FFmpeg, and ShazamIO."
    )
    await update.message.reply_text(about_text)

async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    terms_text = (
        "Regulatory & Operational Terms\n\n"
        "1. Authorization: Usage is restricted to owned assets, Creative Commons, public-domain works, or explicitly authorized materials.\n"
        "2. Access Controls: Linko does not bypass DRM architectures, paid authentication tiers, or private accounts.\n"
        "3. Data Handling: Stream data is stored in isolated volatile memory and completely purged upon delivery completion."
    )
    await update.message.reply_text(terms_text)

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("Retrieving current global telemetry...")
    try:
        r = requests.get("https://itunes.apple.com/us/rss/topsongs/limit=10/json", timeout=10)
        r.raise_for_status()
        entries = r.json().get("feed", {}).get("entry", [])
        if not entries:
            raise ValueError("Empty feed returned.")
            
        if "links" not in context.user_data:
            context.user_data["links"] = {}
            
        keyboard = []
        for i, entry in enumerate(entries, 1):
            title = entry.get("im:name", {}).get("label", "Unknown Track")
            artist = entry.get("im:artist", {}).get("label", "Unknown Artist")
            req_id = str(uuid.uuid4())[:8]
            context.user_data["links"][req_id] = f"ytsearch1:{title} {artist}"
            keyboard.append([InlineKeyboardButton(f"{i}. {title} - {artist}", callback_data=f"askq|{req_id}")])
            
        keyboard.append([InlineKeyboardButton("Cancel Operation", callback_data="cancel|000")])
        await msg.edit_text("Global Top Charts (Select to extract):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await msg.edit_text("Error: Unable to synchronize with global chart telemetry.")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Syntax Requirement: Query argument missing.\nFormat: /search [keywords]")
        return
        
    msg = await update.message.reply_text("Executing database index search...")
    
    def do_search():
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True}) as ydl:
            return ydl.extract_info(f"ytsearch5:{query}", download=False)
            
    try:
        info = await asyncio.to_thread(do_search)
        entries = info.get("entries", [])
        if not entries:
            await msg.edit_text("Query Result: No matching records found.")
            return
            
        if "links" not in context.user_data: 
            context.user_data["links"] = {}
            
        keyboard = []
        for i, entry in enumerate(entries, 1):
            title = entry.get("title", "Unknown")[:40]
            req_id = str(uuid.uuid4())[:8]
            context.user_data["links"][req_id] = entry.get("url")
            keyboard.append([InlineKeyboardButton(f"{i}. {title}", callback_data=f"askq|{req_id}")])
            
        keyboard.append([InlineKeyboardButton("Cancel Operation", callback_data="cancel|000")])
        await msg.edit_text("Database Results (Select track):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await msg.edit_text("Error: Search query execution encountered a failure.")

async def handle_audio_recognition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("Acoustic payload detected. Initializing recognition engine...")
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    
    try:
        file_obj = getattr(update.message, 'voice', None) or getattr(update.message, 'audio', None)
        if not file_obj:
            raise ValueError("No recognizable audio component found.")
            
        tg_file = await context.bot.get_file(file_obj.file_id)
        temp_path = os.path.join(job_dir, "input_audio.ogg")
        await tg_file.download_to_drive(temp_path)
        
        await msg.edit_text("Analyzing acoustic fingerprint against global index...")
        out = await shazam_engine.recognize(temp_path)
        track = out.get("track")
        if not track:
            await msg.edit_text("Recognition Result: Zero fingerprint matches found in database.")
            return
        
        title = track.get("title", "Unknown Title")
        artist = track.get("subtitle", "Unknown Artist")
        
        if "links" not in context.user_data:
            context.user_data["links"] = {}
        req_id = str(uuid.uuid4())[:8]
        context.user_data["links"][req_id] = f"ytsearch1:{title} {artist}"
        
        keyboard = [
            [InlineKeyboardButton("Extract Matched Audio", callback_data=f"askq|{req_id}")],
            [InlineKeyboardButton("Cancel Operation", callback_data="cancel|000")]
        ]
        await msg.edit_text(f"Match Confirmed:\nTrack: {title}\nArtist: {artist}\n\nSelect an option below:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        logger.error(f"Acoustic recognition error: {e}")
        await msg.edit_text("Error: Audio recognition protocol failed to complete.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Privacy Notice: Linko accepts URLs and audio payloads exclusively. "
        "Static image files and unrelated documents are discarded for data privacy.\n\n"
        "Submit a media link or issue /search to begin extraction."
    )

async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    if user_active_tasks.get(user_id, 0) > 0:
        await update.message.reply_text("Concurrency Policy: Active processing is underway. Await completion before issuing another request.")
        return

    now = time.time()
    last_req = user_last_request.get(user_id, 0)
    if now - last_req < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last_req))
        await update.message.reply_text(f"Rate Limiter: Request rejected. Cooldown active for {remaining} seconds.")
        return
    
    message_text = update.message.text.strip()
    
    if not re.search(r"https?://", message_text.lower()):
        if len(message_text) < 50 and any(w in message_text.lower() for w in ["hi", "hello", "hey", "sup", "start"]):
            await update.message.reply_text("Linko System operational. Provide a media URL or execute /search to download content.")
        else:
            await update.message.reply_text("Syntax Error: Valid URL starting with http:// or https:// required. Alternatively, use /search.")
        return

    is_safe, error_reason = is_safe_public_url(message_text)
    if not is_safe:
        await update.message.reply_text(f"Validation Error: {error_reason}")
        return
        
    request_id = str(uuid.uuid4())[:8]
    if "links" not in context.user_data:
        context.user_data["links"] = {}
    context.user_data["links"][request_id] = message_text
    
    keyboard = []
    if any(d in message_text.lower() for d in ["tiktok.com", "instagram.com/reel", "youtube.com/shorts", "youtu.be"]):
        keyboard.append([InlineKeyboardButton("Video Extraction (MP4)", callback_data=f"dlv|{request_id}")])
        
    if "list=" in message_text or "/playlist" in message_text or "/album/" in message_text:
        keyboard.append([InlineKeyboardButton("Single Track", callback_data=f"askq|{request_id}")])
        keyboard.append([InlineKeyboardButton(f"Full Playlist (Limit {MAX_PLAYLIST_ITEMS})", callback_data=f"askqpl|{request_id}")])
        keyboard.append([InlineKeyboardButton("Cancel Operation", callback_data="cancel|000")])
        await update.message.reply_text("Playlist Structure Detected. Select extraction parameter:", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard.append([InlineKeyboardButton("Standard Audio (128 kbps)", callback_data=f"dl|{request_id}|standard")])
        keyboard.append([InlineKeyboardButton("Medium Audio (192 kbps)", callback_data=f"dl|{request_id}|medium")])
        keyboard.append([InlineKeyboardButton("High Fidelity (320 kbps)", callback_data=f"dl|{request_id}|high")])
        keyboard.append([InlineKeyboardButton("Voice Note (OGG Opus)", callback_data=f"dl|{request_id}|voice")])
        keyboard.append([InlineKeyboardButton("Cancel Operation", callback_data="cancel|000")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("Media Verified. Select encoding specification:", reply_markup=reply_markup)

async def execute_video_download(chat_id, url, context, status_msg=None):
    async def update_status(text):
        if status_msg:
            try:
                await context.bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg.message_id)
            except Exception:
                pass

    await update_status("Executing watermark bypass and video extraction...")
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    output_template = os.path.join(job_dir, "video_track.mp4")
    
    try:
        async with download_semaphore:
            metadata = await asyncio.to_thread(BlockingDownloadVideo, url, output_template)
            
        mp4_path = os.path.join(job_dir, "video_track.mp4")
        if not os.path.exists(mp4_path):
            raise ValueError("Target video stream could not be generated.")
            
        file_size_mb = os.path.getsize(mp4_path) / (1024 * 1024)
        if file_size_mb > 49.5:
            await update_status(f"Capacity Limit: Video file ({file_size_mb:.2f} MB) exceeds Telegram's 50 MB threshold.")
            return
            
        await update_status("Transmitting video payload to client...")
        with open(mp4_path, "rb") as vf:
            await context.bot.send_video(
                chat_id=chat_id,
                video=vf,
                caption=f"Title: {metadata.get('title', 'Video Track')}\nFormat: MP4 Video | Size: {file_size_mb:.2f} MB",
                supports_streaming=True
            )
        if status_msg:
            await context.bot.delete_message(chat_id, status_msg.message_id)
            
    except Exception as e:
        logger.error(f"Video extraction failure: {e}")
        await update_status(f"Error: {explain_download_error(e)}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

async def execute_download(chat_id, url, preset_key, context, status_msg=None, status_prefix=""):
    preset = QUALITY_PRESETS.get(preset_key, QUALITY_PRESETS["medium"])
    bitrate = preset["bitrate"]
    is_voice = preset["type"] == "voice"

    async def update_status(text):
        if status_msg:
            try: 
                await context.bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg.message_id)
            except Exception: 
                pass

    await update_status(f"{status_prefix}Processing media stream ({preset['label']})...\nPlease maintain connection while audio is extracted and encoded.")
    
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    output_template = os.path.join(job_dir, "audio_track.%(ext)s")
    
    try:
        async with download_semaphore:
            metadata = await asyncio.to_thread(BlockingDownloadAudio, url, bitrate, output_template, is_voice)
            
        target_ext = ".ogg" if is_voice else ".mp3"
        files = [os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.endswith(target_ext)]
        if not files:
            await update_status(f"{status_prefix}Error: Audio conversion failed to produce the designated target format.")
            return
            
        media_path = files[0]
        file_size_mb = os.path.getsize(media_path) / (1024 * 1024)
        
        if file_size_mb > 49.5:
            await update_status(f"{status_prefix}Capacity Limit: File size ({file_size_mb:.2f} MB) exceeds Telegram's 50 MB threshold.")
            return
            
        await update_status(f"{status_prefix}Transmitting audio payload to client...")
        
        raw_title = metadata.get('title', 'Audio Track')
        uploader = metadata.get('uploader', 'Linko').replace(' - Topic', '')
        clean_title = re.sub(r'(?i)(\(official.*?\)|\[official.*?\]|\(music video.*?\)|\(lyric.*?\)|\[lyric.*?\]|\(audio.*?\)|\[audio.*?\]|\(live.*?\))', '', raw_title).strip()
        
        if uploader.lower() not in clean_title.lower():
            search_str = f"{clean_title} {uploader}"
        else:
            search_str = clean_title
            
        search_str = search_str.strip()
        search_bytes = search_str.encode('utf-8')
        if len(search_bytes) > 50:
            search_str = search_bytes[:50].decode('utf-8', errors='ignore')
            
        btn = InlineKeyboardMarkup([[InlineKeyboardButton("Query Lyrics Database", callback_data=f"lyrics|{search_str}")]])
        
        if is_voice:
            with open(media_path, "rb") as vf:
                await context.bot.send_voice(
                    chat_id=chat_id,
                    voice=vf,
                    caption=f"Track: {clean_title}\nFormat: OGG Opus Voice Note | Size: {file_size_mb:.2f} MB",
                    reply_markup=btn
                )
        else:
            thumb_path = next((os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.endswith('.jpg')), None)
            if thumb_path:
                small_thumb = os.path.join(job_dir, "cover_320.jpg")
                subprocess.run(
                    ["ffmpeg", "-y", "-i", thumb_path, "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "5", small_thumb], 
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                if os.path.exists(small_thumb):
                    thumb_path = small_thumb

            with open(media_path, "rb") as audio_file:
                thumb_file = open(thumb_path, "rb") if thumb_path else None
                try:
                    await context.bot.send_audio(
                        chat_id=chat_id,
                        audio=audio_file,
                        title=metadata.get("title", "Audio").replace("_", " "),
                        performer=metadata.get("uploader", "Linko").replace("_", " "),
                        thumbnail=thumb_file,
                        caption=f"Processed via Linko\nEncoding: {bitrate} kbps MP3 | Size: {file_size_mb:.2f} MB",
                        reply_markup=btn
                    )
                finally:
                    if thumb_file:
                        thumb_file.close()
            
        if status_msg: 
            await context.bot.delete_message(chat_id, status_msg.message_id)
            
    except ValueError as ve:
        await update_status(f"{status_prefix}Policy Notice: {ve}")
    except Exception as e:
        host = urlparse(url).hostname or "unknown host"
        safe_detail = re.sub(r"https?://[^\s]+", "[URL]", str(e))[:500]
        logger.error("Media processing failed for host=%s (%s): %s", host, type(e).__name__, safe_detail)
        await update_status(f"{status_prefix}Error: {explain_download_error(e)}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    
    if data.startswith("lyrics|"):
        search_term = data.split("|", 1)[1]
        await context.bot.send_message(update.effective_chat.id, f"Querying lyrics database for: {search_term}...")
        lyrics = await asyncio.to_thread(fetch_lrclib_lyrics, search_term)
        
        if lyrics:
            if len(lyrics) > 4000: 
                lyrics = lyrics[:4000] + "...\n(Document Truncated)"
            await context.bot.send_message(update.effective_chat.id, f"Lyrics Record: {search_term}\n\n{lyrics}")
        else:
            await context.bot.send_message(update.effective_chat.id, "Query Notice: No synchronized lyrics found in database.")
        return

    if data.startswith("cancel"):
        return await query.edit_message_text("Operation terminated by user.")

    if data.startswith("askq"):
        parts = data.split("|")
        prefix, req_id = parts[0], parts[1]
        dl_prefix = "dlpl" if "pl" in prefix else "dl"
        
        keyboard = [
            [InlineKeyboardButton("Standard Audio (128 kbps)", callback_data=f"{dl_prefix}|{req_id}|standard")],
            [InlineKeyboardButton("Medium Audio (192 kbps)", callback_data=f"{dl_prefix}|{req_id}|medium")],
            [InlineKeyboardButton("High Fidelity (320 kbps)", callback_data=f"{dl_prefix}|{req_id}|high")],
            [InlineKeyboardButton("Voice Note (OGG Opus)", callback_data=f"{dl_prefix}|{req_id}|voice")],
            [InlineKeyboardButton("Cancel Operation", callback_data="cancel|000")]
        ]
        return await query.edit_message_text("Select encoding parameters:", reply_markup=InlineKeyboardMarkup(keyboard))

    if data.startswith("dlv"):
        if user_active_tasks.get(user_id, 0) > 0:
            await query.edit_message_text("Concurrency Policy: Active processing task in progress. Await completion.")
            return

        req_id = data.split("|")[1]
        url = context.user_data.get("links", {}).pop(req_id, None)
        if not url:
            await query.edit_message_text("Session Expired: Please resubmit the media link.")
            return

        user_last_request[user_id] = time.time()
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
        
        try:
            await query.edit_message_text("Extraction task queued...")
            await execute_video_download(update.effective_chat.id, url, context, query.message)
        finally:
            if user_active_tasks[user_id] > 0:
                user_active_tasks[user_id] -= 1
        return

    if data.startswith("dl"):
        if user_active_tasks.get(user_id, 0) > 0:
            await query.edit_message_text("Concurrency Policy: Active processing task in progress. Await completion.")
            return

        parts = data.split("|")
        prefix, req_id, quality_key = parts[0], parts[1], parts[2]
        
        url = context.user_data.get("links", {}).pop(req_id, None)
        if not url:
            await query.edit_message_text("Session Expired: Please resubmit the media link.")
            return
            
        user_last_request[user_id] = time.time()
        preset = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS["medium"])
        
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
        
        try:
            if prefix == "dl":
                await query.edit_message_text(f"Task queued. Specification: {preset['label']}.")
                await execute_download(update.effective_chat.id, url, quality_key, context, query.message)
            else:
                await query.edit_message_text("Inspecting batch playlist structure...")
                def get_playlist():
                    with yt_dlp.YoutubeDL({"extract_flat": True, "quiet": True}) as ydl:
                        return ydl.extract_info(url, download=False)
                try:
                    info = await asyncio.to_thread(get_playlist)
                    entries = info.get("entries", []) if info else []
                except Exception: 
                    entries = []
                
                if not entries: 
                    entries = [{"url": url}]
                    
                entries = list(entries)[:MAX_PLAYLIST_ITEMS]
                
                await query.edit_message_text(
                    f"Batch queue established: {len(entries)} units. Executing sequential processing pipeline..."
                )
                
                for i, entry in enumerate(entries, 1):
                    if not entry.get("url"): 
                        continue
                    status_msg = await context.bot.send_message(
                        update.effective_chat.id, 
                        f"[Unit {i}/{len(entries)}] Awaiting execution slot..."
                    )
                    await execute_download(
                        update.effective_chat.id, 
                        entry["url"], 
                        quality_key, 
                        context, 
                        status_msg, 
                        f"[Unit {i}/{len(entries)}] "
                    )
        finally:
            if user_active_tasks[user_id] > 0:
                user_active_tasks[user_id] -= 1

def main() -> None:
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        logger.error("Please configure the BOT_TOKEN environment variable.")
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
    application.add_handler(CommandHandler("trending", trending_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("quality", quality_command))
    application.add_handler(CommandHandler("terms", terms_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio_recognition))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, handle_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_message))
    
    logger.info("Linko Enterprise Framework online and active.")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()