import discord
from discord import app_commands
import yt_dlp
import asyncio
import os
import logging
from collections import deque
import datetime

# --- 1. 基礎設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s: %(message)s')
logger = logging.getLogger('MusicBot')
CUSTOM_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# --- 2. yt-dlp 設定 ---
BASE_YTDL_OPTS = {
    'format': 'bestaudio[ext=m4a]/bestaudio/best',
    'default_search': 'ytsearch',
    'quiet': True,
    'ignoreerrors': True,
    'nocheckcertificate': True,
    'user_agent': CUSTOM_UA,
    'source_address': '0.0.0.0',
}

SEARCH_OPTS = BASE_YTDL_OPTS | {'extract_flat': 'in_playlist', 'noplaylist': False}
STREAM_OPTS = BASE_YTDL_OPTS | {'extract_flat': False, 'noplaylist': True}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn -ar 48000 -ac 2 -b:a 192k -filter:a "volume=0.5"',
}

# --- 3. 輔助函式 ---
def get_now_playing_embed(data):
    """產生播放資訊卡片"""
    title = data.get('title', '未知標題')
    url = data.get('webpage_url') or data.get('url')
    duration = data.get('duration')
    duration_str = str(datetime.timedelta(seconds=duration)) if duration else "未知/直播"

    embed = discord.Embed(title="🎵 正在播放", description=f"[{title}]({url})", color=0x1db954)
    if data.get('thumbnail'): embed.set_thumbnail(url=data['thumbnail'])
    embed.add_field(name="⏱️ 時間", value=duration_str, inline=True)
    return embed

async def ensure_voice(interaction: discord.Interaction):
    """檢查連線並回傳 (voice_client, guild_id)，失敗則回傳 (None, None)"""
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ 我不在語音頻道中", ephemeral=True)
        return None, None
    return vc, interaction.guild_id

# --- 4. 主架構 ---
class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        
        self.tree = app_commands.CommandTree(self)
        
        self.queues = {}       
        self.current_song = {} 
        self.music_channels = {}

    async def setup_hook(self):
        # 同步指令
        await self.tree.sync()
        logger.info("✅ 指令同步完成 (Pure Slash Mode)")

    def cleanup_guild_state(self, guild_id):
        if guild_id in self.queues: self.queues[guild_id].clear()
        self.current_song[guild_id] = None

    async def play_next(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        vc = interaction.guild.voice_client

        if not vc or not vc.is_connected(): return

        queue = self.queues.setdefault(guild_id, deque())

        if queue:
            track_info = queue.popleft()
            self.current_song[guild_id] = track_info
            
            url = track_info.get('webpage_url') or track_info.get('url')
            if not url or not url.startswith('http'):
                url = f"https://www.youtube.com/watch?v={track_info.get('id')}"

            logger.info(f"解析: {track_info.get('title')}")

            try:
                loop = asyncio.get_running_loop()
                with yt_dlp.YoutubeDL(STREAM_OPTS) as ydl:
                    data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
                
                if 'entries' in data: data = data['entries'][0]

                source = discord.FFmpegPCMAudio(data['url'], **FFMPEG_OPTIONS)
                player = discord.PCMVolumeTransformer(source, volume=0.5)
                player.title = data.get('title', '未知')

                def after_playing(error):
                    if error: logger.error(f"播放錯誤: {error}")
                    asyncio.run_coroutine_threadsafe(self.play_next(interaction), loop)

                vc.play(player, after=after_playing)
                
                if guild_id in self.music_channels:
                    await self.music_channels[guild_id].send(embed=get_now_playing_embed(data))

            except Exception as e:
                logger.error(f"播放失敗: {e}")
                await asyncio.sleep(1)
                await self.play_next(interaction)
        else:
            self.current_song[guild_id] = None

bot = MusicBot()

# --- 5. 指令區 ---

@bot.tree.command(name="play", description="播放音樂 (網址/清單/搜尋)")
@app_commands.describe(search="網址或關鍵字")
async def play(interaction: discord.Interaction, search: str):
    await interaction.response.defer()
    
    if not interaction.user.voice:
        return await interaction.followup.send("❌ 請先加入語音頻道")
    
    bot.music_channels[interaction.guild_id] = interaction.channel
    vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect()
    guild_id = interaction.guild_id
    
    queue = bot.queues.setdefault(guild_id, deque())

    try:
        # 這裡也要改用 asyncio loop
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(search, download=False))

        entries = data.get('entries') or [data]
        
        count = 0
        for entry in entries:
            if entry:
                queue.append(entry)
                count += 1
        
        if 'entries' in data:
            await interaction.followup.send(f"📂 已載入清單：**{count}** 首歌")
        else:
            await interaction.followup.send(f"✅ 已加入佇列: **{data.get('title')}**")

        if not vc.is_playing() and not vc.is_paused():
            await bot.play_next(interaction)

    except Exception as e:
        logger.error(f"Play Error: {e}")
        await interaction.followup.send(f"⚠️ 錯誤: {e}")

