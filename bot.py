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
import json
import requests
from threading import Thread
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand, InlineQueryResultArticle, InputTextMessageContent
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
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

FEEDBACK_URL = os.environ.get("FEEDBACK_URL", "https://forms.gle/4u6R2iG8vFj5A7Q77")
KBZPAY_QR_URL = os.environ.get("KBZPAY_QR_URL", "")

MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", 1))
download_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)
USER_COOLDOWN_SECONDS = int(os.environ.get("USER_COOLDOWN_SECONDS", 5))
user_last_request: dict[int, float] = {}
user_active_tasks: dict[int, int] = {}
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", 1800))
MAX_PLAYLIST_ITEMS = 10
PROCESS_TIMEOUT = 240  

MAX_QUEUE_CAPACITY = 100
current_in_queue = 0
queue_counter_lock = asyncio.Lock()

GLOBAL_SESSION = {}
FILE_ID_CACHE: dict[str, dict] = {}
GLOBAL_CANCEL_FLAGS = set()

COBALT_ENDPOINTS = [
    "https://rue-cobalt.xenon.zone",
    "https://cobaltapi.cjs.nz",
    "https://cobalt-alpha.wolfy.love",
    "https://api.cobalt.rpkiinval.id",
    "https://api.qwkuns.me",
    "https://nuko-c.meowing.de",
    "https://cobalt-omega.wolfy.love",
]

QUALITY_PRESETS = {
    "standard": {"label": "Standard Audio (MP3)", "bitrate": "128", "type": "audio"},
    "medium": {"label": "Medium Audio (MP3)", "bitrate": "192", "type": "audio"},
    "high": {"label": "High Audio (MP3)", "bitrate": "320", "type": "audio"},
    "voice": {"label": "Voice Message (OGG)", "bitrate": "64", "type": "voice"}
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
        
        blocked_exts = ['.jpg', '.jpeg', '.png', '.webp', '.pdf', '.docx', '.zip']
        if any(result.path.lower().endswith(ext) for ext in blocked_exts):
            return (False, "This link points to a document or image. Please send a video or audio link.")

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
                return (False, "This link goes to a private network and cannot be downloaded.")
        return (True, "")
    except socket.gaierror:
        return (False, "Could not find that website. Please check the link for typos.")
    except Exception:
        return (False, "The link format is invalid.")

def fetch_lrclib_lyrics(query: str) -> str:
    headers = {"User-Agent": "LinkoBot (https://t.me/LinkoBot)"}
    try:
        r = requests.get(f"https://lrclib.net/api/search?q={urllib.parse.quote(query)}", headers=headers, timeout=5)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, list) and len(data) > 0:
                return data[0].get("plainLyrics") or data[0].get("syncedLyrics")
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

def get_groq_transcription(audio_path: str) -> str:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY is not configured. Please add it to the server environment variables.")
    if os.path.getsize(audio_path) > 25 * 1024 * 1024:
        raise ValueError("File is too large for AI transcription (Max 25 MB).")
        
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
    with open(audio_path, "rb") as f:
        files = {"file": ("audio.ogg", f, "audio/ogg")}
        data = {"model": "whisper-large-v3", "response_format": "text"}
        resp = requests.post("https://api.groq.com/openai/v1/audio/transcriptions", headers=headers, files=files, data=data, timeout=60)
    
    if resp.status_code != 200:
        raise ValueError(f"Groq API Error: {resp.text}")
    
    return resp.text.strip()

def get_groq_summary(text_content: str) -> str:
    if not GROQ_API_KEY:
        raise ValueError("GROQ_API_KEY is not configured. Please add it to the server environment variables.")
    
    truncated_text = text_content[:20000] 
    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama3-8b-8192",
        "messages": [
            {
                "role": "system", 
                "content": "You are an assistant that summarizes video transcripts. Provide a clear, concise 5-bullet-point summary of the main topics discussed. Do not add conversational filler, just return the bullet points."
            },
            {
                "role": "user", 
                "content": truncated_text
            }
        ],
        "temperature": 0.5
    }
    resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=30)
    
    if resp.status_code != 200:
        raise ValueError(f"API Error ({resp.status_code}): {resp.text[:100]}...")
        
    return resp.json()["choices"][0]["message"]["content"].strip()

def compress_video_to_50mb(input_path: str, output_path: str) -> None:
    try:
        cmd_dur = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', input_path]
        duration = float(subprocess.check_output(cmd_dur).decode('utf-8').strip())
        
        target_size_bits = 45 * 1024 * 1024 * 8
        target_total_bitrate = int(target_size_bits / duration)
        target_video_bitrate = target_total_bitrate - 128000
        
        if target_video_bitrate < 50000:
            raise ValueError("Video is too long to compress under 50MB with viewable quality.")
            
        cmd_compress = [
            'ffmpeg', '-y', '-threads', '0', '-i', input_path,
            '-b:v', str(target_video_bitrate),
            '-maxrate', str(target_video_bitrate * 1.5),
            '-bufsize', str(target_video_bitrate * 2),
            '-preset', 'ultrafast',
            '-vf', 'scale=-2:480',
            '-c:a', 'aac', '-b:a', '128k',
            output_path
        ]
        subprocess.run(cmd_compress, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception as e:
        raise ValueError(f"Compression engine failed: {str(e)}")

def FastCheckAudioBypass(url: str, bitrate: str) -> dict | None:
    if url.startswith("ytsearch"):
        return None
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
    for api in COBALT_ENDPOINTS:
        try:
            resp = requests.post(api, json=payload, headers=headers, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                dl_url = data.get("url")
                title = data.get("filename", "Audio Track")
                if title.lower().endswith(".mp3"):
                    title = title[:-4]
                title = title.replace("_", " ")
                if dl_url:
                    try:
                        head_resp = requests.head(dl_url, timeout=3, allow_redirects=True)
                        size = int(head_resp.headers.get("Content-Length", 0))
                        if size > 19.5 * 1024 * 1024:
                            return None
                    except Exception:
                        pass
                    return {
                        "title": title[:80] or "Audio Track",
                        "uploader": "Linko",
                        "duration": 0,
                        "webpage_url": url,
                        "direct_url": dl_url
                    }
        except Exception:
            continue
    return None

def FastCheckVideoBypass(url: str) -> dict | None:
    if url.startswith("ytsearch"):
        return None
    payload = {"url": url, "downloadMode": "auto"}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    for api in COBALT_ENDPOINTS:
        try:
            resp = requests.post(api, json=payload, headers=headers, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                dl_url = data.get("url")
                title = data.get("filename", "Video")
                if title.lower().endswith(".mp4"):
                    title = title[:-4]
                title = title.replace("_", " ")
                if dl_url:
                    try:
                        head_resp = requests.head(dl_url, timeout=3, allow_redirects=True)
                        size = int(head_resp.headers.get("Content-Length", 0))
                        if size > 19.5 * 1024 * 1024:
                            return None
                    except Exception:
                        pass
                    return {
                        "title": title[:80] or "Video",
                        "uploader": "Linko",
                        "direct_url": dl_url
                    }
        except Exception:
            continue
    return None

def run_ytdlp_with_fallback(ydl_opts: dict, url: str, start_sec: int = None, end_sec: int = None, req_id: str = None) -> tuple[dict, str, str]:
    has_cookies = os.path.exists(COOKIES_FILE_PATH) and os.path.getsize(COOKIES_FILE_PATH) > 0
    attempts = [False, True] if not has_cookies else [True, False]
    last_err = None

    def _cancel_hook(d):
        if req_id and req_id in GLOBAL_CANCEL_FLAGS:
            raise InterruptedError("Process canceled by user.")

    for use_cookie in attempts:
        opts = dict(ydl_opts)
        opts["progress_hooks"] = [_cancel_hook]
        
        if use_cookie and has_cookies:
            opts["cookiefile"] = COOKIES_FILE_PATH
            opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "android_vr", "ios", "web"]}}
        else:
            opts.pop("cookiefile", None)
            opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "android_vr", "ios"]}}
            
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
                
                if req_id and req_id in GLOBAL_CANCEL_FLAGS:
                    raise InterruptedError("Process canceled by user.")
                    
                if not info:
                    raise ValueError("Could not read the media information.")
                
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
                        raise ValueError(f"Clip cannot be longer than {MAX_DURATION_SECONDS // 60} minutes.")
                elif duration > MAX_DURATION_SECONDS:
                    raise ValueError(f"The media is longer than the {MAX_DURATION_SECONDS // 60}-minute limit.")
                
                info = ydl.process_ie_result(info, download=True)
                return info, info.get("title", "Media"), info.get("uploader", "Linko")
                
        except ValueError as ve:
            raise ve
        except Exception as e:
            if "canceled" in str(e).lower() or isinstance(e, InterruptedError):
                raise InterruptedError("Process canceled by user.")
            last_err = e
            continue

    raise last_err

