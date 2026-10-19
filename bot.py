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
import gc
import concurrent.futures
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
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

FEEDBACK_URL = os.environ.get("FEEDBACK_URL", "https://forms.gle/4u6R2iG8vFj5A7Q77")
KBZPAY_QR_URL = os.environ.get("KBZPAY_QR_URL", "")

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 2))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
USER_COOLDOWN_SECONDS = int(os.environ.get("USER_COOLDOWN_SECONDS", 5))
user_last_request: dict[int, float] = {}
user_active_tasks: dict[int, int] = {}
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", 1800))
MAX_PLAYLIST_ITEMS = 10

MAX_QUEUE_CAPACITY = 100
current_in_queue = 0
queue_counter_lock = asyncio.Lock()

GLOBAL_SESSION = {}
FILE_ID_CACHE: dict[str, dict] = {}

COBALT_ENDPOINTS = [
    "https://api.cobalt.rpkiinval.id",
    "https://rue-cobalt.xenon.zone",
    "https://cobaltapi.cjs.nz",
    "https://cobalt-alpha.wolfy.love",
    "https://api.qwkuns.me",
    "https://nuko-c.meowing.de",
    "https://cobalt-omega.wolfy.love",
]

QUALITY_PRESETS = {
    "standard": {"label": "Standard Audio (MP3)", "bitrate": "128", "type": "audio"},
    "medium": {"label": "Medium Audio (MP3)", "bitrate": "192", "type": "audio"},
    "high": {"label": "High Audio (MP3)", "bitrate": "320", "type": "audio"},
    "voice": {"label": "Voice Message (OGG)", "bitrate": "64", "type": "voice"},
}

COOKIES_B64 = os.environ.get("COOKIES_B64", "").strip()
COOKIES_FILE_PATH = "/tmp/linko_cookies.txt"
if COOKIES_B64:
    try:
        with open(COOKIES_FILE_PATH, "wb") as f:
            f.write(base64.b64decode(COOKIES_B64))
    except Exception:
        pass

shazam_engine = Shazam()

# Persistent HTTP Client with Connection Pooling
http_session = requests.Session()
retries = Retry(total=1, backoff_factor=0.2, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=retries)
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)


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
            self.wfile.write(b"OK - Linko System is running.")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_health_server() -> None:
    try:
        server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
        server.serve_forever()
    except Exception:
        pass


def is_safe_public_url(text: str) -> tuple[bool, str]:
    try:
        result = urlparse(text.strip())
        if result.scheme not in ("http", "https") or not result.netloc:
            return (False, "Please send a valid link starting with http:// or https://")

        blocked_exts = [
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
            ".pdf",
            ".docx",
            ".zip",
        ]
        if any(result.path.lower().endswith(ext) for ext in blocked_exts):
            return (
                False,
                "This link points to an image or document. Please send a video or audio link.",
            )

        hostname = result.hostname
        if not hostname:
            return (False, "Could not read the website link.")
        if hostname.lower() in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
            return (False, "Local network links are not allowed.")
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
                return (
                    False,
                    "This link goes to a private network and cannot be downloaded.",
                )
        return (True, "")
    except socket.gaierror:
        return (False, "Could not find that website. Please check the link for typos.")
    except Exception:
        return (False, "The link format is invalid.")


def fetch_lrclib_lyrics(query: str) -> str:
    headers = {"User-Agent": "LinkoBot (https://t.me/LinkoBot)"}
    try:
        r = http_session.get(
            f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}",
            headers=headers,
            timeout=5,
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("plainLyrics") or data[0].get("syncedLyrics") or ""
    except Exception:
        pass
    return ""


def parse_time_to_seconds(t_str: str) -> int:
    parts = t_str.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return int(t_str)


def _check_single_cobalt(api: str, payload: dict, is_audio: bool = True) -> dict | None:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    try:
        resp = http_session.post(api, json=payload, headers=headers, timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            dl_url = data.get("url")
            if not dl_url:
                return None
            title = data.get("filename", "Audio Track" if is_audio else "Video")
            ext = ".mp3" if is_audio else ".mp4"
            if title.lower().endswith(ext):
                title = title[: -len(ext)]
            title = title.replace("_", " ")

            try:
                head_resp = http_session.head(dl_url, timeout=3, allow_redirects=True)
                size = int(head_resp.headers.get("Content-Length", 0))
                if size > 19.5 * 1024 * 1024:
                    return None
            except Exception:
                pass

            return {
                "title": title[:80] or ("Audio Track" if is_audio else "Video"),
                "uploader": "Linko",
                "duration": 0,
                "webpage_url": payload.get("url", ""),
                "direct_url": dl_url,
            }
    except Exception:
        return None
    return None


def FastCheckAudioBypass(url: str, bitrate: str) -> dict | None:
    if url.startswith("ytsearch"):
        return None
    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "mp3",
        "audioBitrate": bitrate,
    }

    # Parallel endpoint racing: first valid response returned immediately
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(COBALT_ENDPOINTS)) as executor:
        futures = [
            executor.submit(_check_single_cobalt, api, payload, True)
            for api in COBALT_ENDPOINTS
        ]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                executor.shutdown(wait=False, cancel_futures=True)
                return res
    return None


def FastCheckVideoBypass(url: str) -> dict | None:
    if url.startswith("ytsearch"):
        return None
    payload = {"url": url, "downloadMode": "auto"}

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(COBALT_ENDPOINTS)) as executor:
        futures = [
            executor.submit(_check_single_cobalt, api, payload, False)
            for api in COBALT_ENDPOINTS
        ]
        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            if res:
                executor.shutdown(wait=False, cancel_futures=True)
                return res
    return None


