import os, json, time, asyncio, csv, shutil, threading, logging
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from flask import Flask, jsonify
import aiohttp

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("IPTV_BOT")

# --- ENVIRONMENT VARIABLES ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9247296671:AAEWJ2mWUvAnZuQiGrYQaotOl0uVqI3Ievk")
API_ID = int(os.getenv("API_ID", "25800893")) 
API_HASH = os.getenv("API_HASH", "6ccc441e2cbd1ef33ee523fd2349758b")

ADMIN = int(os.getenv("PERMANENT_ADMIN", "6403142441"))
DUMP_ENV = os.getenv("DUMP_CHAT_ID", "-1002627919828")
DUMP = int(DUMP_ENV) if DUMP_ENV else None
DEFAULT_URL = os.getenv("PLAYLIST_URL", "https://iptv-org.github.io/iptv/index.m3u")

# Initialize Kurigram Client
app = Client("iptv_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
# Initialize Flask app
flask_app = Flask(__name__)

def load(f, d):
    try:
        with open(f) as x: 
            return json.load(x)
    except Exception as e:
        logger.warning(f"Failed to load {f}, using default: {e}")
        return d

def save(f, d):
    try:
        with open(f, "w") as x: 
            json.dump(d, x)
    except Exception as e:
        logger.error(f"Failed to save {f}: {e}")

# --- GLOBAL STATES ---
lock = asyncio.Lock()
urls = load("playlists.json", [DEFAULT_URL])
users = set(load("approved_users.json", []))
channels = []
jobs = {}

# --- FLASK WEB SERVER (Synchronous) ---
@flask_app.route("/")
@flask_app.route("/health")
def health():
    return jsonify({
        "status": "running", 
        "channels": len(channels), 
        "active_jobs": len(jobs)
    })

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- ACCESS CONTROL & PERMISSION CHECK ---
def is_admin(user_id):
    return user_id == ADMIN

def is_user_allowed(user_id):
    # If ADMIN is default or user is in approved list or user is ADMIN
    return user_id == ADMIN or user_id in users

async def check_access(m: Message) -> bool:
    """Checks user access; if unauthorized, sends a polite warning message."""
    if not m.from_user:
        await m.reply("⚠️ Cannot identify user.")
        return False
    
    uid = m.from_user.id
    if is_user_allowed(uid):
        return True
    
    await m.reply(
        f"⚠️ **Access Denied!**\n\n"
        f"Aap authorized nahi hain.\n"
        f"Apki User ID: `{uid}`\n\n"
        f"Kripya Admin se approval lein (/approve `{uid}`)."
    )
    return False

# --- ASYNC M3U FETCHING ---
async def fetch(url):
    logger.info(f"Fetching playlist from {url}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                r.raise_for_status()
                text = await r.text()
    except asyncio.TimeoutError:
        raise ValueError(f"Connection timeout fetching playlist from: {url}")
    except Exception as e:
        raise ValueError(f"Failed to fetch {url}: {str(e)}")
            
    if "#EXTM3U" not in text[:500]: 
        raise ValueError("Invalid M3U: Missing #EXTM3U header")
    
    result, name = [], "Unknown"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            name = line.split(",", 1)[-1].strip()
        elif line and not line.startswith("#") and line.startswith(("http://", "https://")):
            result.append({"name": name, "url": line})
            name = "Unknown"
            
    if not result: 
        raise ValueError("No channels found in playlist")
    return result

async def reload_all():
    global channels
    data, errors = [], []
    
    async with lock:
        current_urls = list(urls)
        
    for url in current_urls:
        try: 
            data.extend(await fetch(url))
        except Exception as e: 
            logger.error(f"Error fetching {url}: {e}")
            errors.append(f"{url}: {str(e)}")
            
    if data:
        async with lock:
            channels = [{"id": i, **c} for i, c in enumerate(data, 1)]
    
    logger.info(f"Loaded {len(channels)} total channels across {len(current_urls)} playlists")
    return len(channels), errors

# --- ASYNC VIDEO PROCESSING & UPLOAD ---
async def upload(chat, file, title):
    try:
        thumb = file + ".jpg"
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", "00:00:03", "-i", file,
            "-vframes", "1", "-vf", "scale=320:-1", thumb,
            stdout=asyncio.subprocess.DEVNULL, 
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.communicate()
        
        caption = f"🎬 **{title}**"[:1024]
        if os.path.exists(thumb):
            await app.send_video(
                chat, video=file, caption=caption,
                thumb=thumb, supports_streaming=True
            )
            os.remove(thumb)
        else:
            await app.send_video(
                chat, video=file, caption=caption,
                supports_streaming=True
            )
        return True
    except Exception as e:
        logger.error(f"Upload Error: {e}")
        return False

async def worker(key, chat, url, mins, title):
    folder = f"recordings/{key}"
    os.makedirs(folder, exist_ok=True)
    listing = os.path.join(folder, "list.csv")
    pattern = os.path.join(folder, "part_%04d.mp4")
    
    cmd = ["ffmpeg", "-y", "-reconnect", "1", "-reconnect_streamed", "1",
           "-reconnect_delay_max", "10", "-i", url, "-t", str(mins * 60),
           "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "23", "-c:a", "aac",
           "-f", "segment", "-segment_time", "180", "-reset_timestamps", "1",
           "-segment_list", listing, "-segment_list_type", "csv", pattern]
           
    p = None
    done = set()
    
    async def process_segments():
        if not os.path.exists(listing): return
        with open(listing, "r") as f:
            for row in csv.reader(f):
                if not row: continue
                segment_file = row[0]
                if segment_file not in done and os.path.exists(segment_file):
                    done.add(segment_file)
                    a = await upload(chat, segment_file, title)
                    b = await upload(DUMP, segment_file, title) if DUMP else True
                    if a and b: 
                        try:
                            os.remove(segment_file)
                        except OSError as oe:
                            logger.warning(f"Could not remove segment {segment_file}: {oe}")

    try:
        await app.send_message(chat, f"🔴 **Recording Started:**\n📌 Title: **{title}**\n⏱ Duration: `{mins} mins`")
        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL, 
            stderr=asyncio.subprocess.PIPE
        )
        
        async with lock:
            if key in jobs:
                jobs[key]["process"] = p
            
        while p.returncode is None:
            async with lock:
                if jobs.get(key, {}).get("stop"):
                    p.terminate()
                    break
            
            await process_segments()
            await asyncio.sleep(3)
            
        if p.returncode is None: 
            await p.wait()
            
        await process_segments() 
        await app.send_message(chat, f"✅ **Recording Finished:** {title}")
        
    except Exception as e:
        logger.error(f"Recording worker exception: {e}")
        await app.send_message(chat, f"❌ **Recording Error:**\n`{str(e)[:700]}`")
    finally:
        if p and p.returncode is None: 
            try: p.terminate()
            except ProcessLookupError: pass
            
        async with lock:
            jobs.pop(key, None)
            
        if os.path.exists(folder):
            shutil.rmtree(folder, ignore_errors=True)

async def record(chat, url, mins, title):
    if not url.startswith(("http://", "https://")):
        return await app.send_message(chat, "❌ **Error:** URL must begin with `http://` or `https://`.")
    if not (1 <= mins <= 1440):
        return await app.send_message(chat, "❌ **Error:** Duration must be between 1 and 1440 minutes (24 hours).")
        
    async with lock:
        if any(j["chat"] == chat for j in jobs.values()):
            return await app.send_message(chat, "⚠️ **Warning:** A recording is already active in this chat. Stop it first using /stop.")
            
        key = f"{chat}_{int(time.time())}"
        jobs[key] = {"chat": chat, "stop": False, "start": time.time(), "title": title}
        
    asyncio.create_task(worker(key, chat, url, mins, title))

# --- KURIGRAM BOT COMMANDS ---
@app.on_message(filters.command(["start", "help"]))
async def help_cmd(client: Client, m: Message):
    uid = m.from_user.id if m.from_user else "Unknown"
    is_user_adm = is_admin(uid)
    status_text = "🟢 Admin" if is_user_adm else ("🟢 Approved User" if is_user_allowed(uid) else "🔴 Unauthorized")

    text = (
        f"👋 **Welcome to IPTV Recording Bot!**\n\n"
        f"👤 **Your ID:** `{uid}`\n"
        f"🔐 **Access Status:** {status_text}\n\n"
        f"📋 **Available Commands:**\n"
        f"• `/playlist` - List all available channels\n"
        f"• `/p <name>` - Search channel by name\n"
        f"• `/rec <id> <mins>` - Record channel by ID\n"
        f"• `/record <url> <mins>` - Direct stream recording\n"
        f"• `/status` - Check active recording status\n"
        f"• `/stop` - Stop ongoing recording\n"
    )
    if is_user_adm:
        text += (
            f"\n🛠 **Admin Commands:**\n"
            f"• `/addplaylist <url>` - Add new M3U playlist\n"
            f"• `/reloadplaylist` - Reload all playlists\n"
            f"• `/approve <id>` - Authorize a user\n"
            f"• `/revoke <id>` - Remove authorization\n"
            f"• `/users` - List approved users\n"
            f"• `/stopall` - Stop all active recordings\n"
        )
    await m.reply(text)

@app.on_message(filters.command("addplaylist"))
async def add(client: Client, m: Message):
    if not is_admin(m.from_user.id if m.from_user else 0):
        return await m.reply("❌ **Permission Denied:** This command is only for Admin.")
    if len(m.command) < 2: 
        return await m.reply("⚠️ **Usage:** `/addplaylist <M3U_URL>`")
    try:
        url = m.command[1]
        msg = await m.reply("⏳ Fetching and verifying playlist...")
        new = await fetch(url)
        async with lock:
            if url not in urls:
                urls.append(url)
                save("playlists.json", urls)
                
        n, err = await reload_all()
        await msg.edit_text(f"✅ **Playlist Added!**\nChannels in URL: `{len(new)}`\nTotal Channels: `{n}`")
        if err: 
            await m.reply("⚠️ **Warnings:**\n" + "\n".join(err))
    except Exception as e: 
        logger.error(f"Addplaylist error: {e}")
        await m.reply(f"❌ **Error adding playlist:**\n`{str(e)}`")

@app.on_message(filters.command("reloadplaylist"))
async def reload_cmd(client: Client, m: Message):
    if not is_admin(m.from_user.id if m.from_user else 0):
        return await m.reply("❌ **Permission Denied:** This command is only for Admin.")
    msg = await m.reply("🔄 **Reloading all playlists... Please wait.**")
    n, err = await reload_all()
    await msg.edit_text(f"✅ **Reload Complete!**\nTotal Channels: `{n}`")
    if err: 
        await m.reply("⚠️ **Errors during reload:**\n" + "\n".join(err))

@app.on_message(filters.command(["playlist", "p", "find"]))
async def search(client: Client, m: Message):
    if not await check_access(m): return
    
    key = " ".join(m.command[1:]).lower() if len(m.command) > 1 else ""
    if m.command[0] in ["p", "find"] and not key:
        return await m.reply("⚠️ **Usage:** `/p <Channel Name>`\nExample: `/p sports`")
        
    async with lock:
        result = [c for c in channels if key in c["name"].lower()]
        
    if not result: 
        return await m.reply(f"❌ **No channels found** matching `{key or 'all'}`.")
        
    text = "\n".join(f"🆔 `{c['id']}` — {c['name']}" for c in result[:100])
    extra = f"\n\n*(Showing top {min(len(result), 100)} of {len(result)} channels)*" if len(result) > 100 else ""
    
    for i in range(0, len(text), 3500):
        chunk = text[i:i+3500]
        if i == 0: 
            chunk += extra + "\n\n💡 Use `/rec <ID> <MINUTES>` to record."
        await m.reply(chunk)

@app.on_message(filters.command("rec"))
async def rec(client: Client, m: Message):
    if not await check_access(m): return
    
    if len(m.command) < 3: 
        return await m.reply("⚠️ **Usage:** `/rec <Channel_ID> <Minutes> [Optional Title]`\nExample: `/rec 12 5 Cricket Match`")
    try:
        channel_id = int(m.command[1])
        mins = int(m.command[2])
    except ValueError:
        return await m.reply("❌ **Error:** Channel ID and Minutes must be numbers.")
        
    async with lock:
        c = next((x for x in channels if x["id"] == channel_id), None)
        
    if not c:
        return await m.reply(f"❌ **Error:** Channel ID `{channel_id}` not found. Check `/playlist`.")
        
    title = " ".join(m.command[3:]) if len(m.command) > 3 else c["name"]
    await record(m.chat.id, c["url"], mins, title)

@app.on_message(filters.command("record"))
async def direct(client: Client, m: Message):
    if not await check_access(m): return
    
    if len(m.command) < 3: 
        return await m.reply("⚠️ **Usage:** `/record <Stream_URL> <Minutes> [Optional Title]`\nExample: `/record https://example.com/live.m3u8 10`")
    try:
        url = m.command[1]
        mins = int(m.command[2])
    except ValueError: 
        return await m.reply("❌ **Error:** Minutes must be a valid number.")
        
    title = " ".join(m.command[3:]) if len(m.command) > 3 else "Direct Stream"
    await record(m.chat.id, url, mins, title)

@app.on_message(filters.command("status"))
async def status(client: Client, m: Message):
    if not await check_access(m): return
    
    async with lock:
        if not jobs: 
            return await m.reply("ℹ️ **No active recordings running right now.**")
        lines = []
        for j in jobs.values():
            elapsed = int(time.time() - j["start"])
            lines.append(f"🎥 **{j['title']}**\n⏱ Elapsed: `{elapsed}s` | Chat: `{j['chat']}`")
        text = "📊 **Active Recordings:**\n\n" + "\n\n".join(lines)
    await m.reply(text)

@app.on_message(filters.command(["stop", "stopall"]))
async def stop(client: Client, m: Message):
    if not await check_access(m): return
    
    is_stopall = m.command[0] == "stopall"
    if is_stopall and not is_admin(m.from_user.id if m.from_user else 0): 
        return await m.reply("❌ **Permission Denied:** `/stopall` is only available to Admin.")
    
    n = 0
    caller_id = m.from_user.id if m.from_user else 0
    async with lock:
        for j in jobs.values():
            if is_admin(caller_id) or j["chat"] == m.chat.id:
                j["stop"] = True
                if j.get("process"): 
                    try: 
                        j["process"].terminate()
                    except ProcessLookupError: 
                        pass
                n += 1
                
    if n == 0:
        await m.reply("ℹ️ No matching running recording found to stop.")
    else:
        await m.reply(f"🛑 **Stop signal issued for {n} recording(s).**")

@app.on_message(filters.command(["approve", "revoke"]))
async def access(client: Client, m: Message):
    if not is_admin(m.from_user.id if m.from_user else 0):
        return await m.reply("❌ **Permission Denied:** This command is only for Admin.")
        
    if len(m.command) < 2: 
        return await m.reply(f"⚠️ **Usage:** `/{m.command[0]} <USER_ID>`")
    try:
        uid = int(m.command[1])
        action = m.command[0].lower()
        async with lock:
            if action == "approve": 
                users.add(uid)
                status_msg = f"✅ User `{uid}` has been **Approved**."
            else: 
                users.discard(uid)
                status_msg = f"🚫 User `{uid}` access has been **Revoked**."
            save("approved_users.json", list(users))
        await m.reply(status_msg)
    except ValueError: 
        await m.reply("❌ **Error:** USER_ID must be a numeric ID.")

@app.on_message(filters.command("users"))
async def users_cmd(client: Client, m: Message):
    if not is_admin(m.from_user.id if m.from_user else 0):
        return await m.reply("❌ **Permission Denied:** This command is only for Admin.")
        
    async with lock:
        text = "\n".join(f"• `{u}`" for u in sorted(users)) if users else "No approved users yet."
    await m.reply(f"👥 **Approved Users List:**\n{text}\n\n👑 **Admin ID:** `{ADMIN}`")

# Catch-all handler for unknown commands
@app.on_message(filters.command([]))
async def unknown_cmd(client: Client, m: Message):
    if m.command:
        await m.reply(f"⚠️ **Unknown command:** `/{m.command[0]}`\nSend `/help` or `/start` to see the list of valid commands.")

# --- BOT LAUNCHER ---
async def main():
    logger.info("Initializing IPTV Bot...")
    # 1. Fetch initial playlists asynchronously
    try:
        n, err = await reload_all()
        logger.info(f"Loaded {n} channels.")
    except Exception as e:
        logger.error(f"Error during initial reload: {e}")
        
    # 2. Start Kurigram
    await app.start()
    logger.info("Bot is polling via MTProto...")
    await idle()
    await app.stop()

if __name__ == "__main__":
    # Start Flask Web Server in a Background Thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Run the Kurigram async event loop in the Main Thread
    asyncio.run(main())