def BlockingDownloadAudio(url: str, bitrate: str, output_template: str, is_voice: bool = False, start_sec: int = None, end_sec: int = None, req_id: str = None) -> dict:
    job_dir = os.path.dirname(output_template)

    if start_sec is None and not url.startswith("ytsearch"):
        payload = {"url": url, "downloadMode": "audio", "audioFormat": "mp3", "audioBitrate": bitrate}
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        for api in COBALT_ENDPOINTS:
            if req_id and req_id in GLOBAL_CANCEL_FLAGS:
                raise InterruptedError("Process canceled by user.")
            try:
                resp = requests.post(api, json=payload, headers=headers, timeout=3)
                if resp.status_code == 200:
                    dl_url = resp.json().get("url")
                    title = resp.json().get("filename", "Audio").replace("_", " ")
                    if dl_url:
                        raw_file = os.path.join(job_dir, "raw_audio_temp")
                        with requests.get(dl_url, stream=True, timeout=10) as r:
                            r.raise_for_status()
                            with open(raw_file, "wb") as f:
                                for chunk in r.iter_content(chunk_size=65536):
                                    if req_id and req_id in GLOBAL_CANCEL_FLAGS:
                                        raise InterruptedError("Process canceled by user.")
                                    f.write(chunk)
                        
                        if is_voice:
                            final_ogg = os.path.join(job_dir, "voice_track.ogg")
                            subprocess.run(
                                ["ffmpeg", "-y", "-threads", "0", "-i", raw_file, "-c:a", "libopus", "-b:a", f"{bitrate}k", final_ogg],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
                            )
                        else:
                            final_mp3 = os.path.join(job_dir, "audio_track.mp3")
                            subprocess.run(
                                ["ffmpeg", "-y", "-threads", "0", "-i", raw_file, "-vn", "-ar", "44100", "-ac", "2", "-b:a", f"{bitrate}k", final_mp3],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
                            )

                        return {
                            "title": title[:80] or "Audio Track",
                            "uploader": "Linko",
                            "duration": 0,
                            "webpage_url": url,
                        }
            except InterruptedError as ie:
                raise ie
            except Exception:
                continue

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
        "extractor_retries": 2,
        "fragment_retries": 2,
        "max_filesize": 50 * 1024 * 1024,
        "writethumbnail": not is_voice,
        "postprocessor_args": {"ffmpeg": ["-threads", "0"]},
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    }

    if start_sec is not None and end_sec is not None:
        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)])
        ydl_opts["force_keyframes_at_cuts"] = True

    if not is_voice:
        ydl_opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": bitrate},
            {"key": "FFmpegThumbnailsConvertor", "format": "jpg"}
        ]

    info, title, uploader = run_ytdlp_with_fallback(ydl_opts, url, start_sec, end_sec, req_id)

    if req_id and req_id in GLOBAL_CANCEL_FLAGS:
        raise InterruptedError("Process canceled by user.")

    if is_voice:
        raw_file = next((os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.startswith("audio_track")), None)
        if raw_file:
            final_ogg = os.path.join(job_dir, "voice_track.ogg")
            subprocess.run(
                ["ffmpeg", "-y", "-threads", "0", "-i", raw_file, "-c:a", "libopus", "-b:a", f"{bitrate}k", final_ogg],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
            if os.path.exists(raw_file):
                os.remove(raw_file)

    return {"title": title, "uploader": uploader, "duration": info.get("duration") or 0, "webpage_url": info.get("webpage_url", url)}

def BlockingDownloadVideo(url: str, output_template: str, start_sec: int = None, end_sec: int = None, req_id: str = None) -> tuple[dict, bool]:
    job_dir = os.path.dirname(output_template)
    was_compressed = False
    
    if start_sec is None and not url.startswith("ytsearch"):
        payload = {"url": url, "downloadMode": "auto"}
        headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        
        for api in COBALT_ENDPOINTS:
            if req_id and req_id in GLOBAL_CANCEL_FLAGS:
                raise InterruptedError("Process canceled by user.")
            try:
                resp = requests.post(api, json=payload, headers=headers, timeout=3)
                if resp.status_code == 200 and resp.json().get("url"):
                    dl_url = resp.json().get("url")
                    title = resp.json().get("filename", "Video").replace("_", " ")
                    final_mp4 = os.path.join(job_dir, "video_track.mp4")
                    with requests.get(dl_url, stream=True, timeout=10) as r:
                        r.raise_for_status()
                        with open(final_mp4, "wb") as f:
                            for chunk in r.iter_content(chunk_size=65536):
                                if req_id and req_id in GLOBAL_CANCEL_FLAGS:
                                    raise InterruptedError("Process canceled by user.")
                                f.write(chunk)
                    
                    if os.path.getsize(final_mp4) > 49.5 * 1024 * 1024:
                        comp_path = os.path.join(job_dir, "video_comp.mp4")
                        compress_video_to_50mb(final_mp4, comp_path)
                        os.replace(comp_path, final_mp4)
                        was_compressed = True

                    return {"title": title[:80] or "Video", "uploader": "Linko"}, was_compressed
            except InterruptedError as ie:
                raise ie
            except Exception:
                continue
            
    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": output_template,
        "restrictfilenames": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "max_filesize": 100 * 1024 * 1024,
        "postprocessor_args": {"ffmpeg": ["-threads", "0"]},
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    }
    
    if start_sec is not None and end_sec is not None:
        ydl_opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)])
        ydl_opts["force_keyframes_at_cuts"] = True
        
    _, title, uploader = run_ytdlp_with_fallback(ydl_opts, url, start_sec, end_sec, req_id)
    
    if os.path.exists(output_template) and os.path.getsize(output_template) > 49.5 * 1024 * 1024:
        comp_path = os.path.join(job_dir, "video_comp.mp4")
        compress_video_to_50mb(output_template, comp_path)
        os.replace(comp_path, output_template)
        was_compressed = True
        
    return {"title": title, "uploader": uploader}, was_compressed

def explain_download_error(error: Exception) -> str:
    message = str(error).lower()
    if "canceled" in message:
        return "Process was canceled by the user."
    if "compression engine failed" in message:
        return "The video was too massive to compress under 50MB."
    if any((term in message for term in ("sign in to confirm", "not a bot"))):
        return "The website blocked access. The bot needs updated cookies to download this."
    if any((term in message for term in ("sign in", "login required", "private", "authentication"))):
        return "This media requires a login or is private. Linko only works with public links."
    if "unsupported url" in message or "no suitable extractor" in message:
        return "This link format is not supported. Please send a valid public video or audio link."
    if any((term in message for term in ("no video formats", "requested format", "no formats", "no video", "not a video"))):
        return "No downloadable video or audio was found. This might be a picture post or an unsupported link."
    if "larger than" in message or "filesize" in message or "file size" in message:
        return "The requested file is larger than 100 MB and cannot be safely compressed to meet Telegram limits."
    if "ffmpeg" in message or "ffprobe" in message or "postprocessing" in message:
        return "The audio conversion failed. The file format might not be supported."
    return "Could not download the media. Make sure the link is public and valid."