def run_ytdlp_with_fallback(
    ydl_opts: dict, url: str, start_sec: int = None, end_sec: int = None
) -> tuple[dict, str, str]:
    has_cookies = (
        os.path.exists(COOKIES_FILE_PATH) and os.path.getsize(COOKIES_FILE_PATH) > 0
    )
    attempts = [False, True] if not has_cookies else [True, False]
    last_err = None

    for use_cookie in attempts:
        opts = dict(ydl_opts)
        if use_cookie and has_cookies:
            opts["cookiefile"] = COOKIES_FILE_PATH
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["tv_embedded", "android_vr", "ios", "web"],
                }
            }
        else:
            opts.pop("cookiefile", None)
            opts["extractor_args"] = {
                "youtube": {
                    "player_client": ["tv_embedded", "android_vr", "ios"],
                }
            }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    raise ValueError("Could not read media information.")
                if "entries" in info:
                    entries = list(info.get("entries") or [])
                    if not entries:
                        raise ValueError("No results found.")
                    info = entries[0]
                if info.get("is_live"):
                    raise ValueError("Live streams cannot be downloaded.")

                duration = info.get("duration") or 0
                if start_sec is not None and end_sec is not None:
                    clip_duration = end_sec - start_sec
                    if clip_duration > MAX_DURATION_SECONDS:
                        raise ValueError(
                            f"Clip cannot be longer than {MAX_DURATION_SECONDS // 60} minutes."
                        )
                elif duration > MAX_DURATION_SECONDS:
                    raise ValueError(
                        f"Media exceeds {MAX_DURATION_SECONDS // 60}-minute limit."
                    )

                info = ydl.process_ie_result(info, download=True)
                return info, info.get("title", "Media"), info.get("uploader", "Linko")
        except ValueError as ve:
            raise ve
        except Exception as e:
            last_err = e
            continue

    raise last_err


def BlockingDownloadAudio(
    url: str,
    bitrate: str,
    output_template: str,
    is_voice: bool = False,
    start_sec: int = None,
    end_sec: int = None,
) -> dict:
    job_dir = os.path.dirname(output_template)

    if start_sec is None and not url.startswith("ytsearch"):
        payload = {
            "url": url,
            "downloadMode": "audio",
            "audioFormat": "mp3",
            "audioBitrate": bitrate,
        }
        res = FastCheckAudioBypass(url, bitrate)
        if res and res.get("direct_url"):
            dl_url = res["direct_url"]
            title = res["title"]
            raw_file = os.path.join(job_dir, "raw_audio_temp")
            with http_session.get(dl_url, stream=True, timeout=20) as r:
                r.raise_for_status()
                with open(raw_file, "wb") as f:
                    for chunk in r.iter_content(chunk_size=131072):
                        f.write(chunk)

            if is_voice:
                final_ogg = os.path.join(job_dir, "voice_track.ogg")
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-threads",
                        "0",
                        "-i",
                        raw_file,
                        "-c:a",
                        "libopus",
                        "-b:a",
                        f"{bitrate}k",
                        "-application",
                        "voip",
                        "-compression_level",
                        "0",
                        final_ogg,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )
            else:
                final_mp3 = os.path.join(job_dir, "audio_track.mp3")
                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-threads",
                        "0",
                        "-i",
                        raw_file,
                        "-vn",
                        "-ar",
                        "44100",
                        "-ac",
                        "2",
                        "-b:a",
                        f"{bitrate}k",
                        final_mp3,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                )

            return {
                "title": title[:80] or "Audio Track",
                "uploader": "Linko",
                "duration": 0,
                "webpage_url": url,
            }

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "socket_timeout": 15,
        "retries": 2,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 16,
        "http_chunk_size": 10485760,
        "max_filesize": 50 * 1024 * 1024,
        "writethumbnail": not is_voice,
        "postprocessor_args": {"ffmpeg": ["-threads", "0"]},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
    }

    if start_sec is not None and end_sec is not None:
        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(
            None, [(start_sec, end_sec)]
        )
        ydl_opts["force_keyframes_at_cuts"] = True

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
            },
        ]

    info, title, uploader = run_ytdlp_with_fallback(ydl_opts, url, start_sec, end_sec)

    if is_voice:
        raw_file = next(
            (
                os.path.join(job_dir, f)
                for f in os.listdir(job_dir)
                if f.startswith("audio_track")
            ),
            None,
        )
        if raw_file:
            final_ogg = os.path.join(job_dir, "voice_track.ogg")
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-threads",
                    "0",
                    "-i",
                    raw_file,
                    "-c:a",
                    "libopus",
                    "-b:a",
                    f"{bitrate}k",
                    "-application",
                    "voip",
                    "-compression_level",
                    "0",
                    final_ogg,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
            )
            if os.path.exists(raw_file):
                os.remove(raw_file)

    return {
        "title": title,
        "uploader": uploader,
        "duration": info.get("duration") or 0,
        "webpage_url": info.get("webpage_url", url),
    }


def BlockingDownloadVideo(
    url: str, output_template: str, start_sec: int = None, end_sec: int = None
) -> dict:
    job_dir = os.path.dirname(output_template)

    if start_sec is None and not url.startswith("ytsearch"):
        res = FastCheckVideoBypass(url)
        if res and res.get("direct_url"):
            dl_url = res["direct_url"]
            title = res["title"]
            final_mp4 = os.path.join(job_dir, "video_track.mp4")
            with http_session.get(dl_url, stream=True, timeout=25) as r:
                r.raise_for_status()
                with open(final_mp4, "wb") as f:
                    for chunk in r.iter_content(chunk_size=131072):
                        f.write(chunk)
            return {"title": title[:80] or "Video", "uploader": "Linko"}

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "socket_timeout": 15,
        "concurrent_fragment_downloads": 4,
        "buffersize": 1024 * 16,
        "http_chunk_size": 10485760,
        "max_filesize": 50 * 1024 * 1024,
        "postprocessor_args": {"ffmpeg": ["-threads", "0"]},
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
        },
    }

    if start_sec is not None and end_sec is not None:
        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(
            None, [(start_sec, end_sec)]
        )
        ydl_opts["force_keyframes_at_cuts"] = True

    _, title, uploader = run_ytdlp_with_fallback(ydl_opts, url, start_sec, end_sec)
    return {"title": title, "uploader": uploader}


