import asyncio
import logging
import re
import os
import json
import requests
import glob
import shutil
import subprocess
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, InputMediaPhoto, InputMediaVideo
)
from shazamio import Shazam
import yt_dlp
from PIL import Image

load_dotenv()

API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")

IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_TXT = os.path.join(BASE_DIR, "cookies.txt")
COOKIES_JSON = os.path.join(BASE_DIR, "cookies.json")
# temp converted file when cookies.json is in JSON array format
COOKIES_CONVERTED = os.path.join(BASE_DIR, "cookies_converted.txt")
SESSION_PATH = os.path.join(BASE_DIR, "bot_session")

app = Client(
    name=SESSION_PATH,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user_data = {}
cancel_flags = {}
LINK_REGEX = re.compile(r'https?://\S+')
logging.basicConfig(level=logging.INFO)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

# ================== COBALT BACKUP API (INSTAGRAM FALLBACK #1) ==================
# Primary: ClipsSaver -> Backup: Cobalt -> Last resort: gallery-dl
# Override via env: COBALT_API_URL / COBALT_API_KEY
COBALT_API_URL = os.getenv("COBALT_API_URL", "https://api.cobalt.tools/")
COBALT_API_KEY = os.getenv(
    "COBALT_API_KEY",
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJkd1VGak9WRCIsInN1YiI6IkNzUmhtdmtWIiwiZXhwIjoxNzg5NDA2MzYzfQ.M1rFSh0S--siq0pm-VyGcJt_cAbyseZHSnYIgMAnUac",
)

def is_cancelled(key: str) -> bool:
    return cancel_flags.get(key, False)

def get_cancel_markup(proc_msg_id: int, user_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{proc_msg_id}:{user_id}")]])

# ================== COOKIES HANDLING ==================
def convert_json_cookies_to_netscape(json_path: str, out_path: str) -> bool:
    """
    Convert cookies.json (browser export) to Netscape cookies.txt format for yt-dlp.
    Supports:
      - List of cookie dicts (most common)
      - Dict with 'cookies' key
    Returns True if conversion succeeded.
    """
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        cookies = []
        if isinstance(data, list):
            cookies = data
        elif isinstance(data, dict) and "cookies" in data:
            cookies = data["cookies"]
        else:
            # unknown structure, try to find list inside
            for v in data.values() if isinstance(data, dict) else []:
                if isinstance(v, list) and v and isinstance(v[0], dict) and "name" in v[0]:
                    cookies = v
                    break

        if not cookies:
            logging.error(f"cookies.json: no cookies found in {json_path}")
            return False

        with open(out_path, "w", encoding="utf-8", newline="\n") as out:
            out.write("# Netscape HTTP Cookie File\n")
            out.write("# Converted from cookies.json for yt-dlp\n")
            out.write("# https://curl.haxx.se/rfc/cookie_spec.html\n\n")
            for c in cookies:
                try:
                    domain = c.get("domain", "")
                    if not domain:
                        continue
                    # Netscape fields: domain, flag, path, secure, expiration, name, value
                    flag = "TRUE" if domain.startswith(".") else "FALSE"
                    # Also respect hostOnly field if present
                    if "hostOnly" in c:
                        flag = "FALSE" if c.get("hostOnly") else "TRUE"
                    path = c.get("path", "/")
                    secure = "TRUE" if c.get("secure") else "FALSE"
                    # expirationDate vs expiry vs expires
                    exp = c.get("expirationDate") or c.get("expires") or c.get("expiry") or 0
                    # yt-dlp expects int, ensure not float
                    try:
                        exp = int(float(exp))
                    except:
                        exp = 0
                    name = c.get("name", "")
                    value = c.get("value", "")
                    out.write(f"{domain}\t{flag}\t{path}\t{secure}\t{exp}\t{name}\t{value}\n")
                except Exception as e:
                    logging.warning(f"Skipping cookie {c.get('name')}: {e}")
                    continue
        logging.info(f"Converted {len(cookies)} cookies from {json_path} -> {out_path}")
        return True
    except Exception as e:
        logging.error(f"Failed to convert cookies.json: {e}")
        return False

def get_cookiefile() -> str | None:
    """
    Resolve best cookie file for yt-dlp.
    Priority:
      1. cookies.txt if exists and non-empty
      2. cookies.json if exists:
         - if it's already Netscape format -> use directly
         - if it's JSON -> convert to cookies_converted.txt and use that
    Returns path or None.
    """
    # 1. Prefer cookies.txt (Netscape) if valid
    if os.path.exists(COOKIES_TXT) and os.path.getsize(COOKIES_TXT) > 10:
        return COOKIES_TXT

    if os.path.exists(COOKIES_JSON) and os.path.getsize(COOKIES_JSON) > 10:
        # Peek first bytes to detect format
        try:
            with open(COOKIES_JSON, "r", encoding="utf-8") as f:
                head = f.read(2048).strip()
            if head.startswith("# Netscape") or "Netscape HTTP Cookie File" in head:
                logging.info(f"Using {COOKIES_JSON} as Netscape cookie file directly")
                return COOKIES_JSON
            if head.startswith("[") or head.startswith("{"):
                # JSON -> needs conversion
                if convert_json_cookies_to_netscape(COOKIES_JSON, COOKIES_CONVERTED):
                    return COOKIES_CONVERTED
                else:
                    logging.error("cookies.json conversion failed, yt-dlp will run without cookies")
                    return None
            # Fallback: try json load
            if convert_json_cookies_to_netscape(COOKIES_JSON, COOKIES_CONVERTED):
                return COOKIES_CONVERTED
        except Exception as e:
            logging.error(f"get_cookiefile error: {e}")
            return None

    # Check converted file as last resort (if user manually placed)
    if os.path.exists(COOKIES_CONVERTED):
        return COOKIES_CONVERTED

    return None

# ================== UTILITIES ==================
def clean_reddit_url(url):
    if "/s/" in url:
        try:
            headers = {'User-Agent': USER_AGENT}
            response = requests.head(url, headers=headers, allow_redirects=True, timeout=5)
            url = response.url
        except Exception as e:
            logging.error(f"Reddit redirect failed: {e}")
    if "redd.it" in url:
        post_id = url.split("/")[-1].split("?")[0]
        return f"https://www.reddit.com/comments/{post_id}"
    match = re.search(r'(https?://(www\.|old\.|new\.|v\.)?reddit.com/[^\s?]+)', url)
    return match.group(1) if match else url

def is_instagram_url(url):
    return 'instagram.com' in url or 'instagr.am' in url

def is_twitter_url(url):
    return 'twitter.com' in url or 'x.com' in url

def is_youtube_community_url(url):
    return ('youtube.com/post/' in url or
            ('youtube.com' in url and '/community' in url and 'lb=' in url) or
            'youtube.com/shorts/' not in url and re.search(r'youtube\.com/.+/community\?', url))

def get_ydl_opts(extra_opts=None):
    ydl_opts = {
        'quiet': True,
        'noplaylist': False,
        'extract_flat': False,
        'ignoreerrors': True,
        'extractor_args': {'youtube': ['player_client=ios,mweb']},
        'http_headers': {'User-Agent': USER_AGENT}
    }
    cookiefile = get_cookiefile()
    if cookiefile and os.path.exists(cookiefile):
        ydl_opts['cookiefile'] = cookiefile
        logging.info(f"Using cookiefile: {cookiefile}")
    if extra_opts:
        ydl_opts.update(extra_opts)
    return ydl_opts

def get_video_info(url):
    with yt_dlp.YoutubeDL(get_ydl_opts()) as ydl:
        return ydl.extract_info(url, download=False)

def download_video(url, format_id):
    output = os.path.join(BASE_DIR, "downloads", f"vid_{os.urandom(8).hex()}.mp4")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    opts = get_ydl_opts({
        'format': f"{format_id}+bestaudio/best" if format_id != "best" else "bestvideo+bestaudio/best",
        'outtmpl': output,
        'merge_output_format': 'mp4',
    })

    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return output

# ================== IMAGE CONVERSION ==================
def convert_to_jpg(image_path):
    try:
        if not image_path.lower().endswith(('.jpg', '.jpeg')):
            img = Image.open(image_path).convert('RGB')
            new_path = os.path.splitext(image_path)[0] + ".jpg"
            img.save(new_path, "JPEG", quality=95, optimize=True)
            if os.path.exists(image_path):
                os.remove(image_path)
            return new_path
        return image_path
    except Exception as e:
        logging.error(f"Image conversion failed for {image_path}: {e}")
        return image_path

# ================== CLIPSSAVER API (INSTAGRAM ONLY) ==================
def fetch_clipssaver_data(url: str):
    is_story = "/stories/" in url
    endpoint_type = "download-story" if is_story else "download-post"
    api_url = f"https://clipssaver.com/api/instagram/instagramDownloader/{endpoint_type}"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://clipssaver.com",
        "Referer": "https://clipssaver.com/instagram-profile-downloader",
        "User-Agent": USER_AGENT
    }

    payload = {"url": url}

    last_exc = None
    for attempt in range(3):
        try:
            response = requests.post(api_url, json=payload, headers=headers, timeout=30)
            response.raise_for_status()
            return response.json()
        except Exception as e:
            last_exc = e
            logging.warning(f"ClipsSaver attempt {attempt+1}/3 failed: {e}")
            if attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))
    raise last_exc


# ================== COBALT API (INSTAGRAM BACKUP) ==================
def _cobalt_guess_is_video(filename: str, direct_url: str) -> bool:
    fn = (filename or "").lower()
    u = (direct_url or "").split("?")[0].lower()
    image_exts = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".heic", ".heif", ".avif", ".jfif", ".tiff")
    video_exts = (".mp4", ".mov", ".mkv", ".webm", ".m4v", ".gif")
    if fn.endswith(image_exts) or u.endswith(image_exts):
        return False
    if fn.endswith(video_exts) or u.endswith(video_exts):
        return True
    # Cobalt picker uses explicit types; for redirect default to video (IG reels/posts are video-heavy)
    # but if filename hints image, we already returned False above.
    if fn.endswith((".mp3", ".m4a", ".opus", ".ogg", ".wav")):
        return True  # treat audio as video-file path; sender will upload as video (or extend later)
    return True