def build_audio_buttons(raw_title: str, uploader: str) -> tuple[InlineKeyboardMarkup, str]:
    uploader_clean = uploader.replace(' - Topic', '')
    clean_title = re.sub(r'(?i)(\(official.*?\)|\[official.*?\]|\(music video.*?\)|\(lyric.*?\)|\[lyric.*?\]|\(audio.*?\)|\[audio.*?\]|\(live.*?\))', '', raw_title).strip()
    if uploader_clean.lower() not in clean_title.lower() and uploader_clean.lower() != "linko":
        search_str = f"{clean_title} {uploader_clean}"
    else:
        search_str = clean_title
    search_str = search_str.strip()
    
    search_bytes = search_str.encode('utf-8')
    if len(search_bytes) > 50:
        search_str = search_bytes[:50].decode('utf-8', errors='ignore')
        
    action_buttons = []
    if search_str:
        action_buttons.append([InlineKeyboardButton("📝 View Lyrics", callback_data=f"lyrics|{search_str}")])
        
    action_buttons.append([
        InlineKeyboardButton("🔖 Bookmark", callback_data="bmark"),
        InlineKeyboardButton("🏷️ Edit Tags", callback_data="tag_req")
    ])
    action_buttons.append([
        InlineKeyboardButton("☕ Buy Me a Coffee", callback_data="donate"),
        InlineKeyboardButton("💬 Feedback", url=FEEDBACK_URL)
    ])
    return InlineKeyboardMarkup(action_buttons), clean_title

async def post_init(application) -> None:
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("search", "Search for songs"),
        BotCommand("clip", "Clip specific audio/video parts"),
        BotCommand("trending", "See top trending songs"),
        BotCommand("help", "Learn how to use the bot"),
        BotCommand("quality", "See audio quality options"),
        BotCommand("donate", "Support Linko"),
        BotCommand("feedback", "Send suggestions and feedback"),
        BotCommand("terms", "Rules and privacy terms"),
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
                await update.message.reply_text("Please wait. Your previous file is still processing.")
                return
            
            parts = payload.split("_")
            is_video = payload.startswith("dlvi_")
            req_id = parts[1] if is_video else parts[2]
            quality_key = "high" if is_video else parts[1]

            session = GLOBAL_SESSION.pop(req_id, None)
            if not session:
                await update.message.reply_text("This link has expired. Please search again.")
                return
            
            url = session.get("url")
            user_last_request[user_id] = time.time()
            user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
            
            try:
                status_msg = await update.message.reply_text("⚡ Processing download from inline search...")
                if is_video:
                    await asyncio.wait_for(execute_video_download(update.effective_chat.id, url, context, status_msg, req_id=req_id), timeout=PROCESS_TIMEOUT)
                else:
                    await asyncio.wait_for(execute_download(update.effective_chat.id, url, quality_key, context, status_msg, "", req_id=req_id), timeout=PROCESS_TIMEOUT)
            except (asyncio.TimeoutError, TimeoutError):
                GLOBAL_CANCEL_FLAGS.add(req_id)
                await context.bot.send_message(update.effective_chat.id, "⏳ The request took too long (over 4 minutes) and was automatically canceled. Please try again.")
            except InterruptedError:
                pass 
            finally:
                GLOBAL_CANCEL_FLAGS.discard(req_id)
                if user_active_tasks[user_id] > 0:
                    user_active_tasks[user_id] -= 1
            return

    user_first_name = update.effective_user.first_name or "there"
    welcome_text = (
        f"<b>Welcome to Linko, {user_first_name}!</b> ⚡\n\n"
        "Here is what I can do:\n\n"
        "<b>1. Download Media</b>\n"
        "Send me any public video or audio link.\n\n"
        "<b>2. Smart Clipper (New!)</b>\n"
        "Type <code>/clip [link] 01:15-01:45</code> to save a specific part.\n\n"
        "<b>3. Inline Search (New!)</b>\n"
        "Type @Linko4UBot in ANY chat to find and share music.\n\n"
        "<b>4. Search & Trending</b>\n"
        "Type /search [song name] or /trending.\n\n"
        "<b>5. Identify & Transcribe</b>\n"
        "Forward a voice note to Shazam it or transcribe it to text.\n\n"
        "<b>Guides:</b>\n"
        "• <a href='https://telegra.ph/Linko---User-Guide-10-17'>Read the English Guide</a>\n"
        "• <a href='https://telegra.ph/Linko---User-Guide-MM-10-17'>မြန်မာလမ်းညွှန် ဖတ်ရန်</a>\n\n"
        "<i>By using Linko, you agree to our /terms.</i>"
    )
    await update.message.reply_text(
        welcome_text, 
        parse_mode="HTML", 
        disable_web_page_preview=True
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "<b>⚡ Linko Quick Help Guide</b>\n\n"
        "<b>How to use:</b>\n"
        "• Send any URL (YouTube, TikTok, Reels, etc.)\n"
        "• <code>/clip [link] [start]-[end]</code> to trim media\n"
        "• <code>@Linko4UBot [query]</code> in any chat for inline search\n"
        "• Send a voice note/audio to identify the song\n"
        "• <code>/search [song]</code> or <code>/trending</code>\n\n"
        "<b>Quality Options:</b>\n"
        "🎵 <b>Standard</b> (128kbps) | <b>Medium</b> (192kbps) | <b>High</b> (320kbps)\n"
        "🎤 <b>Voice Note</b> (OGG) | 🎬 <b>Video</b> (MP4)\n\n"
        "<b>📖 Detailed Guides:</b>\n"
        "• <a href='https://telegra.ph/Linko---User-Guide-10-17'>Read the English Guide</a>\n"
        "• <a href='https://telegra.ph/Linko---User-Guide-MM-10-17'>မြန်မာလမ်းညွှန် ဖတ်ရန်</a>\n\n"
        "<i>Limits: Max 30 mins length, max 50 MB file size limit, 4 min processing limit.</i>"
    )
    await update.message.reply_text(
        help_text, 
        parse_mode="HTML", 
        disable_web_page_preview=True
    )

async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    quality_text = (
        "Audio Quality Options\n\n"
        "1. Standard Audio (MP3 - 128 kbps)\n"
        "   - Best for fast downloads and saving data.\n"
        "   - Size: ~1.0 MB per minute.\n\n"
        "2. Medium Audio (MP3 - 192 kbps)\n"
        "   - Great balance of quality and file size.\n"
        "   - Size: ~1.4 MB per minute.\n\n"
        "3. High Audio (MP3 - 320 kbps)\n"
        "   - Best audio quality for music listening.\n"
        "   - Size: ~2.4 MB per minute.\n\n"
        "4. Voice Message (OGG)\n"
        "   - Sends the audio as a playable Telegram voice message."
    )
    await update.message.reply_text(quality_text)

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    about_text = (
        "<b>About Linko</b>\n\n"
        "Linko is a streamlined Telegram utility designed to simplify media downloading and music discovery.\n\n"
        "The bot allows users to save watermark-free videos, convert public web links into audio files, and identify playing music. It processes all requests directly within the chat and includes built-in features such as music search, trending charts, and lyrics retrieval.\n\n"
        "To ensure reliable performance, the system is powered by Python, yt-dlp, FFmpeg, and ShazamIO.\n\n"
        "Developed and maintained by <a href='https://www.htetzawpaing.site'>Htet Zaw Paing</a>.\n"
        "A project by <a href='https://www.myancode.com'>MyanCode</a>."
    )
    await update.message.reply_text(about_text, parse_mode="HTML")

async def terms_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    terms_text = (
        "<b>⚖️ Linko Bot Terms of Service & Rules</b>\n\n"
        "By using this bot, you explicitly agree to the following strict guidelines:\n\n"
        "<b>1. Copyright & Fair Use</b>\n"
        "You must only download content for which you hold the rights, or that is freely available under public domain/Creative Commons. You are solely responsible for how you use the downloaded media.\n\n"
        "<b>2. Prohibited Content</b>\n"
        "Attempting to bypass DRM, download age-restricted, adult, or illegal content, or accessing private accounts without authorization is strictly prohibited.\n\n"
        "<b>3. Abuse & Rate Limits</b>\n"
        "Spamming requests or exploiting the bot's queuing system will result in an automatic, permanent ban. The bot enforces strict rate limits to ensure fair access for all users.\n\n"
        "<b>4. Privacy Guarantee</b>\n"
        "Linko operates on a strict zero-retention policy. All processed audio and video files are permanently purged from our servers the exact millisecond they are delivered to your Telegram chat.\n\n"
        "<i>Failure to adhere to these rules will result in immediate restriction from the bot.</i>"
    )
    await update.message.reply_text(terms_text, parse_mode="HTML")

