import os, json, time, asyncio, csv, shutil, threading
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from flask import Flask, jsonify
import aiohttp

# --- ENVIRONMENT VARIABLES ---
BOT_TOKEN = os.getenv("BOT_TOKEN", "9231852175:AAEmC4uuHjhxiSts_gyBUgsSoQWBHEr4e4c")
API_ID = int(os.getenv("API_ID", "25800893")) 
API_HASH = os.getenv("6ccc441e2cbd1ef33ee523fd2349758b", "YOUR_API_HASH")

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
    except: 
        return d

def save(f, d):
    with open(f, "w") as x: 
        json.dump(d, x)

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
    # Reading lengths of lists/dicts is atomic in Python, so no async lock is needed here.
    return jsonify({
        "status": "running", 
        "channels": len(channels), 
        "active_jobs": len(jobs)
    })

def run_flask():
    port = int(os.getenv("PORT", "10000"))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

# --- ACCESS CONTROL ---
def allowed(user_id):
    return user_id == ADMIN or user_id in users

async def allowed_filter(_, __, message: Message):
    return allowed(message.from_user.id) if message.from_user else False
is_allowed = filters.create(allowed_filter)

# --- ASYNC M3U FETCHING ---
async def fetch(url):
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=30) as r:
            r.raise_for_status()
            text = await r.text()
            
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
            errors.append(f"{url}: {str(e)}")
            
    if data:
        async with lock:
            channels = [{"id": i, **c} for i, c in enumerate(data, 1)]
    
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
        
        if os.path.exists(thumb):
            await app.send_video(
                chat, video=file, caption=title[:1024],
                thumb=thumb, supports_streaming=True
            )
            os.remove(thumb)
        else:
            await app.send_video(
                chat, video=file, caption=title[:1024],
                supports_streaming=True
            )
        return True
    except Exception as e:
        print("Upload Error:", e)
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
                        os.remove(segment_file)

    try:
        await app.send_message(chat, f"🔴 Recording: **{title}** for {mins} mins.")
        p = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL, 
            stderr=asyncio.subprocess.PIPE
        )
        
        async with lock:
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
        await app.send_message(chat, f"✅ Recording finished: {title}")
        
    except Exception as e:
        await app.send_message(chat, f"❌ Error: {str(e)[:700]}")
    finally:
        if p and p.returncode is None: 
            try: p.terminate()
            except ProcessLookupError: pass
            
        async with lock:
            jobs.pop(key, None)
            
        if os.path.exists(folder):
            shutil.rmtree(folder, ignore_errors=True)

async def record(chat, url, mins, title):
    if not url.startswith(("http://", "https://")) or not 1 <= mins <= 1440:
        return await app.send_message(chat, "❌ Invalid URL or duration.")
        
    async with lock:
        if any(j["chat"] == chat for j in jobs.values()):
            return await app.send_message(chat, "⚠️ A recording is already running.")
            
        key = f"{chat}_{int(time.time())}"
        jobs[key] = {"chat": chat, "stop": False, "start": time.time(), "title": title}
        
    asyncio.create_task(worker(key, chat, url, mins, title))

# --- KURIGRAM BOT COMMANDS ---
@app.on_message(filters.command(["start", "help"]))
async def help_cmd(client: Client, m: Message):
    text = ("/playlist - All channels\n/p NAME - Search by name\n"
            "/rec ID MINUTES - Record by ID\n/record URL MINUTES - Direct Record\n"
            "/status - Active jobs\n/stop - Stop your recording\n\n"
            "**Admin:**\n"
            "/addplaylist URL\n/reloadplaylist\n"
            "/approve ID\n/revoke ID\n/users\n/stopall")
    await m.reply(text)

@app.on_message(filters.command("addplaylist") & filters.user(ADMIN))
async def add(client: Client, m: Message):
    if len(m.command) < 2: return await m.reply("Usage: /addplaylist URL")
    try:
        new = await fetch(m.command[1])
        async with lock:
            if m.command[1] not in urls:
                urls.append(m.command[1])
                save("playlists.json", urls)
                
        n, err = await reload_all()
        await m.reply(f"✅ Added {len(new)} channels. Total: {n}")
        if err: await m.reply("\n".join(err))
    except Exception as e: 
        await m.reply(f"❌ {e}")