def explain_download_error(error: Exception) -> str:
    message = str(error).lower()
    if any(term in message for term in ("sign in to confirm", "not a bot")):
        return "Access temporarily blocked by the platform. Updated cookies needed."
    if any(
        term in message
        for term in ("sign in", "login required", "private", "authentication")
    ):
        return "This media is private or login-restricted. Linko only works with public links."
    if "unsupported url" in message or "no suitable extractor" in message:
        return "Link format not supported. Send a valid public video or audio link."
    if any(
        term in message
        for term in (
            "no video formats",
            "requested format",
            "no formats",
            "no video",
            "not a video",
        )
    ):
        return "No downloadable stream found for this link."
    if any(term in message for term in ("file is larger", "filesize", "file size")):
        return "The requested file exceeds Telegram's 50 MB bot limit."
    if "ffmpeg" in message or "ffprobe" in message or "postprocessing" in message:
        return "Audio/video conversion failed on this media format."
    return "Could not download media. Verify the link is public and active."


def build_audio_buttons(
    raw_title: str, uploader: str
) -> tuple[InlineKeyboardMarkup, str]:
    uploader_clean = uploader.replace(" - Topic", "")
    clean_title = re.sub(
        r"(?i)(\(official.*?\)|\[official.*?\]|\(music video.*?\)|\(lyric.*?\)|\[lyric.*?\]|\(audio.*?\)|\[audio.*?\]|\(live.*?\))",
        "",
        raw_title,
    ).strip()
    if (
        uploader_clean.lower() not in clean_title.lower()
        and uploader_clean.lower() != "linko"
    ):
        search_str = f"{clean_title} {uploader_clean}"
    else:
        search_str = clean_title
    search_str = search_str.strip()
    search_bytes = search_str.encode("utf-8")
    if len(search_bytes) > 50:
        search_str = search_bytes[:50].decode("utf-8", errors="ignore")
    action_buttons = []
    if search_str:
        action_buttons.append(
            [InlineKeyboardButton("📝 View Lyrics", callback_data=f"lyrics|{search_str}")]
        )
    action_buttons.append(
        [
            InlineKeyboardButton("☕ Buy Me a Coffee", callback_data="donate"),
            InlineKeyboardButton("💬 Feedback", url=FEEDBACK_URL),
        ]
    )
    return InlineKeyboardMarkup(action_buttons), clean_title


async def post_init(application) -> None:
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("search", "Search songs"),
        BotCommand("clip", "Clip audio/video"),
        BotCommand("trending", "Trending charts"),
        BotCommand("help", "Usage guide"),
        BotCommand("quality", "Quality options"),
        BotCommand("donate", "Support Linko"),
        BotCommand("feedback", "Send feedback"),
        BotCommand("terms", "Rules & terms"),
        BotCommand("about", "About Linko"),
    ]
    await application.bot.set_my_commands(commands)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    user_id = update.effective_user.id

    if args:
        payload = args[0]
        if payload.startswith("dli_") or payload.startswith("dlvi_"):
            if user_active_tasks.get(user_id, 0) > 0:
                await update.message.reply_text("Please wait, your previous request is still running.")
                return

            parts = payload.split("_")
            is_video = payload.startswith("dlvi_")
            req_id = parts[1] if is_video else parts[2]
            quality_key = "high" if is_video else parts[1]

            session = GLOBAL_SESSION.pop(req_id, None)
            if not session:
                await update.message.reply_text("This link expired. Please search again.")
                return

            url = session.get("url")
            user_last_request[user_id] = time.time()
            user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1

            try:
                status_msg = await update.message.reply_text("⚡ Processing download...")
                if is_video:
                    await execute_video_download(
                        update.effective_chat.id, url, context, status_msg
                    )
                else:
                    await execute_download(
                        update.effective_chat.id,
                        url,
                        quality_key,
                        context,
                        status_msg,
                        "",
                    )
            finally:
                if user_active_tasks[user_id] > 0:
                    user_active_tasks[user_id] -= 1
            return

    user_first_name = update.effective_user.first_name or "there"
    welcome_text = (
        f"<b>Welcome to Linko, {user_first_name}!</b> ⚡\n\n"
        "<b>1. Fast Downloader</b>\n"
        "Send any public video or audio link.\n\n"
        "<b>2. Smart Clipper</b>\n"
        "Type /clip [link] 01:15-01:45 to trim media.\n\n"
        "<b>3. Inline Search</b>\n"
        "Type @Linko4UBot in any chat to share songs directly.\n\n"
        "<b>4. Search & Trending</b>\n"
        "Type /search [song] or /trending.\n\n"
        "<b>5. Song Finder</b>\n"
        "Forward any voice note or audio file to identify it.\n\n"
        "<b>Guides:</b>\n"
        "• <a href='https://telegra.ph/Linko---User-Guide-10-17'>English Guide</a>\n"
        "• <a href='https://telegra.ph/Linko---User-Guide-MM-10-17'>မြန်မာလမ်းညွှန်</a>\n\n"
        "<i>Read our /terms for fair use details.</i>"
    )
    await update.message.reply_text(
        welcome_text, parse_mode="HTML", disable_web_page_preview=True
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "⚡ Linko Quick Help Guide\n\n"
        "Commands & Usage:\n"
        "• Send any URL (YouTube, TikTok, Reels, etc.)\n"
        "• /clip [link] [start]-[end] to cut parts\n"
        "• @Linko4UBot [query] for inline song lookup\n"
        "• Send a voice note or audio to recognize the track\n"
        "• /search [song name] or /trending\n\n"
        "Audio Formats:\n"
        "• Standard Audio (128 kbps MP3)\n"
        "• Medium Audio (192 kbps MP3)\n"
        "• High Audio (320 kbps MP3)\n"
        "• Voice Message (OGG Opus)\n"
        "• Standard Video (MP4)\n\n"
        "Limits: Up to 30 mins, 50 MB max per file."
    )
    await update.message.reply_text(help_text)