async def send_donation_info(chat_id, context: ContextTypes.DEFAULT_TYPE) -> None:
    donate_caption = (
        "☕ Thank you so much for using Linko!\n\n"
        "If Linko has been helpful and made your downloads convenient, you can support "
        "and buy me a coffee by scanning the KBZPay QR code above.\n\n"
        "Any amount is genuinely appreciated and helps keep the bot fast, stable, and running 24/7!"
    )
    
    if KBZPAY_QR_URL:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=KBZPAY_QR_URL, caption=donate_caption)
            return
        except Exception as e:
            logger.error(f"Failed to send QR image from URL: {e}")
            
    fallback_caption = donate_caption.replace(" scanning the KBZPay QR code above", " supporting our project")
    await context.bot.send_message(chat_id=chat_id, text=fallback_caption)

async def donate_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_donation_info(update.effective_chat.id, context)

async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [[InlineKeyboardButton("💬 Open Feedback Form", url=FEEDBACK_URL)]]
    await update.message.reply_text(
        "We would love to hear your thoughts, suggestions, or bug reports to improve Linko!\n\n"
        "Tap the button below to share your feedback:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def clip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("✂️ Smart Clipper\nUsage: `/clip [link] [start]-[end]`\nExample: `/clip https://youtu.be/xyz 01:15-01:45`", parse_mode="Markdown")
        return
        
    url = args[0]
    timestamps = "".join(args[1:])
    
    if "-" not in timestamps:
        await update.message.reply_text("Error: Please use the format start-end (e.g., 01:15-01:45).")
        return
        
    try:
        start_str, end_str = timestamps.split("-", 1)
        start_sec = parse_time_to_seconds(start_str)
        end_sec = parse_time_to_seconds(end_str)
    except Exception:
        await update.message.reply_text("Error: Invalid time format. Use MM:SS.")
        return

    is_safe, error_reason = is_safe_public_url(url)
    if not is_safe:
        await update.message.reply_text(f"Error: {error_reason}")
        return

    req_id = str(uuid.uuid4())[:8]
    GLOBAL_SESSION[req_id] = {"url": url, "start": start_sec, "end": end_sec}
    
    keyboard = [
        [InlineKeyboardButton("🎵 Clip Audio (MP3)", callback_data=f"dlc|{req_id}|high")],
        [InlineKeyboardButton("🎬 Clip Video (MP4)", callback_data=f"dlvc|{req_id}")]
    ]
    await update.message.reply_text(f"✂️ Clipping from {start_str} to {end_str}.\nChoose format:", reply_markup=InlineKeyboardMarkup(keyboard))

async def edit_tags_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message.reply_to_message or not update.message.reply_to_message.audio:
        await update.message.reply_text("⚠️ To edit tags, you must strictly REPLY to an existing AUDIO message with:\n`/edit Artist - Title`", parse_mode="Markdown")
        return
    
    message_text = update.message.text or update.message.caption or ""
    args_text = message_text.replace("/edit", "").strip()
    
    if "-" not in args_text:
        await update.message.reply_text("⚠️ Please provide the new tags in the format:\n`/edit Artist Name - Song Title`\n\n*(Make sure you reply directly to the audio!)*", parse_mode="Markdown")
        return
        
    artist, title = [x.strip() for x in args_text.split("-", 1)]
    status_msg = await update.message.reply_text("🏷️ Downloading original audio to update tags...")
    
    job_id = str(uuid.uuid4())[:8]
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    
    try:
        audio_file_size = update.message.reply_to_message.audio.file_size
        if audio_file_size and audio_file_size > 20 * 1024 * 1024:
            await status_msg.edit_text("⚠️ This audio file is too large (over 20MB) for the bot to edit directly due to Telegram limits.")
            return

        orig_file = await context.bot.get_file(update.message.reply_to_message.audio.file_id)
        input_mp3 = os.path.join(job_dir, "input.mp3")
        output_mp3 = os.path.join(job_dir, "output.mp3")
        await orig_file.download_to_drive(input_mp3)
        
        photo_path = None
        if update.message.photo:
            photo_file = await context.bot.get_file(update.message.photo[-1].file_id)
            photo_path = os.path.join(job_dir, "cover.jpg")
            await photo_file.download_to_drive(photo_path)
            
        await status_msg.edit_text("🏷️ Injecting ID3 Tags and Cover Art...")
        
        if photo_path:
            cmd = [
                'ffmpeg', '-y', '-i', input_mp3, '-i', photo_path, 
                '-map', '0:0', '-map', '1:0', '-c', 'copy', 
                '-id3v2_version', '3', '-metadata:s:v', 'title="Album cover"', 
                '-metadata:s:v', 'comment="Cover (front)"', 
                '-metadata', f'title={title}', '-metadata', f'artist={artist}', 
                output_mp3
            ]
        else:
            cmd = [
                'ffmpeg', '-y', '-i', input_mp3, '-c', 'copy', 
                '-id3v2_version', '3', 
                '-metadata', f'title={title}', '-metadata', f'artist={artist}', 
                output_mp3
            ]
            
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        
        await status_msg.edit_text("⚡ Uploading newly tagged file...")
        btn, _ = build_audio_buttons(title, artist)
        
        with open(output_mp3, "rb") as af:
            thumb_file = open(photo_path, "rb") if photo_path else None
            try:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id, 
                    audio=af, 
                    title=title, 
                    performer=artist, 
                    thumbnail=thumb_file, 
                    caption="🏷️ Tags Updated!", 
                    reply_markup=btn
                )
            finally:
                if thumb_file:
                    thumb_file.close()
                
        await status_msg.delete()
    except Exception as e:
        await status_msg.edit_text(f"⚠️ Failed to update tags: {str(e)}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

async def trending_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("Getting the top trending songs...")
    try:
        r = requests.get("https://itunes.apple.com/us/rss/topsongs/limit=10/json", timeout=10)
        r.raise_for_status()
        entries = r.json().get("feed", {}).get("entry", [])
        if not entries:
            raise ValueError()
            
        if "links" not in context.user_data:
            context.user_data["links"] = {}
            
        keyboard = []
        for i, entry in enumerate(entries, 1):
            title = entry.get("im:name", {}).get("label", "Unknown Track")
            artist = entry.get("im:artist", {}).get("label", "Unknown Artist")
            req_id = str(uuid.uuid4())[:8]
            context.user_data["links"][req_id] = f"ytsearch1:{title} {artist}"
            keyboard.append([InlineKeyboardButton(f"{i}. {title} - {artist}", callback_data=f"askq|{req_id}")])
            
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
        await msg.edit_text("🔥 Top Trending Songs (Select one to download):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await msg.edit_text("Sorry, I could not load the trending charts right now.")

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Please add a song name to search.\nExample: /search Blinding Lights")
        return
        
    msg = await update.message.reply_text("🔎 Searching for your song...")
    
    def do_search():
        opts = {
            "quiet": True,
            "extract_flat": True,
            "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android_vr", "ios"]}}
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch5:{query}", download=False)
            
    try:
        info = await asyncio.to_thread(do_search)
        entries = info.get("entries", [])
        if not entries:
            await msg.edit_text("Sorry, no results found for that search.")
            return
            
        if "links" not in context.user_data: 
            context.user_data["links"] = {}
            
        keyboard = []
        for i, entry in enumerate(entries, 1):
            title = entry.get("title", "Unknown")[:40]
            req_id = str(uuid.uuid4())[:8]
            context.user_data["links"][req_id] = entry.get("url")
            keyboard.append([InlineKeyboardButton(f"{i}. {title}", callback_data=f"askq|{req_id}")])
            
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
        await msg.edit_text("Search Results (Select a track):", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        await msg.edit_text("Search failed. Please try again later.")

async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query.query.strip()
    if not query:
        return
        
    def do_search():
        opts = {
            "quiet": True,
            "extract_flat": True,
            "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android_vr", "ios"]}}
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"ytsearch5:{query}", download=False)
            
    try:
        info = await asyncio.to_thread(do_search)
        results = []
        bot_username = context.bot.username
        
        for entry in info.get("entries", []):
            url = entry.get("url")
            if not url: continue
            
            req_id = str(uuid.uuid4())[:8]
            GLOBAL_SESSION[req_id] = {"url": url}
            title = entry.get("title", "Unknown")[:50]
            
            keyboard = [
                [
                    InlineKeyboardButton("🎵 Download MP3", url=f"https://t.me/{bot_username}?start=dli_high_{req_id}"),
                    InlineKeyboardButton("🎬 Download MP4", url=f"https://t.me/{bot_username}?start=dlvi_{req_id}")
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
                        disable_web_page_preview=True
                    ),
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            )
        await update.inline_query.answer(results, cache_time=10)
    except Exception:
        pass