def fetch_cobalt_media_list(url: str):
    """Call Cobalt API and return [(direct_media_url, is_video), ...].

    Handles Cobalt responses:
      - {"status": "redirect"/"tunnel", "url": "...", "filename": "..."}
      - {"status": "picker", "picker": [{"type": "video"/"photo"/"gif", "url": "..."}, ...]}
    Raises on {"status": "error"} or unexpected payload.
    """
    if not COBALT_API_KEY:
        raise RuntimeError("COBALT_API_KEY is not configured")

    # Ensure trailing slash: Cobalt expects POST / (your capture shows :path /)
    api_url = COBALT_API_URL if COBALT_API_URL.endswith("/") else COBALT_API_URL + "/"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {COBALT_API_KEY}",
        "Origin": "https://cobalt.tools",
        "Referer": "https://cobalt.tools/",
        "User-Agent": USER_AGENT,
    }
    payload = {
        "localProcessing": "preferred",
        "url": url,
    }

    last_exc = None
    for attempt in range(2):
        try:
            resp = requests.post(api_url, json=payload, headers=headers, timeout=30)
            # Cobalt returns 200 with {"status": "error", ...} on logical failures
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"Unexpected Cobalt response: {str(data)[:300]}")

            status = data.get("status")
            if status in ("redirect", "tunnel"):
                direct = data.get("url")
                if not direct:
                    raise RuntimeError(f"Cobalt {status} missing url: {str(data)[:300]}")
                filename = data.get("filename", "")
                return [(direct, _cobalt_guess_is_video(filename, direct))]

            if status == "picker":
                picker = data.get("picker") or []
                media = []
                for item in picker:
                    if not isinstance(item, dict):
                        continue
                    u = item.get("url")
                    if not u:
                        continue
                    typ = (item.get("type") or "").lower()
                    if typ in ("photo", "image", "picture"):
                        media.append((u, False))
                    elif typ in ("video", "gif", "loop"):
                        media.append((u, True))
                    else:
                        # Fallback to extension guess
                        fn = item.get("filename", "") or ""
                        media.append((u, _cobalt_guess_is_video(fn, u)))
                if not media:
                    raise RuntimeError(f"Cobalt picker empty: {str(data)[:300]}")
                return media

            if status == "error":
                err = data.get("error") or data
                raise RuntimeError(f"Cobalt error: {str(err)[:400]}")

            raise RuntimeError(f"Unexpected Cobalt status: {str(data)[:400]}")
        except Exception as e:
            last_exc = e
            # Don't retry logical Cobalt errors (status=error) — only transport errors
            msg = str(e)
            if "Cobalt error:" in msg or "Unexpected Cobalt" in msg or "picker empty" in msg:
                raise
            logging.warning(f"Cobalt attempt {attempt+1}/2 failed: {e}")
            if attempt < 1:
                import time
                time.sleep(2)
    raise last_exc


async def run_cobalt_fallback(client: Client, message: Message, processing_msg: Message, url: str):
    """Backup #1 for Instagram: Cobalt API. Shares cancel_key with caller.

    Downloads via Cobalt direct URL(s) and uploads to Telegram.
    Raises on any failure so caller can fall through to gallery-dl.
    """
    cancel_key = f"{message.chat.id}:{processing_msg.id}"
    if cancel_key not in cancel_flags:
        cancel_flags[cancel_key] = False
    cancel_markup = get_cancel_markup(processing_msg.id, message.from_user.id)
    try:
        await processing_msg.edit_text("⚠️ Clipsaver failed, trying backup API (Cobalt)...", reply_markup=cancel_markup)
    except:
        pass

    job_dir = os.path.join(BASE_DIR, "downloads", f"cobalt_{os.urandom(8).hex()}")
    os.makedirs(job_dir, exist_ok=True)

    try:
        media_list = await asyncio.to_thread(fetch_cobalt_media_list, url)
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        if not media_list:
            raise RuntimeError("Cobalt returned no media")

        try:
            await processing_msg.edit_text(f"⬇️ Downloading {len(media_list)} item(s) via backup...", reply_markup=cancel_markup)
        except:
            pass

        def download_file(media_url, path):
            headers = {"User-Agent": USER_AGENT, "Referer": "https://www.instagram.com/"}
            res = requests.get(media_url, headers=headers, stream=True, timeout=60)
            res.raise_for_status()
            with open(path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
            return path

        downloaded_files = []
        for idx, (m_url, is_video) in enumerate(media_list):
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            ext = ".mp4" if is_video else ".jpg"
            file_path = os.path.join(job_dir, f"media_{idx}{ext}")
            await asyncio.to_thread(download_file, m_url, file_path)

            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return

            if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
                if not is_video:
                    file_path = convert_to_jpg(file_path)
                downloaded_files.append((file_path, is_video))

        if not downloaded_files:
            raise RuntimeError("Cobalt download produced no files")

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await processing_msg.edit_text("📤 Uploading to Telegram...", reply_markup=cancel_markup)
        except:
            pass

        images = [p for p, is_vid in downloaded_files if not is_vid]
        videos = [p for p, is_vid in downloaded_files if is_vid]

        if images:
            if len(images) == 1:
                if is_cancelled(cancel_key):
                    try:
                        await processing_msg.edit_text("❌ Cancelled.")
                    except:
                        pass
                    return
                await client.send_photo(chat_id=message.chat.id, photo=images[0], reply_to_message_id=message.id)
            else:
                for i in range(0, len(images), 10):
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    media_group = [InputMediaPhoto(img) for img in images[i:i+10]]
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1)

        if videos:
            if len(videos) == 1:
                if is_cancelled(cancel_key):
                    try:
                        await processing_msg.edit_text("❌ Cancelled.")
                    except:
                        pass
                    return
                await client.send_video(chat_id=message.chat.id, video=videos[0], reply_to_message_id=message.id)
            else:
                for i in range(0, len(videos), 10):
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    media_group = [InputMediaVideo(vid) for vid in videos[i:i+10]]
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1)

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await processing_msg.delete()
        except:
            pass
    finally:
        # Don't pop cancel_key here — owner (run_clipssaver_fallback) owns it.
        # Second pop there is harmless, but popping here would break caller's cancelled checks.
        if os.path.exists(job_dir):
            await asyncio.to_thread(shutil.rmtree, job_dir, ignore_errors=True)


async def run_clipssaver_fallback(client: Client, message: Message, processing_msg: Message, url: str):
    cancel_key = f"{message.chat.id}:{processing_msg.id}"
    cancel_flags[cancel_key] = False
    cancel_markup = get_cancel_markup(processing_msg.id, message.from_user.id)
    try:
        await processing_msg.edit_text("⚡ Instagram detected. Processing media...", reply_markup=cancel_markup)
    except:
        pass

    job_dir = os.path.join(BASE_DIR, "downloads", f"cs_local_{os.urandom(8).hex()}")
    os.makedirs(job_dir, exist_ok=True)

    try:
        data = await asyncio.to_thread(fetch_clipssaver_data, url)
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return
        media_list = []

        if isinstance(data, dict):
            post_data = data.get("data", {}).get("post", {})
            
            if isinstance(post_data, dict):
                sidecar = post_data.get("edge_sidecar_to_children", {}).get("edges", [])
                if sidecar:
                    for edge in sidecar:
                        node = edge.get("node", {})
                        if node.get("is_video") and node.get("video_url"):
                            media_list.append((node.get("video_url"), True))
                        elif node.get("display_url"):
                            media_list.append((node.get("display_url"), False))

                if not media_list:
                    if post_data.get("is_video") or post_data.get("video_url"):
                        v_url = post_data.get("video_url") or post_data.get("download_url") or post_data.get("url")
                        if v_url:
                            media_list.append((v_url, True))
                    else:
                        img_url = post_data.get("display_url") or post_data.get("thumbnail_src") or post_data.get("download_url")
                        if img_url:
                            media_list.append((img_url, False))

        if not media_list:
            logging.error(f"No media URLs found in payload: {data}")
            raise RuntimeError("ClipsSaver returned no media")

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await processing_msg.edit_text(f"⬇️ Downloading {len(media_list)} item(s)...", reply_markup=cancel_markup)
        except:
            pass

        downloaded_files = []

        def download_file(media_url, path):
            headers = {"User-Agent": USER_AGENT}
            res = requests.get(media_url, headers=headers, stream=True, timeout=30)
            res.raise_for_status()
            with open(path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    f.write(chunk)
            return path

        for idx, (m_url, is_video) in enumerate(media_list):
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            ext = ".mp4" if is_video else ".jpg"
            file_path = os.path.join(job_dir, f"media_{idx}{ext}")
            await asyncio.to_thread(download_file, m_url, file_path)

            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return

            if os.path.exists(file_path):
                if not is_video:
                    file_path = convert_to_jpg(file_path)
                downloaded_files.append((file_path, is_video))

        if not downloaded_files:
            raise RuntimeError("Clipsaver download produced no files")

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await processing_msg.edit_text("📤 Uploading to Telegram...", reply_markup=cancel_markup)
        except:
            pass

        images = [p for p, is_vid in downloaded_files if not is_vid]
        videos = [p for p, is_vid in downloaded_files if is_vid]

        if images:
            if len(images) == 1:
                if is_cancelled(cancel_key):
                    try:
                        await processing_msg.edit_text("❌ Cancelled.")
                    except:
                        pass
                    return
                await client.send_photo(chat_id=message.chat.id, photo=images[0], reply_to_message_id=message.id)
            else:
                for i in range(0, len(images), 10):
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    media_group = [InputMediaPhoto(img) for img in images[i:i+10]]
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1)

        if videos:
            if len(videos) == 1:
                if is_cancelled(cancel_key):
                    try:
                        await processing_msg.edit_text("❌ Cancelled.")
                    except:
                        pass
                    return
                await client.send_video(chat_id=message.chat.id, video=videos[0], reply_to_message_id=message.id)
            else:
                for i in range(0, len(videos), 10):
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    media_group = [InputMediaVideo(vid) for vid in videos[i:i+10]]
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1)

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await processing_msg.delete()
        except:
            pass

    except Exception as e:
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return
        logging.error(f"ClipsSaver API error: {e}")
        # Chain: Clipsaver -> Cobalt (backup #1) -> gallery-dl (last resort)
        try:
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            await run_cobalt_fallback(client, message, processing_msg, url)
            return
        except Exception as cobalt_e:
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            logging.error(f"Cobalt backup also failed: {cobalt_e}")
        # Last resort: gallery-dl
        try:
            try:
                await processing_msg.edit_text("⚠️ Backup API failed, trying gallery-dl...", reply_markup=cancel_markup)
            except:
                pass
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            await run_gallery_dl_fallback(client, message, processing_msg, url)
            return
        except Exception as fallback_e:
            logging.error(f"Gallery-dl fallback also failed: {fallback_e}")
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return
        try:
            await processing_msg.edit_text(f"❌ Failed to process Instagram post.\n`{str(e)[:400]}`")
        except:
            pass
    finally:
        cancel_flags.pop(cancel_key, None)
        if os.path.exists(job_dir):
            await asyncio.to_thread(shutil.rmtree, job_dir, ignore_errors=True)


