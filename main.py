import discord
from discord import app_commands
import asyncio
import os
import logging
from collections import deque
import datetime
import subprocess
import json

# --- 1) 基礎設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s: %(message)s')
logger = logging.getLogger('MusicBot')

CUSTOM_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/120.0.0.0 Safari/537.36'
)

# --- 2) FFmpeg 設定（穩定版）---
# ✅ 核心修正：移除 aresample async=1（會導致突然加速/跳段）
# ✅ 強化 reconnect 與 buffer，增加串流耐受度
FFMPEG_OPTIONS = {
    'before_options': (
        '-nostdin '
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1 '
        '-rw_timeout 20000000 '
        '-thread_queue_size 8192 '
        f'-user_agent "{CUSTOM_UA}" '
        '-headers "Referer: https://www.youtube.com/\r\n"'
    ),
    'options': '-vn -ac 2 -ar 48000 -af "volume=0.5"',
}

# --- 3) yt-dlp（CLI）---
def get_info_via_cli(url, is_search=False, flat=False):
    """
    flat=True  -> 快速拿基本資訊（例如 playlist entries / search）
    flat=False -> 取得可播放的真實串流 URL（full info）
    """
    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-warnings",
        "--quiet",
        "--force-ipv4",
        "-f", "bestaudio[ext=m4a]/bestaudio/best",
    ]

    if flat:
        cmd.append("--flat-playlist")
    else:
        cmd.append("--no-playlist")

    # full 模式才需要 pot provider 參數（用於降低 403 / SABR）
    if not flat:
        cmd.extend([
            "--remote-components", "ejs:github",
            "--extractor-args", "youtubepot-bgutilhttp:base_url=http://bgutil-provider:4416",
            "--extractor-args", "youtube:player_client=web",
        ])

    if is_search:
        cmd.append(f"ytsearch:{url}")
    else:
        cmd.append(url)

    logger.info(f"yt-dlp (flat={flat}) => {' '.join(cmd)}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    lines = [line for line in result.stdout.strip().split('\n') if line.strip()]
    if not lines:
        return None
    parsed = [json.loads(line) for line in lines]
    return parsed if len(parsed) > 1 else parsed[0]


# --- 4) Embed ---
def get_now_playing_embed(data):
    title = data.get('title', '未知標題')
    url = data.get('webpage_url') or data.get('url')
    duration = data.get('duration')
    duration_str = str(datetime.timedelta(seconds=duration)) if duration else "直播/未知"

    embed = discord.Embed(
        title="🎵 正在播放",
        description=f"[{title}]({url})",
        color=0x1db954
    )
    if data.get('thumbnail'):
        embed.set_thumbnail(url=data['thumbnail'])
    embed.add_field(name="⏱️ 時間", value=duration_str, inline=True)
    return embed


async def ensure_voice(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ 我不在語音頻道中", ephemeral=True)
        return None, None
    return vc, interaction.guild_id


# --- 5) 主 Bot ---
class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

        self.queues = {}           # guild_id -> deque
        self.current_song = {}     # guild_id -> full_data
        self.music_channels = {}   # guild_id -> text channel
        self._loop = None          # 主 event loop（給 after callback 排程）

    async def setup_hook(self):
        await self.tree.sync()
        self._loop = asyncio.get_running_loop()
        logger.info("✅ 指令同步完成")

    def cleanup_guild_state(self, guild_id: int):
        if guild_id in self.queues:
            self.queues[guild_id].clear()
        self.current_song[guild_id] = None

    async def play_next(self, guild_id: int):
        guild = self.get_guild(guild_id)
        if not guild:
            return

        vc = guild.voice_client
        if not vc or not vc.is_connected():
            return

        queue = self.queues.setdefault(guild_id, deque())

        if not queue:
            self.current_song[guild_id] = None
            return

        track_basic = queue.popleft()

        video_id = track_basic.get('id')
        url = f"https://www.youtube.com/watch?v={video_id}" if video_id else (
            track_basic.get('webpage_url') or track_basic.get('url')
        )

        logger.info(f"準備播放: {track_basic.get('title')}")

        try:
            loop = asyncio.get_running_loop()
            full_data = await loop.run_in_executor(None, lambda: get_info_via_cli(url, flat=False))
            if isinstance(full_data, list):
                full_data = full_data[0]

            self.current_song[guild_id] = full_data

            source = discord.FFmpegPCMAudio(full_data['url'], **FFMPEG_OPTIONS)
            player = discord.PCMVolumeTransformer(source, volume=1.0)

            def after_playing(error):
                if error:
                    logger.error(f"播放錯誤: {error}")

                # after callback 在別的 thread，用主 loop 安全排程下一首
                if self._loop and not self._loop.is_closed():
                    asyncio.run_coroutine_threadsafe(self.play_next(guild_id), self._loop)

            vc.play(player, after=after_playing)

            ch = self.music_channels.get(guild_id)
            if ch:
                await ch.send(embed=get_now_playing_embed(full_data))

        except Exception as e:
            logger.error(f"播放失敗: {e}")
            await asyncio.sleep(1)
            await self.play_next(guild_id)


bot = MusicBot()

# =========================
# 6) Slash Commands
# =========================

@bot.tree.command(name="play", description="播放音樂 (網址/清單/搜尋)")
@app_commands.describe(search="網址或關鍵字")
async def play(interaction: discord.Interaction, search: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        return await interaction.followup.send("❌ 請先加入語音頻道")

    guild_id = interaction.guild_id
    bot.music_channels[guild_id] = interaction.channel

    vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect()
    queue = bot.queues.setdefault(guild_id, deque())

    try:
        loop = asyncio.get_running_loop()
        is_url = search.startswith('http')

        data = await loop.run_in_executor(None, lambda: get_info_via_cli(search, is_search=not is_url, flat=True))

        if isinstance(data, list):
            # playlist
            added = 0
            for entry in data:
                if entry.get('id'):
                    queue.append(entry)
                    added += 1
            await interaction.followup.send(f"📂 已載入清單：**{added}** 首歌")
        else:
            entries = data.get('entries')
            track = entries[0] if entries else data
            queue.append(track)
            await interaction.followup.send(f"✅ 已加入佇列: **{track.get('title', '未知標題')}**")

        if not vc.is_playing() and not vc.is_paused():
            await bot.play_next(guild_id)

    except Exception as e:
        logger.error(f"Play Error: {e}")
        await interaction.followup.send(f"⚠️ 錯誤: {e}")


@bot.tree.command(name="insert", description="插播（下一首播放，不打斷）(網址/清單/搜尋)")
@app_commands.describe(search="網址或關鍵字")
async def insert(interaction: discord.Interaction, search: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        return await interaction.followup.send("❌ 請先加入語音頻道")

    guild_id = interaction.guild_id
    bot.music_channels[guild_id] = interaction.channel

    vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect()
    queue = bot.queues.setdefault(guild_id, deque())

    try:
        loop = asyncio.get_running_loop()
        is_url = search.startswith('http')

        data = await loop.run_in_executor(None, lambda: get_info_via_cli(search, is_search=not is_url, flat=True))

        if isinstance(data, list):
            inserted = [e for e in data if e.get('id')]
            for entry in reversed(inserted):
                queue.appendleft(entry)
            await interaction.followup.send(f"📌 已插播清單：**{len(inserted)}** 首（將從下一首開始播放）")
        else:
            entries = data.get('entries')
            track = entries[0] if entries else data
            queue.appendleft(track)
            await interaction.followup.send(f"📌 已插播: **{track.get('title', '未知標題')}**（下一首播放）")

        # 若目前沒在播，也沒暫停，就直接開始播
        if not vc.is_playing() and not vc.is_paused():
            await bot.play_next(guild_id)

    except Exception as e:
        logger.error(f"Insert Error: {e}")
        await interaction.followup.send(f"⚠️ 錯誤: {e}")


@bot.tree.command(name="interrupt", description="立刻插播（直接切掉目前歌曲）(網址/清單/搜尋)")
@app_commands.describe(search="網址或關鍵字")
async def interrupt(interaction: discord.Interaction, search: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        return await interaction.followup.send("❌ 請先加入語音頻道")

    guild_id = interaction.guild_id
    bot.music_channels[guild_id] = interaction.channel

    vc = interaction.guild.voice_client or await interaction.user.voice.channel.connect()
    queue = bot.queues.setdefault(guild_id, deque())

    try:
        loop = asyncio.get_running_loop()
        is_url = search.startswith('http')

        data = await loop.run_in_executor(None, lambda: get_info_via_cli(search, is_search=not is_url, flat=True))

        if isinstance(data, list):
            inserted = [e for e in data if e.get('id')]
            for entry in reversed(inserted):
                queue.appendleft(entry)
            await interaction.followup.send(f"🚨 立刻插播清單：**{len(inserted)}** 首（現在立刻切歌播放）")
        else:
            entries = data.get('entries')
            track = entries[0] if entries else data
            queue.appendleft(track)
            await interaction.followup.send(f"🚨 立刻插播: **{track.get('title', '未知標題')}**（現在立刻切歌播放）")

        # ✅ 正在播放或暫停：直接 stop，觸發 after callback 播下一首（也就是我們剛插播那首）
        if vc.is_playing() or vc.is_paused():
            vc.stop()
        else:
            await bot.play_next(guild_id)

    except Exception as e:
        logger.error(f"Interrupt Error: {e}")
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
async def queue_cmd(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    queue = bot.queues.get(guild_id)

    if not queue:
        return await interaction.response.send_message("📭 佇列是空的")

    items = list(queue)[:10]
    msg = [f"📜 **排隊清單 (共 {len(queue)} 首):**"] + [
        f"`{i}.` {s.get('title', '未知標題')}" for i, s in enumerate(items, 1)
    ]
    if len(queue) > 10:
        msg.append(f"...還有 {len(queue) - 10} 首")

    await interaction.response.send_message("\n".join(msg))


@bot.tree.command(name="skip", description="跳過")
async def skip(interaction: discord.Interaction):
    vc, guild_id = await ensure_voice(interaction)
    if not vc:
        return

    if not vc.is_playing():
        return await interaction.response.send_message("❌ 沒在播歌", ephemeral=True)

    queue = bot.queues.get(guild_id)
    if queue and len(queue) > 0:
        await interaction.response.send_message(f"⏭️ 跳過！下一首: **{queue[0].get('title', '未知標題')}**")
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
    if not vc:
        return

    bot.cleanup_guild_state(guild_id)

    if vc.is_playing() or vc.is_paused():
        vc.stop()
        await interaction.response.send_message("⏹️ 已停止並清空")
    else:
        await interaction.response.send_message("⏹️ 已清空狀態")


@bot.tree.command(name="pause", description="暫停")
async def pause(interaction: discord.Interaction):
    vc, _ = await ensure_voice(interaction)
    if not vc:
        return

    if vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ 暫停")
    else:
        await interaction.response.send_message("⚠️ 非播放中")


@bot.tree.command(name="resume", description="繼續")
async def resume(interaction: discord.Interaction):
    vc, _ = await ensure_voice(interaction)
    if not vc:
        return

    if vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ 繼續")
    else:
        await interaction.response.send_message("⚠️ 非暫停中")


@bot.tree.command(name="leave", description="離開")
async def leave(interaction: discord.Interaction):
    vc, guild_id = await ensure_voice(interaction)
    if not vc:
        return

    bot.cleanup_guild_state(guild_id)
    await vc.disconnect()
    await interaction.response.send_message("👋")


if __name__ == "__main__":
    token = os.getenv('BOT_TOKEN')
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Please create .env with BOT_TOKEN=....")
    bot.run(token)