async def handle_audio_recognition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    file_obj = getattr(update.message, 'voice', None) or getattr(update.message, 'audio', None)
    if not file_obj:
        return
        
    if file_obj.file_size and file_obj.file_size > 20 * 1024 * 1024:
        await update.message.reply_text("⚠️ This file is too large (over 20MB) for AI recognition.")
        return
        
    req_id = str(uuid.uuid4())[:8]
    GLOBAL_SESSION[req_id] = {"file_id": file_obj.file_id}
    
    keyboard = [
        [InlineKeyboardButton("🎵 Identify Song (Shazam)", callback_data=f"shazam|{req_id}")],
        [InlineKeyboardButton("📝 Transcribe to Text (AI)", callback_data=f"transcribe|{req_id}")],
        [InlineKeyboardButton("Cancel", callback_data="cancel|000")]
    ]
    await update.message.reply_text("🎧 Audio received! What would you like to do?", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_sticker_gif(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.sticker:
        await update.message.reply_sticker(update.message.sticker.file_id)
    elif update.message.animation:
        await update.message.reply_animation(update.message.animation.file_id)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Linko only works with links and audio files. Please do not send images or documents for privacy reasons.\n\n"
        "Send a media link or use /search to begin."
    )

async def handle_link_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    
    if user_active_tasks.get(user_id, 0) > 0:
        await update.message.reply_text("Please wait. Your previous file is still processing.")
        return

    now = time.time()
    last_req = user_last_request.get(user_id, 0)
    if now - last_req < USER_COOLDOWN_SECONDS:
        remaining = int(USER_COOLDOWN_SECONDS - (now - last_req))
        await update.message.reply_text(f"Please wait {remaining} seconds before sending another link.")
        return
    
    message_text = update.message.text.strip()
    
    if not re.search(r"https?://", message_text.lower()):
        if len(message_text) < 50 and any(w in message_text.lower() for w in ["hi", "hello", "hey", "sup", "start"]):
            await update.message.reply_text("Hello! Send me a public link or use /search to download music.")
        else:
            await update.message.reply_text("Please send a valid link starting with http:// or https://, or use /search.")
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
    
    if "youtube.com" in message_text.lower() or "youtu.be" in message_text.lower():
        keyboard.append([InlineKeyboardButton("📋 Summarize Video (AI)", callback_data=f"summary|{request_id}")])

    if any(d in message_text.lower() for d in ["tiktok.com", "instagram.com/reel", "youtube.com/shorts", "youtu.be"]):
        keyboard.append([InlineKeyboardButton("🎬 Standard Video (MP4)", callback_data=f"dlv|{request_id}")])
        
    if "list=" in message_text or "/playlist" in message_text or "/album/" in message_text:
        keyboard.append([InlineKeyboardButton("🎵 Single Track", callback_data=f"askq|{request_id}")])
        keyboard.append([InlineKeyboardButton(f"📂 Full Playlist (Max {MAX_PLAYLIST_ITEMS})", callback_data=f"askqpl|{request_id}")])
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
        await update.message.reply_text("Playlist found! What would you like to download?", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        keyboard.append([InlineKeyboardButton("Standard Audio (MP3)", callback_data=f"dl|{request_id}|standard")])
        keyboard.append([InlineKeyboardButton("Medium Audio (MP3)", callback_data=f"dl|{request_id}|medium")])
        keyboard.append([InlineKeyboardButton("High Audio (MP3)", callback_data=f"dl|{request_id}|high")])
        keyboard.append([InlineKeyboardButton("Voice Message (OGG)", callback_data=f"dl|{request_id}|voice")])
        keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text("⚡ Link verified! Choose your download format:", reply_markup=reply_markup)

async def execute_video_download(chat_id, url, context, status_msg=None, start_sec=None, end_sec=None, req_id=None):
    global current_in_queue
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel Processing", callback_data=f"abort|{req_id}")]]) if req_id else None

    async def update_status(text):
        if req_id and req_id in GLOBAL_CANCEL_FLAGS:
            return
        if status_msg:
            try:
                await context.bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg.message_id, parse_mode="HTML", reply_markup=cancel_kb)
            except Exception:
                pass

    video_buttons = [
        [InlineKeyboardButton("🔖 Bookmark", callback_data="bmark")],
        [
            InlineKeyboardButton("☕ Buy Me a Coffee", callback_data="donate"),
            InlineKeyboardButton("💬 Feedback", url=FEEDBACK_URL)
        ]
    ]
    cache_key = f"vid|{url}|{start_sec}|{end_sec}"

    if cache_key in FILE_ID_CACHE:
        cached = FILE_ID_CACHE[cache_key]
        try:
            await context.bot.send_video(
                chat_id=chat_id,
                video=cached["file_id"],
                caption=cached["caption"],
                supports_streaming=True,
                reply_markup=InlineKeyboardMarkup(video_buttons)
            )
            if status_msg:
                try:
                    await context.bot.delete_message(chat_id, status_msg.message_id)
                except Exception:
                    pass
            return
        except Exception:
            FILE_ID_CACHE.pop(cache_key, None)

    if start_sec is None:
        await update_status("⚡ Processing your request. Fetching video stream...")
        fast_meta = await asyncio.to_thread(FastCheckVideoBypass, url)
        if fast_meta and fast_meta.get("direct_url"):
            try:
                await update_status("⚡ Uploading video to Telegram...")
                caption_text = f"{fast_meta.get('title', 'Video')}\nSize: < 20 MB (Direct URL)"
                sent_msg = await context.bot.send_video(
                    chat_id=chat_id,
                    video=fast_meta["direct_url"],
                    caption=caption_text,
                    supports_streaming=True,
                    reply_markup=InlineKeyboardMarkup(video_buttons),
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60
                )
                if sent_msg and sent_msg.video:
                    if len(FILE_ID_CACHE) > 500:
                        FILE_ID_CACHE.clear()
                    FILE_ID_CACHE[cache_key] = {
                        "file_id": sent_msg.video.file_id,
                        "caption": caption_text
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
            await update_status("Traffic Alert: Over 100 requests in line right now. Please try again in 1-2 minutes.")
            return
        current_in_queue += 1
        pos = current_in_queue

    in_queue = True
    try:
        if download_semaphore.locked():
            await update_status(f"[High Traffic] Linko is busy. You are #{pos} in line. Please wait...")

        async with download_semaphore:
            async with queue_counter_lock:
                current_in_queue -= 1
                in_queue = False

            if req_id and req_id in GLOBAL_CANCEL_FLAGS:
                return

            if start_sec is not None:
                await update_status("✂️ Clipping video directly from the server...")
            else:
                await update_status("⚡ Processing your request. Downloading video...")
                
            job_id = str(uuid.uuid4())
            job_dir = os.path.join(DOWNLOAD_DIR, job_id)
            os.makedirs(job_dir, exist_ok=True)
            output_template = os.path.join(job_dir, "video_track.mp4")
            
            try:
                metadata, was_compressed = await asyncio.to_thread(BlockingDownloadVideo, url, output_template, start_sec, end_sec, req_id)
                
                if was_compressed:
                    await update_status("⚡ Video was large. Compressed successfully to fit Telegram limits! Uploading...")
                else:
                    await update_status("⚡ Uploading video to Telegram...")

                mp4_path = os.path.join(job_dir, "video_track.mp4")
                if not os.path.exists(mp4_path):
                    raise ValueError("The video file could not be generated.")
                    
                file_size_mb = os.path.getsize(mp4_path) / (1024 * 1024)
                if file_size_mb > 49.5:
                    await update_status(f"This video is too large ({file_size_mb:.2f} MB) and could not be compressed safely. Telegram limits bots to 50 MB.")
                    return
                    
                compression_tag = "\n📉 (Auto-Compressed)" if was_compressed else ""
                caption_text = f"{metadata.get('title', 'Video')}\nSize: {file_size_mb:.2f} MB{compression_tag}"
                
                with open(mp4_path, "rb") as vf:
                    sent_msg = await context.bot.send_video(
                        chat_id=chat_id,
                        video=vf,
                        caption=caption_text,
                        supports_streaming=True,
                        reply_markup=InlineKeyboardMarkup(video_buttons),
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=60
                    )
                    if sent_msg and sent_msg.video:
                        if len(FILE_ID_CACHE) > 500:
                            FILE_ID_CACHE.clear()
                        FILE_ID_CACHE[cache_key] = {
                            "file_id": sent_msg.video.file_id,
                            "caption": caption_text
                        }

                if status_msg:
                    try:
                        await context.bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
                    
            except InterruptedError:
                await update_status("🚫 Process canceled by user.")
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

async def execute_download(chat_id, url, preset_key, context, status_msg=None, status_prefix="", start_sec=None, end_sec=None, req_id=None):
    global current_in_queue
    preset = QUALITY_PRESETS.get(preset_key, QUALITY_PRESETS["medium"])
    bitrate = preset["bitrate"]
    is_voice = preset["type"] == "voice"
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚫 Cancel Processing", callback_data=f"abort|{req_id}")]]) if req_id else None

    async def update_status(text):
        if req_id and req_id in GLOBAL_CANCEL_FLAGS:
            return
        if status_msg:
            try: 
                await context.bot.edit_message_text(text, chat_id=chat_id, message_id=status_msg.message_id, parse_mode="HTML", reply_markup=cancel_kb)
            except Exception: 
                pass

    cache_key = f"aud|{url}|{preset_key}|{start_sec}|{end_sec}"

    if cache_key in FILE_ID_CACHE:
        cached = FILE_ID_CACHE[cache_key]
        try:
            btn, _ = build_audio_buttons(cached["title"], cached["uploader"])
            if is_voice:
                await context.bot.send_voice(
                    chat_id=chat_id,
                    voice=cached["file_id"],
                    caption=cached["caption"],
                    reply_markup=btn
                )
            else:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=cached["file_id"],
                    caption=cached["caption"],
                    reply_markup=btn
                )
            if status_msg:
                try:
                    await context.bot.delete_message(chat_id, status_msg.message_id)
                except Exception:
                    pass
            return
        except Exception:
            FILE_ID_CACHE.pop(cache_key, None)

    if start_sec is None and not is_voice:
        await update_status(f"{status_prefix}⚡ Processing your request. Fetching ({preset['label']})...")
        fast_meta = await asyncio.to_thread(FastCheckAudioBypass, url, bitrate)
        if fast_meta and fast_meta.get("direct_url"):
            try:
                await update_status(f"{status_prefix}⚡ Uploading audio to Telegram...")
                raw_title = fast_meta.get("title", "Audio Track")
                uploader = fast_meta.get("uploader", "Linko")
                btn, _ = build_audio_buttons(raw_title, uploader)
                caption_text = f"{bitrate} kbps | < 20 MB (Direct URL)"
                sent_msg = await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=fast_meta["direct_url"],
                    title=raw_title.replace("_", " "),
                    performer=uploader.replace("_", " "),
                    caption=caption_text,
                    reply_markup=btn,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=60
                )
                if sent_msg and sent_msg.audio:
                    if len(FILE_ID_CACHE) > 500:
                        FILE_ID_CACHE.clear()
                    FILE_ID_CACHE[cache_key] = {
                        "file_id": sent_msg.audio.file_id,
                        "title": raw_title,
                        "uploader": uploader,
                        "caption": caption_text
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
            await update_status(f"{status_prefix}Traffic Alert: Over 100 requests in line right now. Please try again in 1-2 minutes.")
            return
        current_in_queue += 1
        pos = current_in_queue

    in_queue = True
    try:
        if download_semaphore.locked():
            await update_status(f"{status_prefix}[High Traffic] Linko is busy. You are #{pos} in line. Please wait...")

        async with download_semaphore:
            async with queue_counter_lock:
                current_in_queue -= 1
                in_queue = False

            if req_id and req_id in GLOBAL_CANCEL_FLAGS:
                return

            if start_sec is not None:
                await update_status(f"{status_prefix}✂️ Clipping ({preset['label']})... Please wait a moment.")
            else:
                await update_status(f"{status_prefix}⚡ Processing your request. Downloading ({preset['label']})...")
            
            job_id = str(uuid.uuid4())
            job_dir = os.path.join(DOWNLOAD_DIR, job_id)
            os.makedirs(job_dir, exist_ok=True)
            output_template = os.path.join(job_dir, "audio_track.%(ext)s")
            
            try:
                metadata = await asyncio.to_thread(BlockingDownloadAudio, url, bitrate, output_template, is_voice, start_sec, end_sec, req_id)
                await update_status(f"{status_prefix}⚡ Uploading audio to Telegram...")
                
                raw_title = metadata.get('title', 'Audio Track')
                uploader = metadata.get('uploader', 'Linko')
                btn, clean_title = build_audio_buttons(raw_title, uploader)
                
                target_ext = ".ogg" if is_voice else ".mp3"
                files = [os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.endswith(target_ext)]
                if not files:
                    await update_status(f"{status_prefix}Error: Failed to extract the audio file.")
                    return
                    
                media_path = files[0]
                file_size_mb = os.path.getsize(media_path) / (1024 * 1024)
                
                if file_size_mb > 49.5:
                    await update_status(f"{status_prefix}This file is too large ({file_size_mb:.2f} MB). Telegram limits bots to 50 MB.")
                    return
                    
                if is_voice:
                    caption_text = f"{clean_title}\nSize: {file_size_mb:.2f} MB"
                    with open(media_path, "rb") as vf:
                        sent_msg = await context.bot.send_voice(
                            chat_id=chat_id,
                            voice=vf,
                            caption=caption_text,
                            reply_markup=btn,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=60
                        )
                        if sent_msg and sent_msg.voice:
                            if len(FILE_ID_CACHE) > 500:
                                FILE_ID_CACHE.clear()
                            FILE_ID_CACHE[cache_key] = {
                                "file_id": sent_msg.voice.file_id,
                                "title": raw_title,
                                "uploader": uploader,
                                "caption": caption_text
                            }
                else:
                    thumb_path = next((os.path.join(job_dir, f) for f in os.listdir(job_dir) if f.endswith('.jpg')), None)
                    if thumb_path:
                        small_thumb = os.path.join(job_dir, "cover_320.jpg")
                        subprocess.run(
                            ["ffmpeg", "-y", "-threads", "0", "-i", thumb_path, "-vf", "scale=320:320:force_original_aspect_ratio=decrease", "-q:v", "5", small_thumb], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                        if os.path.exists(small_thumb):
                            thumb_path = small_thumb

                    caption_text = f"{bitrate} kbps | {file_size_mb:.2f} MB"
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
                                connect_timeout=60
                            )
                            if sent_msg and sent_msg.audio:
                                if len(FILE_ID_CACHE) > 500:
                                    FILE_ID_CACHE.clear()
                                FILE_ID_CACHE[cache_key] = {
                                    "file_id": sent_msg.audio.file_id,
                                    "title": raw_title,
                                    "uploader": uploader,
                                    "caption": caption_text
                                }
                        finally:
                            if thumb_file:
                                thumb_file.close()
                        
                if status_msg: 
                    try:
                        await context.bot.delete_message(chat_id, status_msg.message_id)
                    except Exception:
                        pass
                    
            except InterruptedError:
                await update_status(f"{status_prefix}🚫 Process canceled by user.")
            except ValueError as ve:
                await update_status(f"{status_prefix}Error: {ve}")
            except Exception as e:
                await update_status(f"{status_prefix}Error: {explain_download_error(e)}")
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
        
    if data.startswith("abort|"):
        req_id = data.split("|")[1]
        GLOBAL_CANCEL_FLAGS.add(req_id)
        try:
            await query.edit_message_text("🚫 Process canceled by user.")
        except Exception:
            pass
        return
        
    if data == "bmark":
        current_caption = query.message.caption or ""
        if "#LinkoSaved" in current_caption:
            return
        new_caption = f"{current_caption}\n\n🔖 #LinkoSaved #Music #Video"
        try:
            await query.message.edit_caption(caption=new_caption, reply_markup=query.message.reply_markup)
        except Exception:
            pass
        return
        
    if data == "tag_req":
        await context.bot.send_message(
            update.effective_chat.id,
            "🏷️ **Custom ID3 Tags & Cover Art**\n\nTo change the title, artist, and cover art, simply REPLY directly to the audio message above with this exact format:\n\n`/edit Artist Name - Song Title`\n\n*(Pro Tip: If you attach a square photo to your reply, it will be injected as the new Album Cover!)*",
            parse_mode="Markdown"
        )
        return

    if data.startswith("lyrics|"):
        search_term = data.split("|", 1)[1]
        await context.bot.send_message(update.effective_chat.id, f"📝 Looking up lyrics for: {search_term}...")
        lyrics = await asyncio.to_thread(fetch_lrclib_lyrics, search_term)
        
        if lyrics:
            if len(lyrics) > 4000: 
                lyrics = lyrics[:4000] + "...\n(Truncated)"
            await context.bot.send_message(update.effective_chat.id, f"📝 Lyrics: {search_term}\n\n{lyrics}")
        else:
            await context.bot.send_message(update.effective_chat.id, "Sorry, no lyrics found for this song.")
        return

    if data.startswith("shazam|"):
        req_id = data.split("|")[1]
        session = GLOBAL_SESSION.pop(req_id, None)
        if not session:
            await query.edit_message_text("Session expired. Please send the voice note again.")
            return
            
        await query.edit_message_text("🎧 Identifying the song...")
        job_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4())[:8])
        os.makedirs(job_dir, exist_ok=True)
        try:
            tg_file = await context.bot.get_file(session["file_id"])
            temp_path = os.path.join(job_dir, "input_audio.oga")
            await tg_file.download_to_drive(temp_path)
            
            out = await shazam_engine.recognize(temp_path)
            track = out.get("track")
            
            if not track:
                await query.edit_message_text("⚠️ Shazam didn't recognize this track (common for local Burmese songs).\n\n🤖 Let me try listening to the lyrics with AI...")
                
                transcription = await asyncio.to_thread(get_groq_transcription, temp_path)
                if not transcription or len(transcription.strip()) < 3:
                    await query.edit_message_text("⚠️ Sorry, I couldn't identify the song or hear the lyrics clearly.")
                    return
                
                search_query = transcription[:100].replace('\n', ' ')
                
                def do_fallback_search():
                    opts = {"quiet": True, "extract_flat": True, "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android_vr", "ios"]}}}
                    with yt_dlp.YoutubeDL(opts) as ydl:
                        return ydl.extract_info(f"ytsearch5:{search_query}", download=False)
                
                info = await asyncio.to_thread(do_fallback_search)
                entries = info.get("entries", [])
                
                if not entries:
                    await query.edit_message_text(f"📝 I heard: _{search_query}_\n\n⚠️ But no matches were found on YouTube for those lyrics.", parse_mode="Markdown")
                    return
                
                if "links" not in context.user_data:
                    context.user_data["links"] = {}
                keyboard = []
                for i, entry in enumerate(entries, 1):
                    t_title = entry.get("title", "Unknown")[:40]
                    t_req_id = str(uuid.uuid4())[:8]
                    context.user_data["links"][t_req_id] = entry.get("url")
                    keyboard.append([InlineKeyboardButton(f"{i}. {t_title}", callback_data=f"askq|{t_req_id}")])
                
                keyboard.append([InlineKeyboardButton("Cancel", callback_data="cancel|000")])
                await query.edit_message_text(f"🎵 **AI Lyric Search Results:**\n_{search_query}_", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
                return
            
            title = track.get("title", "Unknown Title")
            artist = track.get("subtitle", "Unknown Artist")
            
            if "links" not in context.user_data:
                context.user_data["links"] = {}
            new_req = str(uuid.uuid4())[:8]
            context.user_data["links"][new_req] = f"ytsearch1:{title} {artist}"
            
            keyboard = [
                [InlineKeyboardButton("🎵 Download This Song", callback_data=f"askq|{new_req}")],
                [InlineKeyboardButton("Cancel", callback_data="cancel|000")]
            ]
            await query.edit_message_text(f"🎵 Song Found!\nTitle: {title}\nArtist: {artist}\n\nSelect an option below:", reply_markup=InlineKeyboardMarkup(keyboard))
            
        except Exception:
            await query.edit_message_text("Sorry, the song recognition failed.")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    if data.startswith("transcribe|"):
        req_id = data.split("|")[1]
        session = GLOBAL_SESSION.pop(req_id, None)
        if not session:
            await query.edit_message_text("Session expired. Please send the voice note again.")
            return
            
        await query.edit_message_text("📝 AI is transcribing the audio...")
        job_dir = os.path.join(DOWNLOAD_DIR, str(uuid.uuid4())[:8])
        os.makedirs(job_dir, exist_ok=True)
        try:
            tg_file = await context.bot.get_file(session["file_id"])
            temp_path = os.path.join(job_dir, "input_audio.oga")
            await tg_file.download_to_drive(temp_path)
            
            transcription = await asyncio.to_thread(get_groq_transcription, temp_path)
            if len(transcription) > 4000:
                transcription = transcription[:4000] + "...\n(Truncated)"
            await query.edit_message_text(f"📝 **Transcription:**\n\n{transcription}", parse_mode="Markdown")
        except ValueError as ve:
            await query.edit_message_text(f"⚠️ {ve}")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Transcription failed: {str(e)[:100]}")
        finally:
            shutil.rmtree(job_dir, ignore_errors=True)
        return
        
    if data.startswith("summary|"):
        req_id = data.split("|")[1]
        url = context.user_data.get("links", {}).get(req_id, None)
        if not url:
            await query.edit_message_text("Session expired. Please send the link again.")
            return
            
        await query.edit_message_text("📋 Fetching video transcript for AI summarization...")
        try:
            def fetch_info():
                opts = {
                    "skip_download": True,
                    "quiet": True,
                    "extractor_args": {"youtube": {"player_client": ["web", "tv_embedded", "ios"]}}
                }
                if os.path.exists(COOKIES_FILE_PATH) and os.path.getsize(COOKIES_FILE_PATH) > 0:
                    opts["cookiefile"] = COOKIES_FILE_PATH
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
                    
            info = await asyncio.to_thread(fetch_info)
            
            target_langs = ['en', 'en-US', 'en-GB', 'en-orig', 'a.en', 'my', 'my-MM', 'a.my']
            sub_url = None
            
            for lang_dict in [info.get('subtitles', {}), info.get('automatic_captions', {})]:
                for lang in target_langs:
                    if lang in lang_dict:
                        for f in lang_dict[lang]:
                            if f.get('ext') == 'json3':
                                sub_url = f.get('url')
                                break
                        if not sub_url and lang_dict[lang]:
                            sub_url = lang_dict[lang][0].get('url')
                    if sub_url:
                        break
                if sub_url:
                    break
            
            if not sub_url:
                for lang_dict in [info.get('subtitles', {}), info.get('automatic_captions', {})]:
                    for key, formats in lang_dict.items():
                        if formats:
                            sub_url = formats[0].get('url')
                            break
                    if sub_url:
                        break
                        
            if not sub_url:
                await query.edit_message_text("⚠️ No suitable captions were found for this video. The AI cannot summarize it.")
                return
                
            resp = await asyncio.to_thread(requests.get, sub_url, timeout=10)
            
            if 'json3' in sub_url:
                data_json = resp.json()
                events = data_json.get('events', [])
                text_parts = []
                for ev in events:
                    if 'segs' in ev:
                        for seg in ev['segs']:
                            if 'utf8' in seg:
                                text_parts.append(seg['utf8'])
                clean_text = "".join(text_parts).replace('\n', ' ').strip()
            else:
                raw_text = resp.text
                clean_text = re.sub(r'<[^>]+>', ' ', raw_text)
                clean_text = re.sub(r'&\w+;', ' ', clean_text)
                clean_text = re.sub(r'\n+', ' ', clean_text).strip()
                
            if not clean_text or len(clean_text) < 10:
                await query.edit_message_text("⚠️ Caption track is empty or unreadable.")
                return
                
            clean_text = clean_text[:20000]
            await query.edit_message_text("📋 AI is analyzing the transcript...")
            summary = await asyncio.to_thread(get_groq_summary, clean_text)
            await query.edit_message_text(f"📋 **AI Video Summary:**\n\n{summary}", parse_mode="Markdown")
            
        except ValueError as ve:
            await query.edit_message_text(f"⚠️ {ve}")
        except Exception as e:
            await query.edit_message_text(f"⚠️ Summarization failed: {str(e)[:100]}")
        return

    if data.startswith("cancel"):
        return await query.edit_message_text("Canceled.")

    if data.startswith("askq"):
        parts = data.split("|")
        prefix, req_id = parts[0], parts[1]
        dl_prefix = "dlpl" if "pl" in prefix else "dl"
        
        keyboard = [
            [InlineKeyboardButton("Standard Audio (MP3)", callback_data=f"{dl_prefix}|{req_id}|standard")],
            [InlineKeyboardButton("Medium Audio (MP3)", callback_data=f"{dl_prefix}|{req_id}|medium")],
            [InlineKeyboardButton("High Audio (MP3)", callback_data=f"{dl_prefix}|{req_id}|high")],
            [InlineKeyboardButton("Voice Message (OGG)", callback_data=f"{dl_prefix}|{req_id}|voice")],
            [InlineKeyboardButton("Cancel", callback_data="cancel|000")]
        ]
        return await query.edit_message_text("Select audio quality:", reply_markup=InlineKeyboardMarkup(keyboard))

    parts = data.split("|")
    prefix = parts[0]

    if prefix in ("dlv", "dlvi", "dlvc"):
        if user_active_tasks.get(user_id, 0) > 0:
            await query.edit_message_text("Please wait. Your previous file is still processing.")
            return

        req_id = parts[1]
        start_sec, end_sec = None, None
        
        if prefix in ("dlvi", "dlvc"):
            session = GLOBAL_SESSION.pop(req_id, None)
            if not session:
                await query.edit_message_text("Your session expired. Please search or clip again.")
                return
            url = session.get("url")
            start_sec = session.get("start")
            end_sec = session.get("end")
        else:
            url = context.user_data.get("links", {}).pop(req_id, None)
            if not url:
                await query.edit_message_text("Your session expired. Please send the link again.")
                return

        user_last_request[user_id] = time.time()
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
        
        try:
            msg = "⚡ Starting download..." if start_sec is None else "✂️ Added clip to queue..."
            await query.edit_message_text(msg)
            await asyncio.wait_for(execute_video_download(update.effective_chat.id, url, context, query.message, start_sec, end_sec, req_id=req_id), timeout=PROCESS_TIMEOUT)
        except (asyncio.TimeoutError, TimeoutError):
            GLOBAL_CANCEL_FLAGS.add(req_id)
            await context.bot.send_message(update.effective_chat.id, "⏳ The request took too long (over 4 minutes) and was automatically canceled. Please try again.")
        except InterruptedError:
            pass
        finally:
            GLOBAL_CANCEL_FLAGS.discard(req_id)
            if user_active_tasks[user_id] > 0:
                user_active_tasks[user_id] -= 1
        return

    if prefix in ("dl", "dlpl", "dli", "dlc"):
        if user_active_tasks.get(user_id, 0) > 0:
            await query.edit_message_text("Please wait. Your previous file is still processing.")
            return

        req_id = parts[1]
        quality_key = parts[2]
        start_sec, end_sec = None, None
        
        if prefix in ("dli", "dlc"):
            session = GLOBAL_SESSION.pop(req_id, None)
            if not session:
                await query.edit_message_text("Your session expired. Please search or clip again.")
                return
            url = session.get("url")
            start_sec = session.get("start")
            end_sec = session.get("end")
        else:
            url = context.user_data.get("links", {}).pop(req_id, None)
            if not url:
                await query.edit_message_text("Your session expired. Please send the link again.")
                return
            
        user_last_request[user_id] = time.time()
        preset = QUALITY_PRESETS.get(quality_key, QUALITY_PRESETS["medium"])
        user_active_tasks[user_id] = user_active_tasks.get(user_id, 0) + 1
        
        try:
            if prefix in ("dl", "dli", "dlc"):
                msg = f"⚡ Starting download ({preset['label']})..." if start_sec is None else f"✂️ Added clip to queue ({preset['label']})..."
                await query.edit_message_text(msg)
                await asyncio.wait_for(execute_download(update.effective_chat.id, url, quality_key, context, query.message, "", start_sec, end_sec, req_id=req_id), timeout=PROCESS_TIMEOUT)
            else:
                await query.edit_message_text("📂 Loading playlist details...")
                def get_playlist():
                    opts = {
                        "extract_flat": True,
                        "quiet": True,
                        "extractor_args": {"youtube": {"player_client": ["tv_embedded", "android_vr", "ios"]}}
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
                
                await query.edit_message_text(f"📂 Playlist queued: {len(entries)} tracks. Downloading sequentially.")
                
                for i, entry in enumerate(entries, 1):
                    if req_id in GLOBAL_CANCEL_FLAGS:
                        break
                    if not entry.get("url"): 
                        continue
                    status_msg = await context.bot.send_message(update.effective_chat.id, f"[Track {i}/{len(entries)}] ⚡ Starting download...")
                    await asyncio.wait_for(execute_download(update.effective_chat.id, entry["url"], quality_key, context, status_msg, f"[Track {i}/{len(entries)}] ", req_id=req_id), timeout=PROCESS_TIMEOUT)
                    gc.collect()
                    await asyncio.sleep(1.5)
        except (asyncio.TimeoutError, TimeoutError):
            GLOBAL_CANCEL_FLAGS.add(req_id)
            await context.bot.send_message(update.effective_chat.id, "⏳ The request took too long (over 4 minutes) and was automatically canceled. Please try again.")
        except InterruptedError:
            pass
        finally:
            GLOBAL_CANCEL_FLAGS.discard(req_id)
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
    application.add_handler(CommandHandler("edit", edit_tags_command))
    application.add_handler(InlineQueryHandler(inline_search))
    application.add_handler(CallbackQueryHandler(handle_callbacks))
    application.add_handler(MessageHandler(filters.Sticker.ALL | filters.ANIMATION, handle_sticker_gif))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_audio_recognition))
    
    application.add_handler(MessageHandler(filters.PHOTO & filters.REPLY & filters.CaptionRegex(r"^/edit"), edit_tags_command))
    application.add_handler(MessageHandler(filters.Document.ALL | (filters.PHOTO & ~filters.REPLY) | filters.VIDEO, handle_media))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link_message))
    
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()