# ================== GALLERY-DL FALLBACK (for Instagram fallback + TikTok photo etc) ==================
async def run_gallery_dl_fallback(client: Client, message: Message, processing_msg: Message, url: str):
    cancel_key = f"{message.chat.id}:{processing_msg.id}"
    # Preserve existing flag if set by caller (e.g., clipssaver fallback), otherwise init
    if cancel_key not in cancel_flags:
        cancel_flags[cancel_key] = False
    cancel_markup = get_cancel_markup(processing_msg.id, message.from_user.id)
    try:
        await processing_msg.edit_text("📸 Trying gallery-dl fallback...", reply_markup=cancel_markup)
    except:
        pass

    job_dir = os.path.join(BASE_DIR, "downloads", f"ig_local_{os.urandom(8).hex()}")
    os.makedirs(job_dir, exist_ok=True)

    cmd = [
        "gallery-dl",
        "-d", job_dir,
        "--no-mtime",
        "--no-part",
        "--write-metadata",
        "-o", f"user-agent={USER_AGENT}",
        "-o", "sleep-request=3.0-7.0",
        "-o", "skip=true",
    ]

    # Attach cookies if available
    cookiefile = get_cookiefile()
    if cookiefile and os.path.exists(cookiefile):
        cmd.extend(["--cookies", cookiefile])

    # Add credentials fallback if available (for Instagram)
    if IG_USERNAME and IG_PASSWORD and IG_USERNAME != "." and IG_PASSWORD != ".":
        cmd.extend(["-u", IG_USERNAME, "-p", IG_PASSWORD])

    cmd.append(url)

    try:
        def run_gallery_dl():
            return subprocess.run(cmd, capture_output=True, text=True, timeout=360)

        process = await asyncio.to_thread(run_gallery_dl)

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        downloaded_files = []
        for root, _, files in os.walk(job_dir):
            for file in files:
                if not file.endswith(('.json', '.txt', '.html')):
                    downloaded_files.append(os.path.join(root, file))

        if not downloaded_files:
            error_output = process.stderr.strip() or process.stdout.strip() or "No output"
            logging.error(f"GALLERY-DL FAILED:\n{error_output}")
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            return await processing_msg.edit_text(f"❌ Gallery-dl failed.\n\nLog: `{error_output[:800]}`")

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await processing_msg.edit_text(f"📤 Uploading {len(downloaded_files)} items...", reply_markup=cancel_markup)
        except:
            pass

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        images = []
        videos = []
        audios = []
        for file_path in sorted(downloaded_files):
            ext = os.path.splitext(file_path)[1].lower()
            if ext in ['.mp4', '.mov', '.mkv', '.webm', '.gif', '.m4v']:
                videos.append(file_path)
            elif ext in ['.mp3', '.m4a', '.aac', '.opus', '.ogg', '.wav', '.flac', '.wma', '.mp2']:
                # TikTok photo posts include background music as mp3 - skip to avoid PHOTO_EXT_INVALID
                # keep for optional audio upload but don't treat as image
                audios.append(file_path)
                logging.info(f"Skipping audio file from gallery-dl: {file_path}")
                continue
            elif ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.heic', '.heif', '.avif', '.jfif', '.tiff']:
                jpg_path = convert_to_jpg(file_path)
                # convert_to_jpg returns original on failure - verify it's still an image
                if os.path.exists(jpg_path) and os.path.splitext(jpg_path)[1].lower() in ['.jpg', '.jpeg']:
                    images.append(jpg_path)
                else:
                    logging.warning(f"Skipping non-image after conversion: {jpg_path}")
            else:
                logging.warning(f"Skipping unknown file type: {file_path} (ext={ext})")
                continue

        # Upload images as media group where possible
        if images:
            if len(images) == 1:
                if is_cancelled(cancel_key):
                    try:
                        await processing_msg.edit_text("❌ Cancelled.")
                    except:
                        pass
                    return
                await client.send_photo(chat_id=message.chat.id, photo=images[0], reply_to_message_id=message.id)
            else:
                for i in range(0, len(images), 10):
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    media_group = [InputMediaPhoto(img) for img in images[i:i+10]]
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1.2)

        # Upload videos
        for vid in videos:
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            await client.send_video(chat_id=message.chat.id, video=vid, reply_to_message_id=message.id)
            await asyncio.sleep(0.8)

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await processing_msg.delete()
        except:
            pass

    except Exception as e:
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return
        logging.error(f"Gallery-dl error: {e}")
        try:
            await processing_msg.edit_text("❌ Failed to process content via gallery-dl.")
        except:
            pass
    finally:
        # Only pop if we created it; if caller created it (clipssaver), leave for caller finally
        # Check if processing_msg still indicates gallery flow alone - we pop safely
        # If clipssaver caller will also pop, second pop is harmless
        cancel_flags.pop(cancel_key, None)
        if os.path.exists(job_dir):
            await asyncio.to_thread(shutil.rmtree, job_dir, ignore_errors=True)

# ================== SHAZAM ==================
@app.on_message(filters.command("search") & filters.reply)
async def recognize_music_command(client: Client, message: Message):
    target = message.reply_to_message
    if not (target.audio or target.voice or target.video or target.document):
        return await message.reply_text("❌ Reply to audio/video file.")

    file_size = getattr(target.audio or target.voice or target.video or target.document, 'file_size', 0)
    if file_size > 50 * 1024 * 1024:
        return await message.reply_text("❌ File too large (max 50MB).")

    status_msg = await message.reply_text("🎧 Downloading media...")
    file_path = None

    try:
        file_path = await target.download(file_name=os.path.join(BASE_DIR, "downloads", f"shazam_{os.urandom(4).hex()}"))
        await status_msg.edit_text("🔍 Recognizing with Shazam...")

        shazam = Shazam()
        out = await shazam.recognize_song(file_path)

        if out and 'track' in out:
            track = out['track']
            title = track.get('title', 'Unknown')
            artist = track.get('subtitle', 'Unknown')
            share = track.get('share', {}).get('html', '')

            text = f"🎵 **Found!**\n\n**Title:** `{title}`\n**Artist:** `{artist}`"
            if share:
                text += f"\n\n🔗 [Shazam Link]({share})"
            await status_msg.edit_text(text, disable_web_page_preview=True)
        else:
            await status_msg.edit_text("❌ No match found.")
    except Exception as e:
        logging.error(f"Shazam error: {e}")
        await status_msg.edit_text("❌ Error during recognition.")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)

# ================== MUSIC SEARCH ==================
@app.on_message(filters.command("deezer") & filters.text)
async def music_search(client: Client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        return await message.reply_text("Usage: `/deezer song name`")

    query = parts[1].strip()
    status_msg = await message.reply_text(f"🔍 Searching: {query}...")

    try:
        results = await asyncio.to_thread(search_music, query)
        if not results:
            return await status_msg.edit_text("❌ No results.")

        buttons = []
        for track in results[:10]:
            title = track['title'][:35]
            channel = track['uploader'][:25]
            dur = track.get('duration')
            dur_str = f" ({dur//60}:{dur%60:02d})" if dur else ""
            buttons.append([InlineKeyboardButton(
                f"🎵 {title} - {channel}{dur_str}",
                callback_data=f"music:{track['id']}:{message.from_user.id}"
            )])

        await status_msg.edit_text(f"🎵 Results for: {query}", reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        logging.error(e)
        await status_msg.edit_text("❌ Search failed.")

@app.on_callback_query(filters.regex(r"^music:"))
async def music_download(client: Client, callback: CallbackQuery):
    _, video_id, user_id_str = callback.data.split(":")
    if callback.from_user.id != int(user_id_str):
        return await callback.answer("Not for you!", show_alert=True)

    url = f"https://www.youtube.com/watch?v={video_id}"
    await callback.edit_message_text("⬇️ Downloading...")

    try:
        file_path = await asyncio.to_thread(download_audio, url)
        await callback.edit_message_text("📤 Uploading...")
        await client.send_audio(
            chat_id=callback.message.chat.id,
            audio=file_path,
            caption="✅ Downloaded via Bot",
            reply_to_message_id=callback.message.reply_to_message_id
        )
        await callback.message.delete()
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logging.error(e)
        await callback.edit_message_text("❌ Download failed.")

def search_music(query):
    ydl_opts = get_ydl_opts({'extract_flat': True, 'noplaylist': True})
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch10:{query}", download=False)
        results = []
        for e in info.get('entries', []):
            if e:
                results.append({
                    'id': e.get('id'),
                    'title': e.get('title', 'Unknown'),
                    'uploader': e.get('uploader', 'Unknown'),
                    'duration': e.get('duration'),
                })
        return results

def download_audio(url):
    output = os.path.join(BASE_DIR, "downloads", f"music_{os.urandom(8).hex()}.mp3")
    os.makedirs(os.path.dirname(output), exist_ok=True)

    ydl_opts = get_ydl_opts({
        'format': 'bestaudio/best',
        'outtmpl': output,
        'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
    })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])

    mp3s = glob.glob(os.path.join(BASE_DIR, "downloads", "music_*.mp3"))
    return max(mp3s, key=os.path.getctime) if mp3s else output

# ================== TWITTER/X HANDLER ==================
def _format_count(n) -> str:
    """Format metric counts like X does: 950 -> '950', 1200 -> '1.2K', 2.3M etc."""
    try:
        n = int(n or 0)
    except:
        return ""
    if n is None:
        return ""
    if n < 0:
        n = 0
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = n / 1000
        s = f"{v:.1f}".rstrip("0").rstrip(".")
        return f"{s}K"
    if n < 1_000_000_000:
        v = n / 1_000_000
        s = f"{v:.1f}".rstrip("0").rstrip(".")
        return f"{s}M"
    v = n / 1_000_000_000
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{s}B"


def _load_tweet_font(size: int, bold: bool = False):
    """Find a usable TTF on both Linux (Railway/Docker) and Windows."""
    from PIL import ImageFont
    bold_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "C:\\Windows\\Fonts\\segoeuib.ttf",
        "arialbd.ttf",
    ]
    regular_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSans-Regular.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "C:\\Windows\\Fonts\\segoeui.ttf",
        "arial.ttf",
    ]
    for p in (bold_candidates if bold else regular_candidates):
        try:
            if os.path.exists(p) or "\\" not in p and "/" not in p:
                return ImageFont.truetype(p, size)
        except:
            continue
    # Last resort: try bare names (PIL + OS font lookup)
    for name in (["DejaVuSans-Bold.ttf", "arialbd.ttf"] if bold else ["DejaVuSans.ttf", "arial.ttf"]):
        try:
            return ImageFont.truetype(name, size)
        except:
            continue
    return ImageFont.load_default()


def _load_arabic_font(size: int, bold: bool = False):
    """Font with proper Persian/Arabic glyphs (Vazirmatn preferred).

    DejaVu/Arial have basic Arabic coverage, but Vazirmatn (downloaded in
    Dockerfile) renders Persian far better. Falls back gracefully.
    """
    from PIL import ImageFont
    # Note: Vazirmatn ships as a variable font — one file covers both weights.
    candidates = [
        "/usr/share/fonts/truetype/vazirmatn/Vazirmatn.ttf",
        "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf" if bold else "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansArabic-Bold.ttf" if bold else "/usr/share/fonts/opentype/noto/NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
    ]
    for p in candidates:
        try:
            if os.path.exists(p) or ("\\" not in p and "/" not in p):
                return ImageFont.truetype(p, size)
        except:
            continue
    return _load_tweet_font(size, bold=bold)


# ---- RTL (Persian/Arabic/Hebrew) support ----
# PIL has no bidi/shaping engine: without reshaping, Persian renders
# disconnected and in the wrong order ("messy"). arabic_reshaper fixes
# letter joining, python-bidi fixes visual order. Both are optional —
# if missing, text still renders (unshaped) instead of crashing.
try:
    import arabic_reshaper as _AR_RESHAPER
    from bidi.algorithm import get_display as _BIDI_DISPLAY
    _HAS_BIDI = True