async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    quality_text = (
        "Audio Quality Presets\n\n"
        "1. Standard Audio (MP3 - 128 kbps)\n"
        "   - Fastest download, lowest bandwidth.\n"
        "   - ~1.0 MB / min.\n\n"
        "2. Medium Audio (MP3 - 192 kbps)\n"
        "   - Balanced everyday sound.\n"
        "   - ~1.4 MB / min.\n\n"
        "3. High Audio (MP3 - 320 kbps)\n"
        "   - Maximum fidelity for earphones and speakers.\n"
        "   - ~2.4 MB / min.\n\n"
        "4. Voice Message (OGG Opus)\n"
        "   - Formatted natively as Telegram voice message."
    )
    await update.message.reply_text(quality_text)


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    about_text = (
        "<b>About Linko</b>\n\n"
        "Linko is a high-speed Telegram media engine built for instant public audio and video downloads, smart audio clipping, and music recognition.\n\n"
        "Powered by yt-dlp, FFmpeg, and ShazamIO.\n\n"
        "Developed and maintained by <a href='https://www.htetzawpaing.site'>Htet Zaw Paing</a>.\n"
        "A project by <a href='https://www.myancode.com'>MyanCode</a>."
    )
    await update.message.reply_text(about_text, parse_mode="HTML")


async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    terms_text = (
        "Terms & Fair Use\n\n"
        "1. Only download content you own or have permission to use.\n"
        "2. Private, DRM, or age-restricted content cannot be retrieved.\n"
        "3. All processed files are permanently purged right after delivery."
    )
    await update.message.reply_text(terms_text)


async def send_donation_info(chat_id, context: ContextTypes.DEFAULT_TYPE) -> None:
    donate_caption = (
        "☕ Thank you for supporting Linko!\n\n"
        "Your support keeps Linko super fast, maintained, and running 24/7 without delays.\n\n"
        "You can buy me a coffee by scanning the KBZPay QR code below."
    )
    if KBZPAY_QR_URL:
        try:
            await context.bot.send_photo(
                chat_id=chat_id, photo=KBZPAY_QR_URL, caption=donate_caption
            )
            return
        except Exception as e:
            logger.error(f"Donation QR failed to send: {e}")
    await context.bot.send_message(chat_id=chat_id, text=donate_caption)