@app.on_message(filters.command("reloadplaylist") & filters.user(ADMIN))
async def reload_cmd(client: Client, m: Message):
    await m.reply("🔄 Reloading channels...")
    n, err = await reload_all()
    await m.reply(f"✅ Reloaded. Channels: {n}")
    if err: await m.reply("Errors during reload:\n" + "\n".join(err))

@app.on_message(filters.command(["playlist", "p", "find"]) & is_allowed)
async def search(client: Client, m: Message):
    key = " ".join(m.command[1:]).lower() if len(m.command) > 1 else ""
    if m.command[0] in ["p", "find"] and not key:
        return await m.reply("Usage: /p Channel Name")
        
    async with lock:
        result = [c for c in channels if key in c["name"].lower()]
        
    if not result: return await m.reply("❌ No channels found")
        
    text = "\n".join(f"🆔 `{c['id']}` — {c['name']}" for c in result)
    for i in range(0, len(text), 3500):
        chunk = text[i:i+3500]
        if i == 0: chunk += "\n\nUse: /rec ID MINUTES"
        await m.reply(chunk)

@app.on_message(filters.command("rec") & is_allowed)
async def rec(client: Client, m: Message):
    if len(m.command) < 3: return await m.reply("Usage: /rec ID MINUTES [TITLE]")
    try:
        channel_id = int(m.command[1])
        mins = int(m.command[2])
        async with lock:
            c = next((x for x in channels if x["id"] == channel_id), None)
        if not c:
            return await m.reply("❌ Channel ID not found.")
            
        title = " ".join(m.command[3:]) if len(m.command) > 3 else c["name"]
        await record(m.chat.id, c["url"], mins, title)
    except ValueError:
        await m.reply("❌ ID and MINUTES must be numbers.")

@app.on_message(filters.command("record") & is_allowed)
async def direct(client: Client, m: Message):
    if len(m.command) < 3: return await m.reply("Usage: /record URL MINUTES [TITLE]")
    try:
        mins = int(m.command[2])
        title = " ".join(m.command[3:]) if len(m.command) > 3 else "Direct Recording"
        await record(m.chat.id, m.command[1], mins, title)
    except ValueError: 
        await m.reply("❌ MINUTES must be a number.")

@app.on_message(filters.command("status") & is_allowed)
async def status(client: Client, m: Message):
    async with lock:
        if not jobs: return await m.reply("No active recordings")
        text = "\n".join(f"🎥 {j['title']} - Runtime: {int(time.time()-j['start'])} sec" for j in jobs.values())
    await m.reply(text)

@app.on_message(filters.command(["stop", "stopall"]) & is_allowed)
async def stop(client: Client, m: Message):
    is_stopall = m.command[0] == "stopall"
    if is_stopall and m.from_user.id != ADMIN: return
    
    n = 0
    async with lock:
        for j in jobs.values():
            if m.from_user.id == ADMIN or j["chat"] == m.chat.id:
                j["stop"] = True
                if j.get("process"): 
                    try: j["process"].terminate()
                    except ProcessLookupError: pass
                n += 1
    await m.reply(f"🛑 Stop requests issued: {n}")

@app.on_message(filters.command(["approve", "revoke"]) & filters.user(ADMIN))
async def access(client: Client, m: Message):
    if len(m.command) < 2: return await m.reply("Usage: /approve USER_ID")
    try:
        uid = int(m.command[1])
        async with lock:
            if m.command[0] == "approve": users.add(uid)
            else: users.discard(uid)
            save("approved_users.json", list(users))
        await m.reply(f"✅ Access updated for {uid}")
    except ValueError: 
        await m.reply("❌ USER_ID must be a number.")

@app.on_message(filters.command("users") & filters.user(ADMIN))
async def users_cmd(client: Client, m: Message):
    async with lock:
        text = "\n".join(map(str, sorted(users))) if users else "No approved users."
    await m.reply("**Approved Users:**\n" + text)

# --- BOT LAUNCHER ---
async def main():
    # 1. Fetch initial playlists asynchronously
    await reload_all()
    
    # 2. Start Kurigram
    await app.start()
    print("Bot is polling via MTProto...")
    await idle()
    await app.stop()

if __name__ == "__main__":
    # Start Flask Web Server in a Background Thread
    threading.Thread(target=run_flask, daemon=True).start()
    
    # Run the Kurigram async event loop in the Main Thread
    app.run(main())