except Exception:
    _AR_RESHAPER = None
    _BIDI_DISPLAY = None
    _HAS_BIDI = False

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF\u0590-\u05FF]")


def _has_arabic(s: str) -> bool:
    return bool(s and _ARABIC_RE.search(s))


def _visual(s: str) -> str:
    """Visual-order string for rendering: shaped + bidi if Arabic-script present."""
    if not s or not _HAS_BIDI or not _has_arabic(s):
        return s
    try:
        return _BIDI_DISPLAY(_AR_RESHAPER.reshape(s))
    except Exception:
        return s


def _extract_tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else ""


def _fetch_via_fxtwitter(tweet_id: str) -> dict:
    """Free, no-auth API that returns FULL text + counts + author + avatar.

    Docs: https://docs.fxembed.com/api/introduction  (endpoint /i/status/:id)
    """
    out: dict = {}
    if not tweet_id:
        return out
    try:
        resp = requests.get(
            f"https://api.fxtwitter.com/i/status/{tweet_id}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return out
        data = resp.json()
        tw = data.get("tweet") or data.get("post") or {}
        if not isinstance(tw, dict) or not tw:
            return out
        author = tw.get("author") or {}
        # full text: 'text' is complete (unlike og:description which truncates ~200 chars)
        text = tw.get("text") or tw.get("full_text") or tw.get("content") or ""
        if isinstance(text, str):
            import html as _html
            # Fx API returns entities escaped; unescape so links/amp show correctly
            text = _html.unescape(text).strip()
            # strip trailing t.co media link if media attached (X appends it, image shows it ugly)
            # keep it — it IS part of the tweet; only strip if you prefer cleaner cards.
            out["text"] = text
        # author fields
        out["display_name"] = author.get("name") or ""
        out["handle"] = author.get("screen_name") or author.get("handle") or ""
        out["avatar_url"] = author.get("avatar_url") or author.get("profile_image_url") or ""
        out["verified"] = bool(author.get("verified") or author.get("blue_verified"))
        # metrics — these are REAL counts, cookies not required for this API
        for src_key, dst_key in [
            ("replies", "replies"), ("reply_count", "replies"),
            ("retweets", "reposts"), ("retweet_count", "reposts"),
            ("likes", "likes"), ("like_count", "likes"), ("favorite_count", "likes"),
            ("views", "views"), ("view_count", "views"), ("impression_count", "views"),
            ("bookmarks", "bookmarks"), ("bookmark_count", "bookmarks"),
        ]:
            if dst_key not in out and tw.get(src_key) is not None:
                try:
                    out[dst_key] = int(tw.get(src_key) or 0)
                except:
                    pass
        created = tw.get("created_at") or tw.get("created_timestamp") or ""
        if isinstance(created, (int, float)) and created > 0:
            try:
                import datetime as _dt
                ts = created / 1000 if created > 1e12 else created
                out["created_at"] = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime("%I:%M %p · %b %d, %Y")
            except:
                pass
        elif isinstance(created, str) and created:
            out["created_at"] = created
    except Exception as e:
        logging.warning(f"fxtwitter API failed for {tweet_id}: {e}")
    return out


def _fetch_via_vxtwitter(tweet_id: str) -> dict:
    """Fallback mirror of the Fx API (same shape, different host)."""
    out: dict = {}
    if not tweet_id:
        return out
    for endpoint in (
        f"https://api.vxtwitter.com/i/status/{tweet_id}",
        f"https://api.vxtwitter.com/Twitter/status/{tweet_id}",
    ):
        try:
            resp = requests.get(
                endpoint,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            tw = data.get("tweet") or data.get("post") or data if isinstance(data, dict) else {}
            if not isinstance(tw, dict):
                continue
            # unwrap one more level if nested
            if "tweet" in tw and isinstance(tw["tweet"], dict):
                tw = tw["tweet"]
            text = tw.get("text") or tw.get("full_text") or ""
            if text and len(text) > len(out.get("text", "")):
                import html as _html
                out["text"] = _html.unescape(text).strip()
            author = tw.get("author") or tw.get("user") or {}
            if isinstance(author, dict):
                if not out.get("display_name"):
                    out["display_name"] = author.get("name") or ""
                if not out.get("handle"):
                    out["handle"] = author.get("screen_name") or ""
                if not out.get("avatar_url"):
                    out["avatar_url"] = author.get("avatar_url") or author.get("profile_image_url") or ""
                if author.get("verified"):
                    out["verified"] = True
            for src_key, dst_key in [
                ("replies", "replies"), ("retweets", "reposts"),
                ("likes", "likes"), ("views", "views"), ("bookmarks", "bookmarks"),
            ]:
                if dst_key not in out and isinstance(tw.get(src_key), int):
                    out[dst_key] = tw[src_key]
            if out.get("text"):
                break
        except Exception as e:
            logging.warning(f"vxtwitter API failed ({endpoint}): {e}")
            continue
    return out


def _enrich_from_gallerydl_json(job_dir: str, result: dict) -> dict:
    """gallery-dl --write-metadata drops JSON with FULL content + counts.

    This is where your Twitter cookies DO matter: with valid cookies
    gallery-dl can see the tweet and its numbers; without them it 401s.
    """
    try:
        for root, _, files in os.walk(job_dir):
            for f in files:
                if not f.endswith(".json"):
                    continue
                try:
                    with open(os.path.join(root, f), "r", encoding="utf-8") as fh:
                        data = json.load(fh)
                except:
                    continue
                candidates = []
                if isinstance(data, dict):
                    # gallery-dl twitter.json structure varies; scan a few likely spots
                    for key in ("content", "full_text", "text", "tweet_text"):
                        if isinstance(data.get(key), str) and data[key].strip():
                            candidates.append(data[key].strip())
                    # nested tweet object
                    tw = data.get("tweet") or data.get("post") or {}
                    if isinstance(tw, dict):
                        for key in ("content", "full_text", "text"):
                            if isinstance(tw.get(key), str) and tw[key].strip():
                                candidates.append(tw[key].strip())
                        # counts
                        mapping = {
                            "favorite_count": "likes", "like_count": "likes",
                            "retweet_count": "reposts", "repost_count": "reposts",
                            "reply_count": "replies", "comment_count": "replies",
                            "view_count": "views", "views": "views", "play_count": "views",
                            "bookmark_count": "bookmarks",
                        }
                        for sk, dk in mapping.items():
                            if dk not in result or not result.get(dk):
                                try:
                                    if tw.get(sk) is not None:
                                        result[dk] = int(tw[sk])
                                except:
                                    pass
                        user = tw.get("user") or tw.get("author") or {}
                        if isinstance(user, dict):
                            if not result.get("display_name"):
                                result["display_name"] = user.get("name") or user.get("nick") or ""
                            if not result.get("handle"):
                                result["handle"] = user.get("screen_name") or user.get("name") or ""
                            if not result.get("avatar_url"):
                                result["avatar_url"] = user.get("profile_image") or user.get("avatar") or ""
                    # top-level counts (flat jsonl style)
                    mapping_top = {
                        "favorite_count": "likes", "retweet_count": "reposts",
                        "reply_count": "replies", "view_count": "views",
                    }
                    for sk, dk in mapping_top.items():
                        if (dk not in result or not result.get(dk)) and data.get(sk) is not None:
                            try:
                                result[dk] = int(data[sk])
                            except:
                                pass
                if candidates:
                    best = max(candidates, key=len)
                    if len(best) > len(result.get("text", "")):
                        result["text"] = best
    except Exception as e:
        logging.warning(f"gallery-dl JSON enrich failed: {e}")
    return result


def _download_avatar_image(avatar_url: str, size: int):
    """Download profile pic for the card header. Returns PIL Image or None."""
    try:
        from PIL import Image as _Image
        if not avatar_url:
            return None
        # ask for bigger variant (Twitter strips _normal for full size)
        url = avatar_url.replace("_normal.", ".").replace("_bigger.", ".").replace("_mini.", ".")
        if url.startswith("//"):
            url = "https:" + url
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        r.raise_for_status()
        import io as _io
        img = _Image.open(_io.BytesIO(r.content)).convert("RGB")
        img = img.resize((size, size), _Image.LANCZOS)
        return img
    except Exception as e:
        logging.warning(f"avatar download failed: {e}")
        return None


def render_tweet_as_image(tweet_text: str, author: str = "", output_path: str = "tweet.jpg", metadata: dict | None = None) -> str:
    """Render a high-fidelity X-style card (dark theme) with REAL metrics.

    Fixes vs old version:
      - never truncates: full text is wrapped over as many lines as needed,
        preserving \\n line breaks and splitting over-long words/URLs.
      - real footer: reply / repost / like / view icons DRAWN with counts
        (old code drew 3 empty circles and no numbers).
      - header: avatar (downloaded when available), display name + verified
        badge, @handle · date, X logo.
      - works on Railway Linux (DejaVu/Liberation/Noto) and Windows (Arial).
    """
    try:
        from PIL import Image, ImageDraw, ImageFont

        meta = dict(metadata or {})
        full_text = (tweet_text or meta.get("text") or "").replace("\r\n", "\n").replace("\r", "\n")
        # NEVER cut the text here — truncation was bug #1. Only strip edge whitespace.
        full_text = full_text.strip()
        if not full_text:
            full_text = "(empty tweet)"

        display_name = meta.get("display_name") or author or "Unknown"
        handle = meta.get("handle") or author or ""
        handle = handle.lstrip("@")
        avatar_url = meta.get("avatar_url") or ""
        verified = bool(meta.get("verified"))
        created_at = meta.get("created_at") or ""

        replies = meta.get("replies")
        reposts = meta.get("reposts")
        likes = meta.get("likes")
        views = meta.get("views")

        # ---- theme (X dark) ----
        BG = (0, 0, 0)
        TEXT = (231, 233, 234)
        SECONDARY = (113, 118, 123)
        ACCENT = (29, 155, 240)
        BORDER = (47, 51, 54)

        width = 860
        pad_x = 32
        pad_y = 30
        avatar_size = 58
        avatar_gap = 16

        font_text = _load_tweet_font(25, bold=False)
        font_name = _load_tweet_font(21, bold=True)
        font_small = _load_tweet_font(17, bold=False)
        font_metric = _load_tweet_font(17, bold=False)
        font_logo = _load_tweet_font(26, bold=True)
        # Arabic-script capable fonts for Persian/Arabic text runs
        font_text_fa = _load_arabic_font(25, bold=False)
        font_name_fa = _load_arabic_font(21, bold=True)

        # measuring context
        meas_img = Image.new("RGB", (10, 10))
        meas = ImageDraw.Draw(meas_img)

        def text_w(s: str, f) -> int:
            try:
                bb = meas.textbbox((0, 0), s, font=f)
                return bb[2] - bb[0]
            except:
                return len(s) * (f.size // 2 if hasattr(f, "size") else 10)

        def line_h(f, extra: int = 10) -> int:
            try:
                bb = f.getbbox("Ay")
                return (bb[3] - bb[1]) + extra
            except:
                return (f.size if hasattr(f, "size") else 20) + extra

        text_area_w = width - pad_x * 2
        body_w = text_area_w  # body spans full width under header (like X mobile)

        # ---- wrap: preserve newlines (old code used text.split() and lost them),
        # ---- split single over-long words (URLs) char-by-char,
        # ---- and measure RTL paragraphs in visual (shaped) form so Persian
        # ---- lines wrap at the right width.
        def wrap_paragraph(para: str, f, fa_f, max_w: int, rtl: bool = False):
            words = para.split(" ")
            lines: list[str] = []
            cur = ""
            mf = fa_f if rtl else f

            def disp(s: str) -> str:
                return _visual(s) if rtl else s

            for w in words:
                if w == "":
                    # collapse multiple spaces to one (PIL collapses anyway)
                    continue
                test = (cur + " " + w).strip() if cur else w
                if text_w(disp(test), mf) <= max_w:
                    cur = test
                    continue
                # word doesn't fit on current line
                if cur:
                    lines.append(cur)
                    cur = ""
                # if the single word itself is wider than the line, hard-split it
                # (kept in logical form; shaping happens at draw time)
                if text_w(disp(w), mf) > max_w:
                    chunk = ""
                    for ch in w:
                        t2 = chunk + ch
                        if text_w(disp(t2), mf) <= max_w:
                            chunk = t2
                        else:
                            if chunk:
                                lines.append(chunk)
                            chunk = ch
                    cur = chunk
                else:
                    cur = w
            if cur:
                lines.append(cur)
            return lines if lines else [""]

        # wrapped entries are (line_text, is_rtl), all in logical order.
        # Shaping/bidi happens at draw time.
        wrapped: list[tuple[str, bool]] = []
        for para in full_text.split("\n"):
            if para.strip() == "":
                wrapped.append(("", False))  # blank line = paragraph gap
            else:
                rtl = _has_arabic(para)
                for ln in wrap_paragraph(para, font_text, font_text_fa, body_w, rtl):
                    wrapped.append((ln, rtl))
        if not wrapped:
            wrapped = [("", False)]

        th = line_h(font_text, 12)
        name_h = line_h(font_name, 6)
        small_h = line_h(font_small, 6)
        metric_h = line_h(font_metric, 6)

        header_h = max(avatar_size, name_h + small_h + 4)
        date_h = (small_h + 10) if created_at else 0
        gap_header_text = 18
        gap_text_date = 14 if created_at else 6
        divider_gap = 16
        footer_h = 30
        # total height grows with text — no cap, so long tweets are never cut (bug #1)
        height = pad_y + header_h + gap_header_text + len(wrapped) * th + gap_text_date + date_h + divider_gap + 1 + divider_gap + footer_h + pad_y

        img = Image.new("RGB", (width, height), BG)
        d = ImageDraw.Draw(img)
        # card border
        d.rounded_rectangle([2, 2, width - 3, height - 3], radius=22, outline=BORDER, width=2)

        y = pad_y

        # ---- header: avatar + name/handle + X logo ----
        avatar_img = _download_avatar_image(avatar_url, avatar_size)
        ax, ay = pad_x, y
        if avatar_img is not None:
            try:
                mask = Image.new("L", (avatar_size, avatar_size), 0)
                md = ImageDraw.Draw(mask)
                md.ellipse([0, 0, avatar_size, avatar_size], fill=255)
                img.paste(avatar_img, (ax, ay), mask)
            except:
                img.paste(avatar_img, (ax, ay))
        else:
            # fallback: gray circle with initial
            d.ellipse([ax, ay, ax + avatar_size, ay + avatar_size], fill=(51, 54, 57))
            initial = (display_name.strip()[:1] or "?").upper()
            try:
                ibb = d.textbbox((0, 0), initial, font=font_name)
                iw, ih = ibb[2] - ibb[0], ibb[3] - ibb[1]
                d.text((ax + (avatar_size - iw) / 2, ay + (avatar_size - ih) / 2 - 1), initial, fill=TEXT, font=font_name)
            except:
                pass

        tx = ax + avatar_size + avatar_gap
        # name (+ verified badge) — shape Persian/Arabic names correctly
        name_is_fa = _has_arabic(display_name)
        name_disp = _visual(display_name) if name_is_fa else display_name
        name_font_use = font_name_fa if name_is_fa else font_name
        d.text((tx, y), name_disp, fill=TEXT, font=name_font_use)
        nx = tx + text_w(name_disp, name_font_use)
        if verified:
            cx, cy, r = nx + 8, y + 4, 9
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
            # white check
            d.line([(cx - 5, cy), (cx - 1, cy + 4), (cx + 5, cy - 4)], fill=(255, 255, 255), width=2, joint="curve")
        # handle · date line
        sub = f"@{handle}" if handle else "@unknown"
        y += name_h
        d.text((tx, y), sub, fill=SECONDARY, font=font_small)
        # X logo top-right (drawn vector — the 𝕏 unicode glyph is missing
        # from DejaVu/Arial on Railway Linux and renders as tofu)
        try:
            lx = width - pad_x - 22
            ly = pad_y + 2
            d.line([(lx, ly), (lx + 18, ly + 18)], fill=TEXT, width=3, joint="curve")
            d.line([(lx + 18, ly), (lx, ly + 18)], fill=TEXT, width=3, joint="curve")
        except:
            pass

        y = pad_y + header_h + gap_header_text

        # ---- body: rich text (URLs / #hashtags / @mentions in blue) ----
        # LTR lines draw left-to-right; RTL (Persian/Arabic) lines draw
        # right-aligned in reversed segment order so mixed Persian+English
        # reads correctly. Arabic-script runs are shaped via _visual().
        def is_special(tok: str) -> bool:
            t = tok.strip(".,!?:;)\"'“”‘’…،؛؟«»")
            tl = t.lower()
            return tl.startswith("http://") or tl.startswith("https://") or tl.startswith("www.") or t.startswith("#") or t.startswith("@")

        def draw_token(x: int, tok: str, col) -> int:
            """Draw one token, shaping Arabic-script runs. Returns advance width."""
            if _has_arabic(tok):
                vis = _visual(tok)
                f = font_text_fa
            else:
                vis = tok
                f = font_text
            try:
                d.text((x, y), vis, fill=col, font=f)
                return text_w(vis, f)
            except Exception:
                if _has_arabic(tok):
                    return 0
                fb = tok.encode("ascii", "replace").decode()
                d.text((x, y), fb, fill=col, font=f)
                return text_w(fb, f)

        space_w = text_w(" ", font_text)
        for line, rtl in wrapped:
            if line == "":
                y += th // 2  # paragraph gap, keeps blank lines visible
                continue
            toks = [t for t in line.split(" ") if t != ""]
            if not toks:
                y += th // 2
                continue
            if not rtl:
                x = pad_x
                for tok in toks:
                    col = ACCENT if is_special(tok) else TEXT
                    x += draw_token(x, tok, col) + space_w
            else:
                # RTL line: measure everything, right-align, draw segments
                # in reverse (visual) order.
                specs = [(t, is_special(t)) for t in toks]
                widths = []
                total = 0
                for t, _sp in specs:
                    if _has_arabic(t):
                        w_ = text_w(_visual(t), font_text_fa)
                    else:
                        w_ = text_w(t, font_text)
                    widths.append(w_)
                    total += w_
                total += space_w * (len(specs) - 1)
                x = width - pad_x - total
                for (t, sp), w_ in zip(reversed(specs), reversed(widths)):
                    col = ACCENT if sp else TEXT
                    draw_token(x, t, col)
                    x += w_ + space_w
            y += th

        # ---- date line ----
        y += gap_text_date
        if created_at:
            try:
                d.text((pad_x, y), str(created_at), fill=SECONDARY, font=font_small)
            except:
                pass
            y += small_h + 10

        # ---- divider ----
        y += divider_gap - (10 if created_at else 0)
        d.line([(pad_x, y), (width - pad_x, y)], fill=BORDER, width=1)
        y += divider_gap

        # ---- footer: REAL icons + counts (bug #2 fix) ----
        icon_c = SECONDARY
        fy = y  # icon row top

        def draw_comment(x0: int, yc: int, s: int = 19):
            # speech-bubble outline + tail
            d.ellipse([x0, yc - s // 2, x0 + s, yc + s // 2], outline=icon_c, width=2)
            d.line([(x0 + 4, yc + s // 2 - 2), (x0 + 2, yc + s // 2 + 5), (x0 + 9, yc + s // 2 - 1)], fill=icon_c, width=2, joint="curve")

        def draw_repost(x0: int, yc: int, s: int = 20):
            # two opposing arrows
            d.line([(x0, yc - 4), (x0 + s, yc - 4)], fill=icon_c, width=2)
            d.line([(x0 + s, yc - 4), (x0 + s - 5, yc - 8)], fill=icon_c, width=2)
            d.line([(x0 + s, yc - 4), (x0 + s - 5, yc)], fill=icon_c, width=2)
            d.line([(x0 + s, yc + 4), (x0, yc + 4)], fill=icon_c, width=2)
            d.line([(x0, yc + 4), (x0 + 5, yc)], fill=icon_c, width=2)
            d.line([(x0, yc + 4), (x0 + 5, yc + 8)], fill=icon_c, width=2)

        def draw_heart(x0: int, yc: int, s: int = 21):
            # X-style outline heart: filled heart silhouette with an inset
            # knockout hole, rendered 4x supersampled for smooth edges.
            # (Pure outline arcs looked like a pretzel; this is a real heart.)
            SS = 4
            W = H = s * SS
            tile = Image.new("L", (W, H), 0)
            t = ImageDraw.Draw(tile)

            def _heart(dr, k: float):
                # heart shapes scaled by k about the heart center
                cx, cy = 0.50 * W, 0.50 * W

                def _pt(px: float, py: float):
                    return (cx + (px * W - cx) * k, cy + (py * W - cy) * k)

                def _bb(x1: float, y1: float, x2: float, y2: float):
                    return [_pt(x1, y1), _pt(x2, y2)]

                # two lobes
                dr.ellipse(_bb(0.08, 0.10, 0.52, 0.54), fill=255)
                dr.ellipse(_bb(0.48, 0.10, 0.92, 0.54), fill=255)
                # tapered bottom
                dr.polygon([_pt(0.10, 0.40), _pt(0.90, 0.40), _pt(0.50, 0.94)], fill=255)

            _heart(t, 1.0)  # outer silhouette...
            # ...minus inset silhouette -> clean outline of even weight
            inner = Image.new("L", (W, H), 0)
            ti = ImageDraw.Draw(inner)
            _heart(ti, 0.66)
            try:
                import PIL.ImageChops as _Chops
                tile = _Chops.subtract(tile, inner)
            except Exception:
                pass
            mask = tile.resize((s, s), Image.LANCZOS)
            solid = Image.new("RGB", (s, s), icon_c)
            img.paste(solid, (int(x0), int(yc - s // 2)), mask)

        def draw_views(x0: int, yc: int):
            # bar-chart (analytics) icon
            d.line([(x0, yc - 8), (x0, yc + 8)], fill=icon_c, width=2)
            d.line([(x0 + 6, yc - 3), (x0 + 6, yc + 8)], fill=icon_c, width=2)
            d.line([(x0 + 12, yc - 8), (x0 + 12, yc + 8)], fill=icon_c, width=2)
            d.line([(x0 + 18, yc + 1), (x0 + 18, yc + 8)], fill=icon_c, width=2)

        def draw_bookmark(x0: int, yc: int, s: int = 18):
            d.line([(x0, yc - s // 2), (x0, yc + s // 2)], fill=icon_c, width=2)
            d.line([(x0 + s - 4, yc - s // 2), (x0 + s - 4, yc + s // 2)], fill=icon_c, width=2)
            d.line([(x0, yc - s // 2), (x0 + s - 4, yc - s // 2)], fill=icon_c, width=2)
            d.line([(x0, yc + s // 2), (x0 + (s - 4) // 2, yc + s // 2 - 5)], fill=icon_c, width=2)
            d.line([(x0 + (s - 4) // 2, yc + s // 2 - 5), (x0 + s - 4, yc + s // 2)], fill=icon_c, width=2)

        def draw_share(x0: int, yc: int, s: int = 18):
            d.line([(x0, yc - 2), (x0 + s, yc - 2)], fill=icon_c, width=2)
            d.line([(x0, yc - 2), (x0, yc + s // 2)], fill=icon_c, width=2)
            d.line([(x0 + s, yc - 2), (x0 + s, yc + s // 2)], fill=icon_c, width=2)
            d.line([(x0, yc + s // 2), (x0 + s, yc + s // 2)], fill=icon_c, width=2)
            mx = x0 + s / 2
            d.line([(mx, yc - 10), (mx, yc + 2)], fill=icon_c, width=2)
            d.line([(mx, yc - 10), (mx - 4, yc - 6)], fill=icon_c, width=2)
            d.line([(mx, yc - 10), (mx + 4, yc - 6)], fill=icon_c, width=2)

        # 4 metric slots share the row; bookmark/share pinned right
        slots = 4
        slot_w = (text_area_w - 90) // slots
        items = [
            ("comment", replies),
            ("repost", reposts),
            ("heart", likes),
            ("views", views),
        ]
        for i, (kind, val) in enumerate(items):
            x0 = pad_x + i * slot_w
            yc = fy + 10
            if kind == "comment":
                draw_comment(x0, yc)
            elif kind == "repost":
                draw_repost(x0, yc)
            elif kind == "heart":
                draw_heart(x0, yc)
            else:
                draw_views(x0, yc)
            label = _format_count(val) if isinstance(val, int) else (_format_count(val) if val else "")
            if label:
                try:
                    d.text((x0 + 28, fy + 10 - metric_h // 2), label, fill=SECONDARY, font=font_metric)
                except:
                    pass
        # right-side icons (no counts, like X)
        draw_bookmark(width - pad_x - 52, fy + 10)
        draw_share(width - pad_x - 22, fy + 10)

        img.save(output_path, "JPEG", quality=92)
        return output_path
    except Exception as e:
        logging.error(f"Failed to render tweet image: {e}", exc_info=True)
        return ""

def fetch_tweet_metadata(url: str) -> dict:
    """Extract FULL tweet text + author + metrics.

    Priority (first hit with full data wins, longest text wins):
      1. FxTwitter API  — full text, likes/reposts/replies/views, avatar, date. No cookies needed.
      2. VxTwitter API  — same shape, mirror host.
      3. yt-dlp (WITH your cookies.txt/cookies.json) — full description + counts when API is down.
      4. og:description scrape — LAST resort, always truncated ~200 chars.
    Your Twitter cookies matter for (3) + gallery-dl media download, NOT for (1)/(2).
    """
    result: dict = {
        "text": "", "author": "", "display_name": "", "handle": "",
        "avatar_url": "", "verified": False, "created_at": "",
        "replies": None, "reposts": None, "likes": None, "views": None,
        "bookmarks": None, "has_media": False,
    }
    # handle from URL as immediate fallback
    m = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status", url or "")
    if m:
        result["author"] = m.group(1)
        result["handle"] = m.group(1)
        result["display_name"] = m.group(1)

    tweet_id = _extract_tweet_id(url)

    def _merge(src: dict):
        if not src:
            return
        if src.get("text") and len(src["text"]) > len(result.get("text", "")):
            result["text"] = src["text"]
        for k in ("display_name", "handle", "author", "avatar_url", "created_at"):
            if src.get(k) and not result.get(k):
                result[k] = src[k]
        if src.get("handle") and not result.get("author"):
            result["author"] = src["handle"]
        if src.get("verified"):
            result["verified"] = True
        for k in ("replies", "reposts", "likes", "views", "bookmarks"):
            if result.get(k) in (None, 0) and isinstance(src.get(k), int) and src[k] > 0:
                result[k] = src[k]

    # 1 + 2: free embed APIs (full text + counts)
    if tweet_id:
        _merge(_fetch_via_fxtwitter(tweet_id))
        # only hit mirror if we're still missing text or all counts
        if (not result["text"] or all(result.get(k) in (None, 0) for k in ("likes", "reposts", "replies", "views"))):
            _merge(_fetch_via_vxtwitter(tweet_id))

    # 3: yt-dlp with cookies (fills gaps + detects media when APIs are down)
    try:
        ydl_opts = get_ydl_opts({
            'noplaylist': True,
            'quiet': True,
            'skip_download': True,
            'no_warnings': True,
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info:
                # take the LONGEST text field — 'description' alone is sometimes short
                cands = [info.get('description') or "", info.get('full_title') or "",
                         info.get('title') or ""]
                best = max(cands, key=len).strip()
                # drop generic "X - ..." titles yt-dlp fabricates when it has no text
                if best and len(best) > len(result.get("text", "")):
                    result["text"] = best
                up = info.get('uploader') or info.get('creator') or info.get('uploader_id') or ""
                if up and not result.get("display_name"):
                    result["display_name"] = up
                    if not result.get("author"):
                        result["author"] = up
                # real counts from yt-dlp twitter extractor (needs cookies for some tweets)
                if result.get("likes") in (None, 0) and info.get("like_count"):
                    try:
                        result["likes"] = int(info["like_count"])
                    except:
                        pass
                if result.get("reposts") in (None, 0) and info.get("repost_count"):
                    try:
                        result["reposts"] = int(info["repost_count"])
                    except:
                        pass
                if result.get("replies") in (None, 0) and info.get("comment_count"):
                    try:
                        result["replies"] = int(info["comment_count"])
                    except:
                        pass
                if result.get("views") in (None, 0) and info.get("view_count"):
                    try:
                        result["views"] = int(info["view_count"])
                    except:
                        pass
                if info.get('formats') or info.get('thumbnails'):
                    result["has_media"] = True
    except Exception as e:
        logging.warning(f"yt-dlp tweet meta failed: {e}")

    # 4: last-resort scrape (TRUNCATED — only used if everything above failed)
    if not result.get("text"):
        try:
            headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            html = resp.text
            match = re.search(r'<meta\s+property="og:description"\s+content="([^"]*)"', html)
            if match:
                import html as _html
                text = _html.unescape(match.group(1))
                for prefix in [' posted by @', ' on X (', ' on Twitter (']:
                    if prefix in text:
                        text = text.split(prefix, 1)[0]
                        break
                result["text"] = text.strip()
            if 'pic.twitter.com' in html or 'pbs.twimg.com' in html:
                result["has_media"] = True
        except Exception as e:
            logging.warning(f"Tweet metadata extraction failed: {e}")

    if result.get("handle") and not result.get("author"):
        result["author"] = result["handle"]
    return result

async def run_twitter_handler(client: Client, message: Message, processing_msg: Message, url: str):
    cancel_key = f"{message.chat.id}:{processing_msg.id}"
    cancel_flags[cancel_key] = False
    cancel_markup = get_cancel_markup(processing_msg.id, message.from_user.id)
    try:
        await processing_msg.edit_text("⬇️ Downloading tweet media...", reply_markup=cancel_markup)
    except:
        pass

    job_dir = os.path.join(BASE_DIR, "downloads", f"tw_{os.urandom(8).hex()}")
    os.makedirs(job_dir, exist_ok=True)

    # Step 1: Extract tweet metadata (FULL text + counts + author)
    metadata = await asyncio.to_thread(fetch_tweet_metadata, url)
    tweet_text = metadata.get("text", "")
    author = metadata.get("handle") or metadata.get("author", "")

    if is_cancelled(cancel_key):
        try:
            await processing_msg.edit_text("❌ Cancelled.")
        except:
            pass
        return

    # Step 2: Use gallery-dl to download media (handles both photos and videos)
    try:
        await processing_msg.edit_text("📸 Downloading tweet media...", reply_markup=cancel_markup)
    except:
        pass

    try:
        cmd = [
            "gallery-dl",
            "-d", job_dir,
            "--no-mtime",
            "--no-part",
            "--write-metadata",
            "-o", f"user-agent={USER_AGENT}",
            "-o", "sleep-request=3.0-7.0",
            "-o", "skip=true",
        ]
        cookiefile = get_cookiefile()
        if cookiefile and os.path.exists(cookiefile):
            cmd.extend(["--cookies", cookiefile])
        cmd.append(url)

        def run_gl():
            return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        result = await asyncio.to_thread(run_gl)
        if result.returncode != 0:
            logging.warning(f"gallery-dl stderr: {result.stderr[:500]}")
    except Exception as e:
        logging.error(f"Twitter gallery-dl failed: {e}")

    if is_cancelled(cancel_key):
        try:
            await processing_msg.edit_text("❌ Cancelled.")
        except:
            pass
        return

    # Step 3: Collect downloaded files
    downloaded_files = []
    for root, _, files in os.walk(job_dir):
        for f in files:
            if not f.endswith(('.json', '.txt', '.html')):
                downloaded_files.append(os.path.join(root, f))

    # Step 3b: gallery-dl JSON has FULL text + counts (cookies help here) — merge it in
    try:
        metadata = _enrich_from_gallerydl_json(job_dir, metadata)
        tweet_text = metadata.get("text", "") or tweet_text
        author = metadata.get("handle") or metadata.get("author", "") or author
    except Exception as e:
        logging.warning(f"gallery-dl enrich failed: {e}")

    if not downloaded_files:
        # Try yt-dlp as last resort for video tweets
        try:
            def download_tweet_video():
                ydl_opts = get_ydl_opts({
                    'noplaylist': True,
                    'outtmpl': os.path.join(job_dir, '%(id)s.%(ext)s'),
                    'quiet': True,
                    'no_warnings': True,
                })
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            await asyncio.to_thread(download_tweet_video)
            # Re-collect files
            for root, _, files in os.walk(job_dir):
                for f in files:
                    if not f.endswith(('.json', '.txt', '.html')):
                        downloaded_files.append(os.path.join(root, f))
        except:
            pass

    # Step 4: If no media found, render tweet as image (FULL text + real counts)
    if not downloaded_files and tweet_text:
        try:
            await processing_msg.edit_text("📝 No media found, creating tweet image...", reply_markup=cancel_markup)
        except:
            pass
        tweet_img_path = os.path.join(job_dir, "tweet.jpg")
        rendered = await asyncio.to_thread(lambda: render_tweet_as_image(tweet_text, author, tweet_img_path, metadata))
        if rendered and os.path.exists(rendered):
            downloaded_files.append(rendered)

    if not downloaded_files:
        return await processing_msg.edit_text("❌ No media found in tweet.")

    if is_cancelled(cancel_key):
        try:
            await processing_msg.edit_text("❌ Cancelled.")
        except:
            pass
        return

    try:
        await processing_msg.edit_text("📤 Uploading to Telegram...", reply_markup=cancel_markup)
    except:
        pass

    full_text_caption = (tweet_text or "").strip()
    # Telegram caption limit is 1024 chars: put the head on the media itself
    # and deliver the REST as follow-up text message(s) so nothing is lost.
    caption = full_text_caption[:1024]
    overflow = full_text_caption[1024:]

    for file_path in downloaded_files:
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return
        ext = os.path.splitext(file_path)[1].lower()
        if ext in ['.mp4', '.mov', '.webm', '.m4v']:
            await client.send_video(
                chat_id=message.chat.id,
                video=file_path,
                caption=caption,
                reply_to_message_id=message.id
            )
        elif ext == '.gif':
            await client.send_animation(
                chat_id=message.chat.id,
                animation=file_path,
                caption=caption,
                reply_to_message_id=message.id
            )
        elif ext in ['.jpg', '.jpeg', '.png', '.webp', '.bmp', '.heic', '.heif']:
            if file_path.endswith("tweet.jpg"):
                # Tweet text image — attach full text as caption too so it stays
                # copyable/selectable even though it's already drawn in the image
                await client.send_photo(
                    chat_id=message.chat.id,
                    photo=file_path,
                    caption=caption or None,
                    reply_to_message_id=message.id
                )
            else:
                jpg_path = convert_to_jpg(file_path)
                await client.send_photo(
                    chat_id=message.chat.id,
                    photo=jpg_path,
                    caption=caption,
                    reply_to_message_id=message.id
                )

    # Overflow: caption couldn't hold the whole tweet → send the rest as text
    # (Telegram messages allow 4096 chars each).
    if overflow and overflow.strip():
        rest = overflow.strip()
        for i in range(0, len(rest), 4096):
            if is_cancelled(cancel_key):
                break
            try:
                await client.send_message(
                    chat_id=message.chat.id,
                    text=rest[i:i + 4096],
                    reply_to_message_id=message.id
                )
                await asyncio.sleep(0.5)
            except Exception as e:
                logging.warning(f"overflow message failed: {e}")
                break

    try:
        await processing_msg.delete()
    except:
        pass

    cancel_flags.pop(cancel_key, None)
    if os.path.exists(job_dir):
        await asyncio.to_thread(shutil.rmtree, job_dir, ignore_errors=True)

# ================== YOUTUBE COMMUNITY POST HANDLER ==================
async def run_youtube_community_handler(client: Client, message: Message, processing_msg: Message, url: str):
    cancel_key = f"{message.chat.id}:{processing_msg.id}"
    cancel_flags[cancel_key] = False
    cancel_markup = get_cancel_markup(processing_msg.id, message.from_user.id)
    try:
        await processing_msg.edit_text("📌 Fetching community post...", reply_markup=cancel_markup)
    except:
        pass

    job_dir = os.path.join(BASE_DIR, "downloads", f"ytcomm_{os.urandom(8).hex()}")
    os.makedirs(job_dir, exist_ok=True)

    try:
        post_data = await asyncio.to_thread(fetch_youtube_community_data, url)

        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return

        if not post_data:
            return await processing_msg.edit_text("❌ Could not parse community post.")

        text = post_data.get('text', '')
        image_urls = post_data.get('images', [])
        video_urls = post_data.get('videos', [])

        if not image_urls and not video_urls:
            # Text-only post
            if text:
                await message.reply_text(f"📌 **Community Post:**\n\n{text}", reply_to_message_id=message.id)
                try:
                    await processing_msg.delete()
                except:
                    pass
            else:
                await processing_msg.edit_text("❌ No content found in community post.")
            return

        try:
            await processing_msg.edit_text(f"⬇️ Downloading {len(image_urls) + len(video_urls)} item(s)...", reply_markup=cancel_markup)
        except:
            pass

        downloaded_files = []

        def download_file(media_url, path):
            headers = {"User-Agent": USER_AGENT}
            res = requests.get(media_url, headers=headers, stream=True, timeout=30)
            res.raise_for_status()
            with open(path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8192):
                    f.write(chunk)

        for idx, img_url in enumerate(image_urls):
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            ext = ".jpg"
            file_path = os.path.join(job_dir, f"img_{idx}{ext}")
            await asyncio.to_thread(download_file, img_url, file_path)
            if os.path.exists(file_path):
                file_path = convert_to_jpg(file_path)
                downloaded_files.append(file_path)

        for idx, vid_url in enumerate(video_urls):
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            file_path = os.path.join(job_dir, f"vid_{idx}.mp4")
            await asyncio.to_thread(download_file, vid_url, file_path)
            if os.path.exists(file_path):
                downloaded_files.append(file_path)

        if not downloaded_files:
            return await processing_msg.edit_text("❌ Failed to download media.")

        try:
            await processing_msg.edit_text("📤 Uploading to Telegram...", reply_markup=cancel_markup)
        except:
            pass

        caption = text.strip() if text else ""

        images = [f for f in downloaded_files if f.endswith(('.jpg', '.jpeg', '.png', '.webp'))]
        videos = [f for f in downloaded_files if f.endswith(('.mp4', '.webm', '.mov'))]

        if images:
            if len(images) == 1:
                await client.send_photo(chat_id=message.chat.id, photo=images[0], caption=caption, reply_to_message_id=message.id)
            else:
                for i in range(0, len(images), 10):
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    media_group = [InputMediaPhoto(img) for img in images[i:i+10]]
                    if i == 0 and caption:
                        media_group[0] = InputMediaPhoto(images[0], caption=caption)
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1)

        if videos:
            for vid in videos:
                if is_cancelled(cancel_key):
                    try:
                        await processing_msg.edit_text("❌ Cancelled.")
                    except:
                        pass
                    return
                await client.send_video(chat_id=message.chat.id, video=vid, caption=caption, reply_to_message_id=message.id)

        try:
            await processing_msg.delete()
        except:
            pass

    except Exception as e:
        logging.error(f"YouTube community handler error: {e}")
        try:
            await processing_msg.edit_text(f"❌ Failed to process community post.\n`{str(e)[:400]}`")
        except:
            pass
    finally:
        cancel_flags.pop(cancel_key, None)
        if os.path.exists(job_dir):
            await asyncio.to_thread(shutil.rmtree, job_dir, ignore_errors=True)


def fetch_youtube_community_data(url: str) -> dict | None:
    """Scrape YouTube community post page to extract text, images, and videos."""
    try:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        html = resp.text

        # Extract ytInitialData JSON
        match = re.search(r'var\s+ytInitialData\s*=\s*(\{.*?\});', html, re.DOTALL)
        if not match:
            # Try alternative pattern
            match = re.search(r'ytInitialData\s*=\s*(\{.*?\});', html, re.DOTALL)
        if not match:
            return None

        data = json.loads(match.group(1))

        # Navigate to the community post content
        contents = (data
            .get('contents', {})
            .get('twoColumnBrowseResultsRenderer', {})
            .get('tabs', []))

        post_content = None
        for tab in contents:
            tab_content = tab.get('tabRenderer', {}).get('content', {})
            section_list = tab_content.get('sectionListRenderer', {}).get('contents', [])
            for section in section_list:
                items = section.get('itemSectionRenderer', {}).get('contents', [])
                for item in items:
                    post = item.get('continuationItemRenderer', {})
                    # Also check for the post directly
                    rich_item = section.get('itemSectionRenderer', {}).get('contents', [])
                    for ri in rich_item:
                        post_renderer = ri.get('backstagePostThreadRenderer', {}).get('post', {})
                        if post_renderer:
                            post_content = post_renderer
                            break

        # Alternative: try to find the post via engagementPanel
        if not post_content:
            panels = data.get('engagementPanels', [])
            for panel in panels:
                renderer = panel.get('engagementPanelSectionListRenderer', {})
                content = renderer.get('content', {})
                section = content.get('sectionListRenderer', {}).get('contents', [])
                for sec in section:
                    items = sec.get('itemSectionRenderer', {}).get('contents', [])
                    for item in items:
                        post_renderer = item.get('backstagePostThreadRenderer', {}).get('post', {})
                        if post_renderer:
                            post_content = post_renderer
                            break

        if not post_content:
            return None

        post = post_content.get('backstagePostRenderer', {})
        if not post:
            return None

        # Extract text
        text_content = post.get('contentText', {})
        text_runs = text_content.get('runs', [])
        text = ''.join(run.get('text', '') for run in text_runs)

        # Extract images
        image_urls = []
        images = post.get('backstageAttachment', {}).get('postMultiImageRenderer', {}).get('images', [])
        if not images:
            # Single image
            single_image = post.get('backstageAttachment', {}).get('imageRenderer', {})
            if single_image:
                images = [single_image]

        for img in images:
            img_renderer = img.get('imageRenderer', {})
            if img_renderer:
                # Get highest resolution URL
                thumbnails = img_renderer.get('thumbnails', [])
                if thumbnails:
                    best_url = max(thumbnails, key=lambda x: x.get('width', 0) * x.get('height', 0)).get('url', '')
                    if best_url:
                        if best_url.startswith('//'):
                            best_url = 'https:' + best_url
                        image_urls.append(best_url)

        # Extract videos
        video_urls = []
        video_renderer = post.get('backstageAttachment', {}).get('videoRenderer', {})
        if video_renderer:
            video_id = video_renderer.get('videoId', '')
            if video_id:
                video_url = f'https://www.youtube.com/watch?v={video_id}'
                video_urls.append(video_url)

        return {'text': text, 'images': image_urls, 'videos': video_urls}

    except Exception as e:
        logging.error(f"YouTube community post scraping error: {e}")
        return None

# ================== MAIN LINK HANDLER (COMBINED LOGIC) ==================
@app.on_message(filters.text & (filters.group | filters.private))
async def handle_links(client: Client, message: Message):
    links = LINK_REGEX.findall(message.text)
    if not links:
        return
    url = links[0]
    if "reddit.com" in url or "redd.it" in url:
        url = clean_reddit_url(url)

    # --- INSTAGRAM: use API directly (bot.py way) ---
    if is_instagram_url(url):
        processing_msg = await message.reply_text("🔗 Analyzing link...", reply_to_message_id=message.id)
        try:
            await run_clipssaver_fallback(client, message, processing_msg, url)
        except Exception as e:
            logging.error(f"Instagram handler error: {e}")
            await processing_msg.edit_text("❌ Failed to process Instagram link.")
        return

    # --- TWITTER/X: use yt-dlp with tweet text as caption ---
    if is_twitter_url(url):
        processing_msg = await message.reply_text("🐦 Analyzing tweet...", reply_to_message_id=message.id)
        try:
            await run_twitter_handler(client, message, processing_msg, url)
        except Exception as e:
            logging.error(f"Twitter handler error: {e}")
            await processing_msg.edit_text("❌ Failed to process tweet.")
        return

    # --- YOUTUBE COMMUNITY POSTS ---
    if is_youtube_community_url(url):
        processing_msg = await message.reply_text("📌 Analyzing community post...", reply_to_message_id=message.id)
        try:
            await run_youtube_community_handler(client, message, processing_msg, url)
        except Exception as e:
            logging.error(f"YouTube community handler error: {e}")
            await processing_msg.edit_text("❌ Failed to process community post.")
        return

    # --- EVERYTHING ELSE: yt-dlp (main_bot.py way) -> TikTok, YouTube, etc ---
    # Clean TikTok photo URLs: strip query params that break extraction
    clean_url = url
    # For TikTok, gallery-dl prefers base URL without tracking params
    if "tiktok.com" in url:
        clean_url = url.split("?")[0]

    processing_msg = await message.reply_text("🔗 Analyzing link...", reply_to_message_id=message.id)

    try:
        info = await asyncio.to_thread(get_video_info, clean_url)

        if not info:
            # yt-dlp failed (Unsupported URL / photo posts etc) -> try gallery-dl
            logging.warning(f"yt-dlp returned no info for {clean_url}, trying gallery-dl fallback")
            # Special hint for TikTok photo
            if "tiktok.com" in clean_url and "/photo/" in clean_url:
                await processing_msg.edit_text("📸 TikTok photo detected, trying gallery-dl...")
            await run_gallery_dl_fallback(client, message, processing_msg, clean_url)
            return

        entries = info.get('entries', [])
        if entries or info.get('_type') == 'playlist':
            await handle_carousel(client, message, processing_msg, entries or [info], clean_url)
            return

        formats = [(f.get('height'), f.get('format_id')) for f in info.get('formats', []) 
                   if f.get('vcodec') != 'none' and f.get('height')]
        formats = sorted(list(set(formats)), key=lambda x: x[0], reverse=True)[:8] or [("Best", "best")]

        storage_key = f"{message.chat.id}:{processing_msg.id}"
        user_data[storage_key] = {
            'url': clean_url,
            'orig_msg_id': message.id,
            'chat_id': message.chat.id,
            'processing': False
        }

        buttons = [[InlineKeyboardButton(
            text=f"{h}p" if isinstance(h, int) else str(h),
            callback_data=f"q:{fid}:{processing_msg.id}"
        )] for h, fid in formats]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cancel:{processing_msg.id}")])

        await processing_msg.edit_text("🎬 Choose quality:", reply_markup=InlineKeyboardMarkup(buttons))

    except Exception as e:
        logging.error(f"Link handler error: {e}")
        # Last chance: gallery-dl for any yt-dlp exception
        try:
            await run_gallery_dl_fallback(client, message, processing_msg, clean_url)
        except Exception as fallback_e:
            logging.error(f"Fallback also failed: {fallback_e}")
            await processing_msg.edit_text("❌ Failed to process link.")

async def handle_carousel(client, message, processing_msg, entries, original_url):
    cancel_key = f"{message.chat.id}:{processing_msg.id}"
    cancel_flags[cancel_key] = False
    cancel_markup = get_cancel_markup(processing_msg.id, message.from_user.id)
    total = len(entries)
    try:
        await processing_msg.edit_text(f"📑 Found {total} items...", reply_markup=cancel_markup)
    except:
        pass
    
    downloaded = []
    for idx, entry in enumerate(entries, 1):
        if not entry: continue
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            # cleanup already downloaded files
            for p, _ in downloaded:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass
            cancel_flags.pop(cancel_key, None)
            return
        try:
            await processing_msg.edit_text(f"⬇️ Downloading {idx}/{total}...", reply_markup=cancel_markup)
        except:
            pass
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            for p, _ in downloaded:
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except:
                        pass
            cancel_flags.pop(cancel_key, None)
            return
        is_video = any(f.get('vcodec') != 'none' for f in entry.get('formats', [])) or entry.get('ext') == 'mp4'
        ext = '.mp4' if is_video else '.jpg'
        output = os.path.join(BASE_DIR, "downloads", f"car_{idx}_{os.urandom(4).hex()}{ext}")
        os.makedirs(os.path.dirname(output), exist_ok=True)

        try:
            ydl_opts = get_ydl_opts({
                'outtmpl': output,
                'quiet': True,
                'noplaylist': True,
                'format': 'bestvideo+bestaudio/best' if is_video else 'best',
            })
            entry_url = entry.get('webpage_url') or original_url
            await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(ydl_opts).download([entry_url]))
            if is_cancelled(cancel_key):
                if os.path.exists(output):
                    try:
                        os.remove(output)
                    except:
                        pass
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                for p, _ in downloaded:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except:
                            pass
                cancel_flags.pop(cancel_key, None)
                return
            if os.path.exists(output):
                # For images, ensure jpg for better Telegram compatibility
                if not is_video:
                    output = convert_to_jpg(output)
                downloaded.append((output, is_video))
        except Exception as e:
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                for p, _ in downloaded:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except:
                            pass
                cancel_flags.pop(cancel_key, None)
                return
            logging.error(f"Carousel item failed: {e}")

    try:
        if is_cancelled(cancel_key):
            try:
                await processing_msg.edit_text("❌ Cancelled.")
            except:
                pass
            return
        if downloaded:
            try:
                await processing_msg.edit_text(f"📤 Uploading {len(downloaded)} items...", reply_markup=cancel_markup)
            except:
                pass
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            images = [p for p, v in downloaded if not v]
            videos = [p for p, v in downloaded if v]
            if images:
                if len(images) == 1:
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    await client.send_photo(message.chat.id, images[0], reply_to_message_id=message.id)
                else:
                    for i in range(0, len(images), 10):
                        if is_cancelled(cancel_key):
                            try:
                                await processing_msg.edit_text("❌ Cancelled.")
                            except:
                                pass
                            return
                        media_group = [InputMediaPhoto(img) for img in images[i:i+10]]
                        await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                        await asyncio.sleep(1)
            
            if videos:
                if len(videos) == 1:
                    if is_cancelled(cancel_key):
                        try:
                            await processing_msg.edit_text("❌ Cancelled.")
                        except:
                            pass
                        return
                    await client.send_video(message.chat.id, videos[0], reply_to_message_id=message.id)
                else:
                    for i in range(0, len(videos), 10):
                        if is_cancelled(cancel_key):
                            try:
                                await processing_msg.edit_text("❌ Cancelled.")
                            except:
                                pass
                            return
                        media_group = [InputMediaVideo(vid) for vid in videos[i:i+10]]
                        await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                        await asyncio.sleep(1)

            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            try:
                await processing_msg.delete()
            except:
                pass
        else:
            if is_cancelled(cancel_key):
                try:
                    await processing_msg.edit_text("❌ Cancelled.")
                except:
                    pass
                return
            await processing_msg.edit_text("❌ Nothing downloaded.")
    finally:
        cancel_flags.pop(cancel_key, None)
        for p, _ in downloaded:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass

@app.on_callback_query(filters.regex(r"^cancel:"))
async def cancel_process_callback(client: Client, callback: CallbackQuery):
    try:
        _, proc_msg_id_str = callback.data.split(":")
        proc_msg_id = int(proc_msg_id_str)
    except:
        return await callback.answer("Invalid cancel data", show_alert=True)
    chat_id = callback.message.chat.id
    cancel_key = f"{chat_id}:{proc_msg_id}"
    state = user_data.get(cancel_key)
    # If pending quality selection and not yet processing -> just cancel selection
    if state and not state.get('processing'):
        user_data.pop(cancel_key, None)
        cancel_flags.pop(cancel_key, None)
        try:
            await callback.edit_message_text("❌ Cancelled.")
        except:
            try:
                await callback.message.delete()
            except:
                pass
        await callback.answer("Cancelled", show_alert=False)
        return
    # Mark active download/upload as cancelled
    cancel_flags[cancel_key] = True
    if state:
        state['processing'] = True
    try:
        await callback.edit_message_text("❌ Cancelled by user.")
    except:
        try:
            await callback.message.edit_text("❌ Cancelled by user.")
        except:
            pass
    await callback.answer("Cancelled", show_alert=False)

@app.on_callback_query(filters.regex(r"^q:"))
async def download_video_callback(client: Client, callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 3:
        return await callback.answer("Invalid request.", show_alert=True)
    
    proc_msg_id = parts[-1]
    format_id = ":".join(parts[1:-1])

    storage_key = f"{callback.message.chat.id}:{proc_msg_id}"
    state = user_data.get(storage_key)
    if not state or state.get('processing'):
        return await callback.edit_message_text("❌ Session expired or already processing.")

    user_data[storage_key]['processing'] = True
    cancel_key = storage_key
    cancel_flags[cancel_key] = False
    cancel_markup = get_cancel_markup(int(proc_msg_id), callback.from_user.id)

    try:
        await callback.edit_message_text("⬇️ Downloading...", reply_markup=cancel_markup)
    except:
        pass

    file_path = None
    try:
        file_path = await asyncio.to_thread(download_video, state['url'], format_id)
        if is_cancelled(cancel_key):
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            user_data.pop(storage_key, None)
            cancel_flags.pop(cancel_key, None)
            try:
                await callback.message.edit_text("❌ Cancelled.")
            except:
                pass
            return

        try:
            await callback.edit_message_text("📤 Uploading...", reply_markup=cancel_markup)
        except:
            pass

        if is_cancelled(cancel_key):
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            user_data.pop(storage_key, None)
            cancel_flags.pop(cancel_key, None)
            try:
                await callback.message.edit_text("❌ Cancelled.")
            except:
                pass
            return

        await client.send_video(
            chat_id=state['chat_id'],
            video=file_path,
            caption="✅ Downloaded!",
            reply_to_message_id=state['orig_msg_id']
        )
        if is_cancelled(cancel_key):
            # Upload finished but user cancelled after; cleanup message state but keep upload
            user_data.pop(storage_key, None)
            cancel_flags.pop(cancel_key, None)
            try:
                await callback.message.edit_text("❌ Cancelled.")
            except:
                pass
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            return

        try:
            await callback.message.delete()
        except:
            pass
        user_data.pop(storage_key, None)
        cancel_flags.pop(cancel_key, None)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass
    except Exception as e:
        logging.error(e)
        if is_cancelled(cancel_key):
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except:
                    pass
            user_data.pop(storage_key, None)
            cancel_flags.pop(cancel_key, None)
            try:
                await callback.message.edit_text("❌ Cancelled.")
            except:
                pass
            return
        try:
            await callback.edit_message_text("❌ Download failed.")
        except:
            try:
                await callback.message.edit_text("❌ Download failed.")
            except:
                pass
        user_data.pop(storage_key, None)
        cancel_flags.pop(cancel_key, None)
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except:
                pass

if __name__ == "__main__":
    # Log which cookiefile will be used at startup
    cf = get_cookiefile()
    if cf:
        print(f"🍪 Using cookies: {cf}")
    else:
        print("⚠️ No cookies file found (cookies.txt / cookies.json). YouTube may fail for age-restricted videos.")
    print("🤖 Combined Bot running! Instagram=API, Others=yt-dlp")
    app.run()