async def donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_donation_info(update.effective_chat.id, context)


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton("💬 Open Feedback Form", url=FEEDBACK_URL)]]
    await update.message.reply_text(
        "Have suggestions, improvements, or found a bug?\n\nTap below to share your feedback:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def clip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "✂️ Smart Clipper\nUsage: `/clip [link] [start]-[end]`\nExample: `/clip https://youtu.be/xyz 01:15-01:45`",
            parse_mode="Markdown",
        )
        return

    url = args[0]
    timestamps = "".join(args[1:])

    if "-" not in timestamps:
        await update.message.reply_text(
            "Format error: Please use start-end format (e.g. 01:15-01:45)."
        )
        return

    try:
        start_str, end_str = timestamps.split("-", 1)
        start_sec = parse_time_to_seconds(start_str)
        end_sec = parse_time_to_seconds(end_str)
    except Exception:
        await update.message.reply_text("Invalid time format. Please use MM:SS.")
        return

    is_safe, error_reason = is_safe_public_url(url)
    if not is_safe:
        await update.message.reply_text(f"Error: {error_reason}")
        return

    req_id = str(uuid.uuid4())[:8]
    GLOBAL_SESSION[req_id] = {"url": url, "start": start_sec, "end": end_sec}

    keyboard = [
        [
            InlineKeyboardButton(
                "🎵 Clip Audio (MP3)", callback_data=f"dlc|{req_id}|high"
            )
        ],
        [InlineKeyboardButton("🎬 Clip Video (MP4)", callback_data=f"dlvc|{req_id}")],
    ]
    await update.message.reply_text(
        f"✂️ Ready to clip from {start_str} to {end_str}.\nSelect format:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("Fetching top trending tracks...")
    try:
        r = http_session.get(
            "https://itunes.apple.com/us/rss/topsongs/limit=10/json", timeout=6
        )
        r.raise_for_status()
        entries = r.json().get("feed", {}).get("entry", [])
        if not entries:
            raise ValueError()

        if "links" not in context.user_data:
            context.user_data["links"] = {}

        keyboard = []
        for i, entry in enumerate(entries, 1):
            title = entry.get("im:name", {}).get("label", "Track")
            artist = entry.get("im:artist", {}).get("label", "Artist")
            req_id = str(uuid.uuid4())[:8]
            context.user_data["links"][req_id] = f"ytsearch1:{title} {artist}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{i}. {title} - {artist}", callback_data=f"askq|{req_id}"
                    )
                ]
            )

        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
        await msg.edit_text(
            "🔥 Trending Songs (Tap to download):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        await msg.edit_text("Could not load trending charts right now.")


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text(
            "Please provide a search term.\nExample: /search Blinding Lights"
        )
        return

    msg = await update.message.reply_text("🔎 Searching...")

    def do_search():
        opts = {
            "quiet": True,
            "extract_flat": True,
            "extractor_args": {
                "youtube": {"player_client": ["tv_embedded", "android_vr", "ios"]}
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch5:{query}", download=False)

    try:
        info = await asyncio.to_thread(do_search)
        entries = info.get("entries", [])
        if not entries:
            await msg.edit_text("No search results found.")
            return

        if "links" not in context.user_data:
            context.user_data["links"] = {}

        keyboard = []
        for i, entry in enumerate(entries, 1):
            title = entry.get("title", "Unknown")[:40]
            req_id = str(uuid.uuid4())[:8]
            context.user_data["links"][req_id] = entry.get("url")
            keyboard.append(
                [InlineKeyboardButton(f"{i}. {title}", callback_data=f"askq|{req_id}")]
            )

        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
        await msg.edit_text(
            "Search Results (Select track):",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        await msg.edit_text("Search failed. Please try again.")


async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query.strip()
    if not query:
        return

    def do_search():
        opts = {
            "quiet": True,
            "extract_flat": True,
            "extractor_args": {
                "youtube": {"player_client": ["tv_embedded", "android_vr", "ios"]}
            },
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch5:{query}", download=False)

    try:
        info = await asyncio.to_thread(do_search)
        results = []
        bot_username = context.bot.username

        for entry in info.get("entries", []):
            url = entry.get("url")
            if not url:
                continue

            req_id = str(uuid.uuid4())[:8]
            GLOBAL_SESSION[req_id] = {"url": url}
            title = entry.get("title", "Unknown")[:50]

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🎵 Download MP3",
                        url=f"https://t.me/{bot_username}?start=dli_high_{req_id}",
                    ),
                    InlineKeyboardButton(
                        "🎬 Download MP4",
                        url=f"https://t.me/{bot_username}?start=dlvi_{req_id}",
                    ),
                ]
            ]

            results.append(
                InlineQueryResultArticle(
                    id=req_id,
                    title=title,
                    description=f"by {entry.get('uploader', 'Unknown')}",
                    input_message_content=InputTextMessageContent(
                        f"<b>{title}</b>\n🔗 {url}",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
            )
        await update.inline_query.answer(results, cache_time=30)
    except Exception:
        pass


async def handle_audio_recognition(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    msg = await update.message.reply_text("🎧 Listening and identifying track...")
    job_id = str(uuid.uuid4())
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        file_obj = getattr(update.message, "voice", None) or getattr(
            update.message, "audio", None
        )
        if not file_obj:
            raise ValueError()

        tg_file = await context.bot.get_file(file_obj.file_id)
        temp_path = os.path.join(job_dir, "input_audio.ogg")
        await tg_file.download_to_drive(temp_path)

        out = await shazam_engine.recognize(temp_path)
        track = out.get("track")
        if not track:
            await msg.edit_text("Could not identify this track.")
            return

        title = track.get("title", "Unknown Title")
        artist = track.get("subtitle", "Unknown Artist")

        if "links" not in context.user_data:
            context.user_data["links"] = {}
        req_id = str(uuid.uuid4())[:8]
        context.user_data["links"][req_id] = f"ytsearch1:{title} {artist}"

        keyboard = [
            [InlineKeyboardButton("🎵 Download Song", callback_data=f"askq|{req_id}")],
            [InlineKeyboardButton("Cancel", callback_data="cancel|000")],
        ]
        await msg.edit_text(
            f"🎵 <b>Song Found!</b>\n\n<b>Title:</b> {title}\n<b>Artist:</b> {artist}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    except Exception:
        await msg.edit_text("Song recognition encountered an error.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Linko works with links and audio files only.\nSend a link or use /search to start."
    )


async def handle_link_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user_id = update.effective_user.id

    if user_active_tasks.get(user_id, 0) > 0:
        await update.message.reply_text("Please wait, your previous request is still running.")
        return

    now = time.time()
    last_req = user_last_request.get(user_id, 0)
    if now - last_req < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last_req))
        await update.message.reply_text(
            f"Please wait {remaining}s before sending another link."
        )
        return

    message_text = update.message.text.strip()

    if not re.search(r"https?://", message_text.lower()):
        if len(message_text) < 50 and any(
            w in message_text.lower() for w in ["hi", "hello", "hey", "sup", "start"]
        ):
            await update.message.reply_text(
                "Hello! Send any public link or use /search to download music."
            )
        else:
            await update.message.reply_text(
                "Send a link starting with http:// or https://, or use /search."
            )
        return

    is_safe, error_reason = is_safe_public_url(message_text)
    if not is_safe:
        await update.message.reply_text(f"Error: {error_reason}")
        return

    request_id = str(uuid.uuid4())[:8]
    if "links" not in context.user_data:
        context.user_data["links"] = {}
    context.user_data["links"][request_id] = message_text

    keyboard = []
    if any(
        d in message_text.lower()
        for d in [
            "tiktok.com",
            "instagram.com/reel",
            "youtube.com/shorts",
            "youtu.be",
            "facebook.com",
        ]
    ):
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🎬 Standard Video (MP4)", callback_data=f"dlv|{request_id}"
                )
            ]
        )

    if (
        "list=" in message_text
        or "/playlist" in message_text
        or "/album/" in message_text
    ):
        keyboard.append(
            [
                InlineKeyboardButton(
                    "🎵 Single Track", callback_data=f"askq|{request_id}"
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"📂 Full Playlist (Max {MAX_PLAYLIST_ITEMS})",
                    callback_data=f"askqpl|{request_id}",
                )
            ]
        )
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
        await update.message.reply_text(
            "Playlist detected! Choose an option:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    else:
        keyboard.append(
            [
                InlineKeyboardButton(
                    "Standard Audio (128k MP3)",
                    callback_data=f"dl|{request_id}|standard",
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "Medium Audio (192k MP3)", callback_data=f"dl|{request_id}|medium"
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "High Audio (320k MP3)", callback_data=f"dl|{request_id}|high"
                )
            ]
        )
        keyboard.append(
            [
                InlineKeyboardButton(
                    "Voice Note (OGG Opus)", callback_data=f"dl|{request_id}|voice"
                )
            ]
        )
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])

        await update.message.reply_text(
            "⚡ Link confirmed! Choose download format:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def execute_video_download(
    chat_id, url, context, status_msg=None, start_sec=None, end_sec=None
):
    global current_in_queue

    async def update_status(text):
        if status_msg:
            try:
                await context.bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass

    video_buttons = [
        [
            InlineKeyboardButton("☕ Buy Me a Coffee", callback_data="donate"),
            InlineKeyboardButton("💬 Feedback", url=FEEDBACK_URL),
        ]
    ]
    cache_key = f"vid|{url}|{start_sec}|{end_sec}"

    # Instant Telegram File ID Cache Hit
    if cache_key in FILE_ID_CACHE:
        cached = FILE_ID_CACHE[cache_key]
        try:
            await context.bot.send_video(
                chat_id=chat_id,
                video=cached["file_id"],
                caption=cached["caption"],
                supports_streaming=True,
                reply_markup=InlineKeyboardMarkup(video_buttons),
            )
            if status_msg:
                try:
                    await context.bot.delete_message(chat_id, status_msg.message_id)
                except Exception:
                    pass
            return
        except Exception:
            FILE_ID_CACHE.pop(cache_key, None)

    # Ultra-Fast Bypass (Cobalt Parallel Race)
    if start_sec is None:
        await update_status("⚡ Resolving direct stream...")
        fast_meta = await asyncio.to_thread(FastCheckVideoBypass, url)
        if fast_meta and fast_meta.get("direct_url"):
            try:
                await update_status("⚡ Delivering video...")
                caption_text = (
                    f"{fast_meta.get('title', 'Video')}\n⚡ Delivered via Fast-Lane"
                )
                sent_msg = await context.bot.send_video(
                    chat_id=chat_id,
                    video=fast_meta["direct_url"],
                    caption=caption_text,
                    supports_streaming=True,
                    reply_markup=InlineKeyboardMarkup(video_buttons),
                    read_timeout=90,
                    write_timeout=90,
                    connect_timeout=30,
                )
                if sent_msg and sent_msg.video:
                    if len(FILE_ID_CACHE) > 500:
                        FILE_ID_CACHE.clear()
                    FILE_ID_CACHE[cache_key] = {
                        "file_id": sent_msg.video.file_id,
                        "caption": caption_text,
                    }
                if status_msg:
                    try:
                        await context.bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
                return
            except Exception:
                pass

    async with queue_counter_lock:
        if current_in_queue >= MAX_QUEUE_CAPACITY:
            await update_status(
                "Traffic Alert: Queue full right now. Please retry in 1 minute."
            )
            return
        current_in_queue += 1
        pos = current_in_queue

    in_queue = True
    try:
        if download_semaphore.locked():
            await update_status(f"⚡ Processing queue (Spot #{pos})...")

        async with download_semaphore:
            async with queue_counter_lock:
                current_in_queue -= 1
                in_queue = False

            if start_sec is not None:
                await update_status("✂️ Trimming video clip directly...")
            else:
                await update_status("⚡ Downloading video stream...")

            job_id = str(uuid.uuid4())[:8]
            job_dir = os.path.join(DOWNLOAD_DIR, job_id)
            os.makedirs(job_dir, exist_ok=True)
            output_template = os.path.join(job_dir, "video_track.mp4")

            try:
                metadata = await asyncio.to_thread(
                    BlockingDownloadVideo, url, output_template, start_sec, end_sec
                )
                await update_status("⚡ Uploading to Telegram...")

                mp4_path = os.path.join(job_dir, "video_track.mp4")
                if not os.path.exists(mp4_path):
                    raise ValueError("Failed to generate video stream.")

                file_size_mb = os.path.getsize(mp4_path) / (1024 * 1024)
                if file_size_mb > 49.5:
                    await update_status(
                        f"File size ({file_size_mb:.1f} MB) exceeds Telegram's 50 MB limit."
                    )
                    return

                caption_text = (
                    f"{metadata.get('title', 'Video')}\nSize: {file_size_mb:.1f} MB"
                )
                with open(mp4_path, "rb") as vf:
                    sent_msg = await context.bot.send_video(
                        chat_id=chat_id,
                        video=vf,
                        caption=caption_text,
                        supports_streaming=True,
                        reply_markup=InlineKeyboardMarkup(video_buttons),
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60,
                    )
                    if sent_msg and sent_msg.video:
                        if len(FILE_ID_CACHE) > 500:
                            FILE_ID_CACHE.clear()
                        FILE_ID_CACHE[cache_key] = {
                            "file_id": sent_msg.video.file_id,
                            "caption": caption_text,
                        }

                if status_msg:
                    try:
                        await context.bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
            except Exception as e:
                await update_status(f"Error: {explain_download_error(e)}")
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
                gc.collect()
    finally:
        if in_queue:
            async with queue_counter_lock:
                current_in_queue -= 1
        gc.collect()


