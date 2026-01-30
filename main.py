import discord
from discord import app_commands
import asyncio
import os
import logging
from collections import deque
import datetime
import subprocess
import json

# --- 1. 基礎設定 ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s: %(message)s')
logger = logging.getLogger('MusicBot')
CUSTOM_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

# --- 2. FFmpeg 設定 ---
FFMPEG_OPTIONS = {
    'before_options': (
        '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 '
        '-rw_timeout 20000000 '
        '-thread_queue_size 4096 '
        f'-user_agent "{CUSTOM_UA}" ' 
        '-headers "Referer: https://www.youtube.com/\r\n"'
    ),
    'options': '-vn -b:a 192k -filter:a "aresample=48000:async=1,volume=0.5"',
}


# --- 3. yt-dlp 設定 ---
def get_info_via_cli(url, is_search=False, flat=False):
    cmd = ["yt-dlp", "--dump-json", "--no-warnings", "--quiet", "--force-ipv4", "-f", "bestaudio[ext=m4a]/bestaudio/best"]

    if flat:
        cmd.append("--flat-playlist")
    else:
        cmd.append("--no-playlist")

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

    logger.info(f"執行指令 (Flat={flat}): {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        lines = [line for line in result.stdout.strip().split('\n') if line.strip()]

        if not lines: return None

        parsed_results = [json.loads(line) for line in lines]

        return parsed_results if len(parsed_results) > 1 else parsed_results[0]

    except Exception as e:
        logger.error(f"解析失敗: {e}")
        raise e


# --- 4. 輔助函式 ---
def get_now_playing_embed(data):
    title = data.get('title', '未知標題')
    url = data.get('webpage_url') or data.get('url')
    duration = data.get('duration')
    duration_str = str(datetime.timedelta(seconds=duration)) if duration else "直播/未知"

    embed = discord.Embed(title="🎵 正在播放", description=f"[{title}]({url})", color=0x1db954)
    if data.get('thumbnail'): embed.set_thumbnail(url=data['thumbnail'])
    embed.add_field(name="⏱️ 時間", value=duration_str, inline=True)
    return embed


async def ensure_voice(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if not vc or not vc.is_connected():
        await interaction.response.send_message("❌ 我不在語音頻道中", ephemeral=True)
        return None, None
    return vc, interaction.guild_id


# --- 5. 主架構 ---
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
        await self.tree.sync()
        logger.info("✅ 指令同步完成")

    def cleanup_guild_state(self, guild_id):
        if guild_id in self.queues: self.queues[guild_id].clear()
        self.current_song[guild_id] = None

    async def play_next(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        vc = interaction.guild.voice_client

        if not vc or not vc.is_connected(): return

        queue = self.queues.setdefault(guild_id, deque())

        if queue:
            track_basic = queue.popleft()
            self.current_song[guild_id] = track_basic

            video_id = track_basic.get('id')
            url = f"https://www.youtube.com/watch?v={video_id}" if video_id else (
                    track_basic.get('webpage_url') or track_basic.get('url'))

            logger.info(f"解析串流: {track_basic.get('title')}")

            try:
                loop = asyncio.get_running_loop()
                full_data = await loop.run_in_executor(None, lambda: get_info_via_cli(url, flat=False))

                if isinstance(full_data, list): full_data = full_data[0]

                source = discord.FFmpegPCMAudio(full_data['url'], **FFMPEG_OPTIONS)
                player = discord.PCMVolumeTransformer(source, volume=1.0)
                player.title = full_data.get('title', '未知')

                def after_playing(error):
                    if error: logger.error(f"播放錯誤: {error}")
                    asyncio.run_coroutine_threadsafe(self.play_next(interaction), loop)

                vc.play(player, after=after_playing)

                if guild_id in self.music_channels:
                    await self.music_channels[guild_id].send(embed=get_now_playing_embed(full_data))

            except Exception as e:
                logger.error(f"播放失敗: {e}")
                await asyncio.sleep(1)
                await self.play_next(interaction)
        else:
            self.current_song[guild_id] = None


bot = MusicBot()


# --- 6. 指令區 ---
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
        loop = asyncio.get_running_loop()
        is_url = search.startswith('http')

        data = await loop.run_in_executor(None, lambda: get_info_via_cli(search, is_search=not is_url, flat=True))

        count = 0
        if isinstance(data, list):
            # 清單處理
            for entry in data:
                if entry.get('id'):
                    queue.append(entry)
                    count += 1
            await interaction.followup.send(f"📂 已載入清單：**{count}** 首歌")
        else:
            # 單曲處理
            entries = data.get('entries')
            if entries:
                queue.append(entries[0])
                title = entries[0].get('title')
            else:
                queue.append(data)
                title = data.get('title')
            await interaction.followup.send(f"✅ 已加入佇列: **{title}**")

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
    if len(queue) > 10: msg.append(f"...還有 {len(queue) - 10} 首")

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
