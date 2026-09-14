import asyncio
import logging
import re
import os
import requests
import glob
import shutil
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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")
SESSION_PATH = os.path.join(BASE_DIR, "bot_session")

app = Client(
    name=SESSION_PATH,
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

user_data = {}
LINK_REGEX = re.compile(r'https?://\S+')
logging.basicConfig(level=logging.INFO)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"

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
    return 'instagram.com' in url

def get_ydl_opts(extra_opts=None):
    ydl_opts = {
        'quiet': True,
        'noplaylist': False,
        'extract_flat': False,
        'ignoreerrors': True,
        'extractor_args': {'youtube': ['player_client=ios,mweb']},
        'http_headers': {'User-Agent': USER_AGENT}
    }
    if os.path.exists(COOKIES_PATH):
        ydl_opts['cookiefile'] = COOKIES_PATH
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

# ================== CLIPSSAVER API FALLBACK ==================
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

    response = requests.post(api_url, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()

async def run_clipssaver_fallback(client: Client, message: Message, processing_msg: Message, url: str):
    await processing_msg.edit_text("⚡ Instagram detected. Processing media...")

    job_dir = os.path.join(BASE_DIR, "downloads", f"cs_local_{os.urandom(8).hex()}")
    os.makedirs(job_dir, exist_ok=True)

    try:
        data = await asyncio.to_thread(fetch_clipssaver_data, url)
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
            return await processing_msg.edit_text("❌ Failed to extract photo/video from Instagram post.")

        await processing_msg.edit_text(f"⬇️ Downloading {len(media_list)} item(s)...")

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
            ext = ".mp4" if is_video else ".jpg"
            file_path = os.path.join(job_dir, f"media_{idx}{ext}")
            await asyncio.to_thread(download_file, m_url, file_path)

            if os.path.exists(file_path):
                if not is_video:
                    file_path = convert_to_jpg(file_path)
                downloaded_files.append((file_path, is_video))

        if not downloaded_files:
            return await processing_msg.edit_text("❌ Failed to download media.")

        await processing_msg.edit_text("📤 Uploading to Telegram...")

        images = [p for p, is_vid in downloaded_files if not is_vid]
        videos = [p for p, is_vid in downloaded_files if is_vid]

        if images:
            if len(images) == 1:
                await client.send_photo(chat_id=message.chat.id, photo=images[0], reply_to_message_id=message.id)
            else:
                for i in range(0, len(images), 10):
                    media_group = [InputMediaPhoto(img) for img in images[i:i+10]]
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1)

        if videos:
            if len(videos) == 1:
                await client.send_video(chat_id=message.chat.id, video=videos[0], reply_to_message_id=message.id)
            else:
                for i in range(0, len(videos), 10):
                    media_group = [InputMediaVideo(vid) for vid in videos[i:i+10]]
                    await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                    await asyncio.sleep(1)

        await processing_msg.delete()

    except Exception as e:
        logging.error(f"ClipsSaver API error: {e}")
        await processing_msg.edit_text("❌ Failed to process Instagram post.")
    finally:
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

# ================== MAIN LINK HANDLER ==================
@app.on_message(filters.text & (filters.group | filters.private))
async def handle_links(client: Client, message: Message):
    links = LINK_REGEX.findall(message.text)
    if not links:
        return
    url = links[0]
    if "reddit.com" in url or "redd.it" in url:
        url = clean_reddit_url(url)

    processing_msg = await message.reply_text("🔗 Analyzing link...", reply_to_message_id=message.id)

    try:
        info = await asyncio.to_thread(get_video_info, url) if not is_instagram_url(url) else None

        if is_instagram_url(url) and (not info or info.get('entries')):
            await run_clipssaver_fallback(client, message, processing_msg, url)
            return

        if not info:
            return await processing_msg.edit_text("❌ Could not extract info.")

        entries = info.get('entries', [])
        if entries or info.get('_type') == 'playlist':
            await handle_carousel(client, message, processing_msg, entries or [info], url)
            return

        formats = [(f.get('height'), f.get('format_id')) for f in info.get('formats', []) 
                   if f.get('vcodec') != 'none' and f.get('height')]
        formats = sorted(list(set(formats)), key=lambda x: x[0], reverse=True)[:8] or [("Best", "best")]

        storage_key = f"{message.chat.id}:{processing_msg.id}"
        user_data[storage_key] = {
            'url': url,
            'orig_msg_id': message.id,
            'chat_id': message.chat.id,
            'timer_started': False
        }

        buttons = [[InlineKeyboardButton(
            text=f"{h}p" if isinstance(h, int) else str(h),
            callback_data=f"q:{fid}:{processing_msg.id}:{message.from_user.id}"
        )] for h, fid in formats]

        await processing_msg.edit_text("🎬 Choose quality (auto in 5s):", reply_markup=InlineKeyboardMarkup(buttons))
        asyncio.create_task(auto_select_quality(client, processing_msg, storage_key, formats, message.from_user.id))

    except Exception as e:
        logging.error(f"Link handler error: {e}")
        await processing_msg.edit_text("❌ Failed to process link.")

async def handle_carousel(client, message, processing_msg, entries, original_url):
    total = len(entries)
    await processing_msg.edit_text(f"📑 Found {total} items...")
    
    downloaded = []
    for idx, entry in enumerate(entries, 1):
        if not entry: continue
        await processing_msg.edit_text(f"⬇️ Downloading {idx}/{total}...")
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
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([entry_url])
            if os.path.exists(output):
                downloaded.append((output, is_video))
        except Exception as e:
            logging.error(f"Carousel item failed: {e}")

    try:
        if downloaded:
            images = [p for p, v in downloaded if not v]
            videos = [p for p, v in downloaded if v]
            if images:
                if len(images) == 1:
                    await client.send_photo(message.chat.id, images[0], reply_to_message_id=message.id)
                else:
                    for i in range(0, len(images), 10):
                        media_group = [InputMediaPhoto(img) for img in images[i:i+10]]
                        await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                        await asyncio.sleep(1)
            
            if videos:
                if len(videos) == 1:
                    await client.send_video(message.chat.id, videos[0], reply_to_message_id=message.id)
                else:
                    for i in range(0, len(videos), 10):
                        media_group = [InputMediaVideo(vid) for vid in videos[i:i+10]]
                        await client.send_media_group(chat_id=message.chat.id, media=media_group, reply_to_message_id=message.id)
                        await asyncio.sleep(1)

            await processing_msg.delete()
        else:
            if is_instagram_url(original_url):
                await run_clipssaver_fallback(client, message, processing_msg, original_url)
            else:
                await processing_msg.edit_text("❌ Nothing downloaded.")
    finally:
        for p, _ in downloaded:
            if os.path.exists(p):
                os.remove(p)

async def auto_select_quality(client, processing_msg, storage_key, formats, user_id):
    await asyncio.sleep(5)
    state = user_data.get(storage_key)
    if not state or state.get('timer_started'):
        return
    user_data[storage_key]['timer_started'] = True
    highest = formats[0]
    label = f"{highest[0]}p" if isinstance(highest[0], int) else "Best"
    try:
        await processing_msg.edit_text(f"⏰ Auto-selecting {label}...")
        file_path = await asyncio.to_thread(download_video, state['url'], highest[1])
        await processing_msg.edit_text("📤 Uploading...")
        await client.send_video(
            chat_id=state['chat_id'],
            video=file_path,
            caption=f"✅ {label}",
            reply_to_message_id=state['orig_msg_id']
        )
        await processing_msg.delete()
        user_data.pop(storage_key, None)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logging.error(e)
        await processing_msg.edit_text("❌ Auto-download failed.")

@app.on_callback_query(filters.regex(r"^q:"))
async def download_video_callback(client: Client, callback: CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) < 4:
        return await callback.answer("Invalid request.", show_alert=True)
    
    user_id_str = parts[-1]
    proc_msg_id = parts[-2]
    format_id = ":".join(parts[1:-2])

    if callback.from_user.id != int(user_id_str):
        return await callback.answer("Not for you!", show_alert=True)

    storage_key = f"{callback.message.chat.id}:{proc_msg_id}"
    state = user_data.get(storage_key)
    if not state or state.get('timer_started'):
        return await callback.edit_message_text("❌ Session expired or processing.")

    user_data[storage_key]['timer_started'] = True
    await callback.edit_message_text("⬇️ Downloading...")

    try:
        file_path = await asyncio.to_thread(download_video, state['url'], format_id)
        await callback.edit_message_text("📤 Uploading...")
        await client.send_video(
            chat_id=state['chat_id'],
            video=file_path,
            caption="✅ Downloaded!",
            reply_to_message_id=state['orig_msg_id']
        )
        await callback.message.delete()
        user_data.pop(storage_key, None)
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception as e:
        logging.error(e)
        await callback.edit_message_text("❌ Download failed.")

if __name__ == "__main__":
    print("🤖 Bot running on Railway!")
    app.run()