async def execute_download(
    chat_id,
    url,
    preset_key,
    context,
    status_msg=None,
    status_prefix="",
    start_sec=None,
    end_sec=None,
):
    global current_in_queue
    preset = QUALITY_PRESETS.get(preset_key, QUALITY_PRESETS["medium"])
    bitrate = preset["bitrate"]
    is_voice = preset["type"] == "voice"

    async def update_status(text):
        if status_msg:
            try:
                await context.bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    parse_mode="HTML",
                )
            except Exception:
                pass

    cache_key = f"aud|{url}|{preset_key}|{start_sec}|{end_sec}"

    # Instant File ID Cache Hit
    if cache_key in FILE_ID_CACHE:
        cached = FILE_ID_CACHE[cache_key]
        try:
            btn, _ = build_audio_buttons(cached["title"], cached["uploader"])
            if is_voice:
                await context.bot.send_voice(
                    chat_id=chat_id,
                    voice=cached["file_id"],
                    caption=cached["caption"],
                    reply_markup=btn,
                )
            else:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=cached["file_id"],
                    caption=cached["caption"],
                    reply_markup=btn,
                )
            if status_msg:
                try:
                    await context.bot.delete_message(chat_id, status_msg.message_id)
                except Exception:
                    pass
            return
        except Exception:
            FILE_ID_CACHE.pop(cache_key, None)

    # Ultra-Fast Audio Bypass
    if start_sec is None and not is_voice:
        await update_status(f"{status_prefix}⚡ Checking Fast-Lane stream...")
        fast_meta = await asyncio.to_thread(FastCheckAudioBypass, url, bitrate)
        if fast_meta and fast_meta.get("direct_url"):
            try:
                await update_status(f"{status_prefix}⚡ Sending audio directly...")
                raw_title = fast_meta.get("title", "Audio Track")
                uploader = fast_meta.get("uploader", "Linko")
                btn, _ = build_audio_buttons(raw_title, uploader)
                caption_text = f"⚡ {bitrate} kbps | Fast-Lane"
                sent_msg = await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=fast_meta["direct_url"],
                    title=raw_title.replace("_", " "),
                    performer=uploader.replace("_", " "),
                    caption=caption_text,
                    reply_markup=btn,
                    read_timeout=90,
                    write_timeout=90,
                    connect_timeout=30,
                )
                if sent_msg and sent_msg.audio:
                    if len(FILE_ID_CACHE) > 500:
                        FILE_ID_CACHE.clear()
                    FILE_ID_CACHE[cache_key] = {
                        "file_id": sent_msg.audio.file_id,
                        "title": raw_title,
                        "uploader": uploader,
                        "caption": caption_text,
                    }
                if status_msg:
                    try:
                        await context.bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
                return
            except Exception:
                pass

    async with queue_counter_lock:
        if current_in_queue >= MAX_QUEUE_CAPACITY:
            await update_status(
                f"{status_prefix}Traffic Alert: Server capacity full. Please wait 1 min."
            )
            return
        current_in_queue += 1
        pos = current_in_queue

    in_queue = True
    try:
        if download_semaphore.locked():
            await update_status(f"{status_prefix}⚡ Queued (#{pos}). Processing shortly...")

        async with download_semaphore:
            async with queue_counter_lock:
                current_in_queue -= 1
                in_queue = False

            if start_sec is not None:
                await update_status(f"{status_prefix}✂️ Trimming ({preset['label']})...")
            else:
                await update_status(f"{status_prefix}⚡ Processing ({preset['label']})...")

            job_id = str(uuid.uuid4())[:8]
            job_dir = os.path.join(DOWNLOAD_DIR, job_id)
            os.makedirs(job_dir, exist_ok=True)
            output_template = os.path.join(job_dir, "audio_track.%(ext)s")

            try:
                metadata = await asyncio.to_thread(
                    BlockingDownloadAudio,
                    url,
                    bitrate,
                    output_template,
                    is_voice,
                    start_sec,
                    end_sec,
                )
                await update_status(f"{status_prefix}⚡ Sending file...")

                raw_title = metadata.get("title", "Audio Track")
                uploader = metadata.get("uploader", "Linko")
                btn, clean_title = build_audio_buttons(raw_title, uploader)

                target_ext = ".ogg" if is_voice else ".mp3"
                files = [
                    os.path.join(job_dir, f)
                    for f in os.listdir(job_dir)
                    if f.endswith(target_ext)
                ]
                if not files:
                    await update_status(f"{status_prefix}Error: Audio extraction failed.")
                    return

                media_path = files[0]
                file_size_mb = os.path.getsize(media_path) / (1024 * 1024)

                if file_size_mb > 49.5:
                    await update_status(
                        f"{status_prefix}Audio ({file_size_mb:.1f} MB) exceeds Telegram's 50 MB limit."
                    )
                    return

                if is_voice:
                    caption_text = f"{clean_title} ({file_size_mb:.1f} MB)"
                    with open(media_path, "rb") as vf:
                        sent_msg = await context.bot.send_voice(
                            chat_id=chat_id,
                            voice=vf,
                            caption=caption_text,
                            reply_markup=btn,
                            read_timeout=90,
                            write_timeout=90,
                            connect_timeout=30,
                        )
                        if sent_msg and sent_msg.voice:
                            if len(FILE_ID_CACHE) > 500:
                                FILE_ID_CACHE.clear()
                            FILE_ID_CACHE[cache_key] = {
                                "file_id": sent_msg.voice.file_id,
                                "title": raw_title,
                                "uploader": uploader,
                                "caption": caption_text,
                            }
                else:
                    thumb_path = next(
                        (
                            os.path.join(job_dir, f)
                            for f in os.listdir(job_dir)
                            if f.endswith(".jpg")
                        ),
                        None,
                    )
                    if thumb_path:
                        small_thumb = os.path.join(job_dir, "cover_320.jpg")
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-y",
                                "-threads",
                                "0",
                                "-i",
                                thumb_path,
                                "-vf",
                                "scale=320:320:force_original_aspect_ratio=decrease",
                                "-q:v",
                                "6",
                                small_thumb,
                            ],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        if os.path.exists(small_thumb):
                            thumb_path = small_thumb

                    caption_text = f"{bitrate} kbps | {file_size_mb:.1f} MB"
                    with open(media_path, "rb") as audio_file:
                        thumb_file = open(thumb_path, "rb") if thumb_path else None
                        try:
                            sent_msg = await context.bot.send_audio(
                                chat_id=chat_id,
                                audio=audio_file,
                                title=raw_title.replace("_", " "),
                                performer=uploader.replace("_", " "),
                                thumbnail=thumb_file,
                                caption=caption_text,
                                reply_markup=btn,
                                read_timeout=120,
                                write_timeout=120,
                                connect_timeout=60,
                            )
                            if sent_msg and sent_msg.audio:
                                if len(FILE_ID_CACHE) > 500:
                                    FILE_ID_CACHE.clear()
                                FILE_ID_CACHE[cache_key] = {
                                    "file_id": sent_msg.audio.file_id,
                                    "title": raw_title,
                                    "uploader": uploader,
                                    "caption": caption_text,
                                }
                        finally:
                            if thumb_file:
                                thumb_file.close()

                if status_msg:
                    try:
                        await context.bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
            except ValueError as ve:
                await update_status(f"{status_prefix}Error: {ve}")
            except Exception as e:
                await update_status(
                    f"{status_prefix}Error: {explain_download_error(e)}"
                )
            finally:
                shutil.rmtree(job_dir, ignore_errors=True)
                gc.collect()
    finally:
        if in_queue:
            async with queue_counter_lock:
                current_in_queue -= 1
        gc.collect()