@bot.tree.command(name="nowplaying", description="顯示播放資訊")
async def nowplaying(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    current = bot.current_song.get(guild_id)
    if current:
        await interaction.response.send_message(embed=get_now_playing_embed(current))
    else:
        await interaction.response.send_message("❌ 沒在播歌", ephemeral=True)

@bot.tree.command(name="queue", description="顯示清單")
async def queue(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queue = bot.queues.get(guild_id)

    if not queue:
        return await interaction.response.send_message("📭 佇列是空的")

    items = list(queue)[:10] 
    msg = [f"📜 **排隊清單 (共 {len(queue)} 首):**"] + [f"`{i}.` {s.get('title')}" for i, s in enumerate(items, 1)]
    if len(queue) > 10: msg.append(f"...還有 {len(queue)-10} 首")
    
    await interaction.response.send_message("\n".join(msg))

@bot.tree.command(name="skip", description="跳過")
async def skip(interaction: discord.Interaction):
    vc, guild_id = await ensure_voice(interaction)
    if not vc: return

    if not vc.is_playing():
        return await interaction.response.send_message("❌ 沒在播歌", ephemeral=True)

    queue = bot.queues.get(guild_id)
    if queue:
        await interaction.response.send_message(f"⏭️ 跳過！下一首: **{queue[0].get('title')}**")
    else:
        await interaction.response.send_message("⏭️ 跳過 (清單將結束)")
    vc.stop()

@bot.tree.command(name="remove", description="清空清單 (保留目前播放)")
async def remove(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queue = bot.queues.get(guild_id)
    
    if queue:
        count = len(queue)
        queue.clear()
        await interaction.response.send_message(f"🗑️ 已清空 **{count}** 首歌")
    else:
        await interaction.response.send_message("📭 本來就是空的")

@bot.tree.command(name="stop", description="停止並清空")
async def stop(interaction: discord.Interaction):
    vc, guild_id = await ensure_voice(interaction)
    if not vc: return

    bot.cleanup_guild_state(guild_id)

    if vc.is_playing() or vc.is_paused():
        vc.stop()
        await interaction.response.send_message("⏹️ 已停止並清空")
    else:
        await interaction.response.send_message("⏹️ 已清空狀態")

@bot.tree.command(name="pause", description="暫停")
async def pause(interaction: discord.Interaction):
    vc, _ = await ensure_voice(interaction)
    if not vc: return

    if vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ 暫停")
    else:
        await interaction.response.send_message("⚠️ 非播放中")

@bot.tree.command(name="resume", description="繼續")
async def resume(interaction: discord.Interaction):
    vc, _ = await ensure_voice(interaction)
    if not vc: return

    if vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ 繼續")
    else:
        await interaction.response.send_message("⚠️ 非暫停中")

@bot.tree.command(name="leave", description="離開")
async def leave(interaction: discord.Interaction):
    vc, guild_id = await ensure_voice(interaction)
    if not vc: return

    bot.cleanup_guild_state(guild_id)
    await vc.disconnect()
    await interaction.response.send_message("👋")

if __name__ == "__main__":
    bot.run(os.getenv('BOT_TOKEN'))