async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "donate":
        await send_donation_info(update.effective_chat.id, context)
        return

    if data.startswith("lyrics|"):
        search_term = data.split("|", 1)[1]
        await context.bot.send_message(
            update.effective_chat.id, f"📝 Fetching lyrics for: {search_term}..."
        )
        lyrics = await asyncio.to_thread(fetch_lrclib_lyrics, search_term)

        if lyrics:
            if len(lyrics) > 4000:
                lyrics = lyrics[:4000] + "...\n(Truncated)"
            await context.bot.send_message(
                update.effective_chat.id, f"📝 <b>Lyrics: {search_term}</b>\n\n{lyrics}", parse_mode="HTML"
            )
        else:
            await context.bot.send_message(
                update.effective_chat.id, "Sorry, no lyrics found for this track."
            )
        return

    if data.startswith("cancel"):
        return await query.edit_message_text("Canceled.")

    if data.startswith("askq"):
        parts = data.split("|")
        prefix, req_id = parts[0], parts[1]
        dl_prefix = "dlpl" if "pl" in prefix else "dl"

        keyboard = [
            [
                InlineKeyboardButton(
                    "Standard Audio (128k MP3)",
                    callback_data=f"{dl_prefix}|{req_id}|standard",
                )
            ],
            [
                InlineKeyboardButton(
                    "Medium Audio (192k MP3)",
                    callback_data=f"{dl_prefix}|{req_id}|medium",
                )
            ],
            [
                InlineKeyboardButton(
                    "High Audio (320k MP3)",
                    callback_data=f"{dl_prefix}|{req_id}|high",
                )
            ],
            [
                InlineKeyboardButton(
                    "Voice Note (OGG Opus)",
                    callback_data=f"{dl_prefix}|{req_id}|voice",
                )
            ],
            [InlineKeyboardButton("Cancel", callback_data="cancel|000")],
        ]
        return await query.edit_message_text(
            "Select audio quality:", reply_markup=InlineKeyboardMarkup(keyboard)
        )

    parts = data.split("|")
    prefix = parts[0]

    if prefix in ("dlv", "dlvi", "dlvc"):
        if user_active_tasks.get(user_id, 0) > 0:
            await query.edit_message_text("Please wait, your previous request is still running.")
            return

        req_id = parts[1]
        start_sec, end_sec = None, None

        if prefix in ("dlvi", "dlvc"):
            session = GLOBAL_SESSION.pop(req_id, None)
            if not session:
                await query.edit_message_text("Session expired. Please search or clip again.")
                return
            url = session.get("url")
            start_sec = session.get("start")
            end_sec = session.get("end")
        else:
            url = context.user_data.get("links", {}).pop(req_id, None)
            if not url:
                await query.edit_message_text("Session expired. Please send the link again.")
                return

        user_last_request[user_id] = time.time()
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1

        try:
            msg = "⚡ Starting video download..." if start_sec is None else "✂️ Processing video clip..."
            await query.edit_message_text(msg)
            await execute_video_download(
                update.effective_chat.id,
                url,
                context,
                query.message,
                start_sec,
                end_sec,
            )
        finally:
            if user_active_tasks[user_id] > 0:
                user_active_tasks[user_id] -= 1
        return

    if prefix in ("dl", "dlpl", "dli", "dlc"):
        if user_active_tasks.get(user_id, 0) > 0:
            await query.edit_message_text("Please wait, your previous request is still running.")
            return

        req_id = parts[1]
        quality_key = parts[2]
        start_sec, end_sec = None, None

        if prefix in ("dli", "dlc"):
            session = GLOBAL_SESSION.pop(req_id, None)
            if not session:
                await query.edit_message_text("Session expired. Please search or clip again.")
                return
            url = session.get("url")
            start_sec = session.get("start")
            end_sec = session.get("end")
        else:
            url = context.user_data.get("links", {}).pop(req_id, None)
            if not url:
                await query.edit_message_text("Session expired. Please send the link again.")
                return

        user_last_request[user_id] = time.time()
        preset = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS["medium"])
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1

        try:
            if prefix in ("dl", "dli", "dlc"):
                msg = (
                    f"⚡ Starting download ({preset['label']})..."
                    if start_sec is None
                    else f"✂️ Clipping ({preset['label']})..."
                )
                await query.edit_message_text(msg)
                await execute_download(
                    update.effective_chat.id,
                    url,
                    quality_key,
                    context,
                    query.message,
                    "",
                    start_sec,
                    end_sec,
                )
            else:
                await query.edit_message_text("Fetching playlist metadata...")

                def get_playlist():
                    opts = {
                        "extract_flat": True,
                        "quiet": True,
                        "extractor_args": {
                            "youtube": {
                                "player_client": ["tv_embedded", "android_vr", "ios"]
                            }
                        },
                    }
                    with yt_dlp.YoutubeDL(opts) as ydl:
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
                    f"📂 Playlist queued: {len(entries)} tracks. Processing sequentially..."
                )

                for i, entry in enumerate(entries, 1):
                    if not entry.get("url"):
                        continue
                    status_msg = await context.bot.send_message(
                        update.effective_chat.id,
                        f"[Track {i}/{len(entries)}] ⚡ Starting download...",
                    )
                    await execute_download(
                        update.effective_chat.id,
                        entry["url"],
                        quality_key,
                        context,
                        status_msg,
                        f"[Track {i}/{len(entries)}] ",
                    )
                    gc.collect()
                    await asyncio.sleep(1.0)
        finally:
            if user_active_tasks[user_id] > 0:
                user_active_tasks[user_id] -= 1


def main() -> None:
    if BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        return
    Thread(target=start_health_server, daemon=True).start()
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("clip", clip_command))
    application.add_handler(CommandHandler("trending", trending_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("quality", quality_command))
    application.add_handler(CommandHandler("donate", donate_command))
    application.add_handler(CommandHandler("feedback", feedback_command))
    application.add_handler(CommandHandler("terms", terms_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(InlineQueryHandler(inline_search))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(
        MessageHandler(filters.VOICE | filters.AUDIO, handle_audio_recognition)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.ALL | filters.VIDEO, handle_media)
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_message)
    )

    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()