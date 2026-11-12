import asyncio
import logging
import math
import os
import random
from collections import deque
from urllib.parse import urlparse

import discord
from discord import app_commands
from dotenv import load_dotenv

from audio_cache import AudioPreparationError, LocalAudioSource, download_audio
from music_controls import PlaybackControls
from discord_helpers import ensure_voice, get_now_playing_embed
from youtube import ExtractionError, extract_tracks, get_info_via_cli, track_url, ytdlp_command
from voice_timing import StableVoiceClient, audio_time

load_dotenv()
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s: %(message)s')
logger = logging.getLogger('MusicBot')

class MusicBot(discord.Client):
    def __init__(self, *, idle_timeout=None):
        super().__init__(intents=discord.Intents.default(), allowed_mentions=discord.AllowedMentions.none())
        self.tree = app_commands.CommandTree(self)
        self.queues = {}
        self.current_song = {}
        self.music_channels = {}
        self.locks = {}
        self.connection_locks = {}
        self.preloads = {}          # guild_id -> (queue entry, download task)
        self.preparations = {}      # guild_id -> currently awaited download task
        self.generations = {}
        self.playback_tokens = {}
        self.control_messages = {}  # guild_id -> (message, playback token)
        self.control_view = None
        self.control_edits = set()
        self.idle_timeout = float(os.getenv('IDLE_TIMEOUT_SECONDS', '300') if idle_timeout is None else idle_timeout)
        if not math.isfinite(self.idle_timeout) or self.idle_timeout < 0:
            raise ValueError('IDLE_TIMEOUT_SECONDS 必須是非負數。')
        self.idle_tasks = {}
        self.idle_keys = {}
        self.pending_requests = {}
        self.shutting_down = False
        self._loop = None

    def guild_lock(self, guild_id):
        return self.locks.setdefault(guild_id, asyncio.Lock())

    def get_control_view(self):
        if self.control_view is None:
            self.control_view = PlaybackControls(self)
        return self.control_view

    def remove_message_controls(self, message):
        async def remove():
            try:
                await message.edit(view=None)
            except discord.HTTPException:
                logger.debug('無法移除舊的播放控制按鈕')
        task = asyncio.create_task(remove())
        self.control_edits.add(task)
        task.add_done_callback(self.control_edits.discard)

    def retire_controls(self, guild_id):
        previous = self.control_messages.pop(guild_id, None)
        if previous:
            self.remove_message_controls(previous[0])

    def register_controls(self, guild_id, message, token):
        if token is None or self.playback_tokens.get(guild_id) is not token:
            self.remove_message_controls(message)
            return
        self.retire_controls(guild_id)
        self.control_messages[guild_id] = (message, token)

    async def authorize_controls(self, interaction):
        panel = self.control_messages.get(interaction.guild_id)
        if (not panel or not interaction.message or panel[0].id != interaction.message.id
                or self.playback_tokens.get(interaction.guild_id) is not panel[1]):
            await interaction.response.send_message('ℹ️ 這個播放器已結束，請使用最新的播放訊息或 /nowplaying。', ephemeral=True)
            return False
        vc, _ = await ensure_voice(interaction)
        return vc is not None

    async def announce_track(self, guild_id, data):
        token = self.playback_tokens.get(guild_id)
        if token is None:
            return
        message = await self.send_music_message(
            guild_id, embed=get_now_playing_embed(data), view=self.get_control_view(),
        )
        if message is not None:
            self.register_controls(guild_id, message, token)

    def cancel_idle(self, guild_id):
        task = self.idle_tasks.pop(guild_id, None)
        self.idle_keys.pop(guild_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def idle_reason(self, guild_id):
        guild = self.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_connected():
            return None
        if not any(not member.bot for member in vc.channel.members):
            return 'alone'
        if (vc.is_playing() or vc.is_paused() or self.queues.get(guild_id)
                or self.preparations.get(guild_id) or self.pending_requests.get(guild_id)
                or self.guild_lock(guild_id).locked()):
            return None
        return 'finished'

    def update_idle(self, guild_id):
        reason = None if self.shutting_down or self.idle_timeout == 0 else self.idle_reason(guild_id)
        if reason is None:
            self.cancel_idle(guild_id)
            return
        vc = self.get_guild(guild_id).voice_client
        key = (vc, vc.channel.id, reason)
        task = self.idle_tasks.get(guild_id)
        if task and not task.done() and self.idle_keys.get(guild_id) == key:
            return
        self.cancel_idle(guild_id)
        self.idle_keys[guild_id] = key
        self.idle_tasks[guild_id] = asyncio.create_task(self.leave_when_idle(guild_id, *key))

    async def wait_idle_timeout(self):
        # asyncio timers use the adjusted guest clock; verify the raw deadline.
        deadline = audio_time() + self.idle_timeout
        while (remaining := deadline - audio_time()) > 0:
            await asyncio.sleep(min(remaining, 5))

    async def leave_when_idle(self, guild_id, vc, channel_id, reason):
        try:
            await self.wait_idle_timeout()
            async with self.connection_locks.setdefault(guild_id, asyncio.Lock()):
                guild = self.get_guild(guild_id)
                if (not guild or guild.voice_client is not vc or vc.channel.id != channel_id
                        or self.idle_reason(guild_id) != reason):
                    return
                self.cleanup_guild_state(guild_id)
                vc.stop()
                await vc.disconnect()
            explanation = '語音頻道已沒有人' if reason == 'alone' else '清單已播完'
            await self.send_music_message(guild_id, content=f'👋 {explanation}，閒置逾時後已自動離開。')
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception('自動離開語音頻道失敗')
        finally:
            if self.idle_tasks.get(guild_id) is asyncio.current_task():
                self.idle_tasks.pop(guild_id, None)
                self.idle_keys.pop(guild_id, None)

    async def handle_control_action(self, interaction, action, *, private=False):
        vc, guild_id = await ensure_voice(interaction)
        if not vc:
            return
        if action == 'toggle':
            action = 'resume' if vc.is_paused() else 'pause'
        if action == 'pause':
            if not vc.is_playing():
                return await interaction.response.send_message('⚠️ 非播放中', ephemeral=True)
            vc.pause()
            message = '⏸️ 暫停'
        elif action == 'resume':
            if not vc.is_paused():
                return await interaction.response.send_message('⚠️ 非暫停中', ephemeral=True)
            vc.resume()
            message = '▶️ 繼續'
        elif action == 'skip':
            self.invalidate_playback(guild_id)
            vc.stop()
            message = '⏭️ 已跳過，將播放下一首'
        elif action == 'stop':
            self.cleanup_guild_state(guild_id)
            vc.stop()
            message = '⏹️ 已停止並清空'
        elif action == 'shuffle':
            count = self.shuffle_queue(guild_id)
            if count < 2:
                message = '📭 佇列是空的，請先用 /play 加入歌曲' if count == 0 else 'ℹ️ 等待中的歌曲只有 1 首，需要至少 2 首才能隨機排列'
                return await interaction.response.send_message(message, ephemeral=True)
            message = f'🔀 已隨機排列 **{count}** 首等待中的歌曲。'
        else:
            raise ValueError('Unknown playback action')
        self.update_idle(guild_id)
        await interaction.response.send_message(message, ephemeral=private)
        if action == 'skip' or (action == 'shuffle' and not vc.is_playing() and not vc.is_paused()):
            await self.play_next(guild_id)

    @staticmethod
    def release_download(task):
        def release(done):
            if not done.cancelled():
                try:
                    done.result().cleanup()
                except Exception:
                    logger.debug('背景快取已取消或清理失敗', exc_info=True)
        if task.done():
            release(task)
        else:
            task.add_done_callback(release)
            task.cancel()

    def discard_preload(self, guild_id):
        previous = self.preloads.pop(guild_id, None)
        if previous:
            self.release_download(previous[1])

    def new_download(self, track):
        async def prepare():
            return await download_audio(track_url(track), ytdlp_command())
        task = asyncio.create_task(prepare())
        # Retrieve early failures even if the song is removed before it plays.
        task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        return task

    def preload_next(self, guild_id):
        queue = self.queues.get(guild_id)
        previous = self.preloads.get(guild_id)
        if previous and queue and previous[0] is queue[0]:
            return
        self.discard_preload(guild_id)
        if queue:
            self.preloads[guild_id] = (queue[0], self.new_download(queue[0]))

    def shuffle_queue(self, guild_id):
        queue = self.queues.get(guild_id)
        count = len(queue) if queue else 0
        if count < 2:
            return count
        tracks = list(queue)
        random.shuffle(tracks)
        # Keep the deque itself: an in-progress player may already reference it.
        # This operation has no await, so playback cannot pop a half-updated queue.
        queue.clear()
        queue.extend(tracks)
        self.preload_next(guild_id)
        return count

    async def prepare_track(self, guild_id, track):
        previous = self.preloads.pop(guild_id, None)
        if previous and previous[0] is track:
            task = previous[1]
        else:
            if previous:
                self.release_download(previous[1])
            task = self.new_download(track)
        self.preparations[guild_id] = task
        try:
            return await task
        except asyncio.CancelledError:
            self.release_download(task)
            raise
        finally:
            if self.preparations.get(guild_id) is task:
                self.preparations.pop(guild_id, None)

    async def close(self):
        self.shutting_down = True
        tasks = list(self.preparations.values()) + [entry[1] for entry in self.preloads.values()] + list(self.idle_tasks.values())
        for guild_id in set(self.queues) | set(self.preloads) | set(self.preparations) | set(self.idle_tasks) | set(self.control_messages):
            self.cleanup_guild_state(guild_id)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.control_edits:
            await asyncio.gather(*self.control_edits, return_exceptions=True)
        if self.control_view is not None:
            self.control_view.stop()
        await super().close()

    async def on_voice_state_update(self, member, before, after):
        if self.user and member.id == self.user.id and before.channel and not after.channel:
            self.cleanup_guild_state(member.guild.id)
            return
        guild_id = member.guild.id
        guild = self.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if vc and (before.channel == vc.channel or after.channel == vc.channel):
            self.update_idle(guild_id)

    async def setup_hook(self):
        self._loop = asyncio.get_running_loop()
        self.add_view(self.get_control_view())
        await self.tree.sync()
        logger.info('✅ 指令同步完成（discord.py %s）', discord.__version__)

    def invalidate_playback(self, guild_id):
        self.generations[guild_id] = self.generations.get(guild_id, 0) + 1
        self.playback_tokens.pop(guild_id, None)
        self.current_song.pop(guild_id, None)
        self.retire_controls(guild_id)
        preparation = self.preparations.get(guild_id)
        if preparation:
            preparation.cancel()

    def cleanup_guild_state(self, guild_id):
        self.cancel_idle(guild_id)
        self.queues.setdefault(guild_id, deque()).clear()
        self.discard_preload(guild_id)
        self.invalidate_playback(guild_id)

    async def send_music_message(self, guild_id, **kwargs):
        channel = self.music_channels.get(guild_id)
        if channel:
            try:
                return await channel.send(**kwargs)
            except discord.HTTPException:
                logger.warning('無法傳送播放訊息到伺服器 %s', guild_id)

    async def finish_track(self, guild_id, token, error):
        if self.playback_tokens.get(guild_id) is not token:
            return
        self.playback_tokens.pop(guild_id, None)
        self.current_song.pop(guild_id, None)
        self.retire_controls(guild_id)
        if error:
            logger.error('音訊播放錯誤: %s', error)
            await self.send_music_message(guild_id, content='⚠️ 音訊播放中斷，將嘗試下一首。')
        await self.play_next(guild_id)

    async def play_next(self, guild_id):
        try:
            async with self.guild_lock(guild_id):
                await self._play_next_locked(guild_id)
        finally:
            self.update_idle(guild_id)

    async def _play_next_locked(self, guild_id):
        guild = self.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if not vc or not vc.is_connected():
            return
        if vc.is_playing() or vc.is_paused():
            self.preload_next(guild_id)
            return
        queue = self.queues.setdefault(guild_id, deque())
        generation = self.generations.get(guild_id, 0)
        while queue:
            track = queue.popleft()
            try:
                cached = await self.prepare_track(guild_id, track)
                if (generation != self.generations.get(guild_id, 0)
                        or guild.voice_client is not vc or not vc.is_connected()):
                    cached.cleanup()
                    return
                full_data = cached.info
                player = await asyncio.to_thread(LocalAudioSource, cached)
                if (generation != self.generations.get(guild_id, 0)
                        or guild.voice_client is not vc or not vc.is_connected()):
                    player.cleanup()
                    return
                token = object()

                def after_playing(error, token=token):
                    if self._loop and not self._loop.is_closed():
                        asyncio.run_coroutine_threadsafe(self.finish_track(guild_id, token, error), self._loop)

                self.playback_tokens[guild_id] = token
                try:
                    vc.play(player, after=after_playing)
                except Exception:
                    self.playback_tokens.pop(guild_id, None)
                    player.cleanup()
                    raise
                self.current_song[guild_id] = full_data
                self.preload_next(guild_id)
            except asyncio.CancelledError:
                if generation != self.generations.get(guild_id, 0):
                    return
                raise
            except Exception as exc:
                if generation != self.generations.get(guild_id, 0):
                    return
                self.current_song.pop(guild_id, None)
                logger.exception('播放失敗: %s', track.get('title'))
                reason = str(exc) if isinstance(exc, (ExtractionError, AudioPreparationError)) else '音訊播放器發生錯誤，請查看 bot 日誌。'
                await self.send_music_message(guild_id, content=f'⚠️ {reason} 已跳過此曲。')
                if generation != self.generations.get(guild_id, 0):
                    return
                continue
            await self.announce_track(guild_id, full_data)
            return
        self.current_song.pop(guild_id, None)

bot = MusicBot()


async def add_music(interaction, search, mode='play'):
    await interaction.response.defer()
    voice = getattr(interaction.user, 'voice', None)
    if not interaction.guild or not voice:
        return await interaction.followup.send('❌ 請先加入伺服器的語音頻道')
    guild_id = interaction.guild_id
    bot.pending_requests[guild_id] = bot.pending_requests.get(guild_id, 0) + 1
    bot.cancel_idle(guild_id)
    try:
        async with bot.connection_locks.setdefault(guild_id, asyncio.Lock()):
            vc = interaction.guild.voice_client
            if vc and vc.is_connected() and vc.channel != voice.channel:
                return await interaction.followup.send('❌ 請先加入與機器人相同的語音頻道')
            if vc and not vc.is_connected():
                await vc.disconnect(force=True)
                vc = None
            if vc is None:
                vc = await voice.channel.connect(timeout=30, self_deaf=True, cls=StableVoiceClient)
        bot.music_channels[guild_id] = interaction.channel
        generation = bot.generations.get(guild_id, 0)
        search = search.strip()
        if not search:
            raise ExtractionError('請輸入 YouTube 網址或歌曲名稱。')
        parsed = urlparse(search)
        is_url = parsed.scheme in ('http', 'https')
        if is_url and (parsed.hostname or '').lower() not in (
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be',
        ):
            raise ExtractionError('請使用 YouTube／YouTube Music 網址或歌曲名稱。')
        data = await asyncio.to_thread(get_info_via_cli, search, is_search=not is_url, flat=True)
        tracks = extract_tracks(data)
        if generation != bot.generations.get(guild_id, 0) or not vc.is_connected():
            return await interaction.followup.send('ℹ️ 播放狀態已變更，本次加入已取消。')
        queue = bot.queues.setdefault(guild_id, deque())
        if mode == 'play':
            queue.extend(tracks)
        else:
            queue.extendleft(reversed(tracks))
        message = f'**{tracks[0].get("title", "未知標題")}**' if len(tracks) == 1 else f'清單 **{len(tracks)}** 首（每次最多 100 首）'
        if mode == 'interrupt':
            bot.invalidate_playback(guild_id)
            vc.stop()
            await interaction.followup.send(f'🚨 立刻插播：{message}')
        elif mode == 'insert':
            await interaction.followup.send(f'📌 已插播：{message}（下一首播放）')
        else:
            await interaction.followup.send(f'✅ 已加入佇列：{message}')
        await bot.play_next(guild_id)
    except Exception as exc:
        logger.exception('加入歌曲失敗')
        reason = str(exc) if isinstance(exc, ExtractionError) else '無法加入語音或載入歌曲，請查看 bot 日誌及頻道權限。'
        await interaction.followup.send(f'⚠️ {reason}')
    finally:
        remaining = bot.pending_requests.get(guild_id, 1) - 1
        if remaining:
            bot.pending_requests[guild_id] = remaining
        else:
            bot.pending_requests.pop(guild_id, None)
        bot.update_idle(guild_id)


@bot.tree.command(name='play', description='播放 YouTube 音樂（網址／清單／搜尋）')
@app_commands.guild_only()
@app_commands.describe(search='YouTube／YouTube Music 網址或歌曲名稱')
async def play(interaction: discord.Interaction, search: str):
    await add_music(interaction, search)


@bot.tree.command(name='insert', description='插播（下一首播放，不打斷目前歌曲）')
@app_commands.guild_only()
@app_commands.describe(search='YouTube／YouTube Music 網址或歌曲名稱')
async def insert(interaction: discord.Interaction, search: str):
    await add_music(interaction, search, 'insert')


@bot.tree.command(name='interrupt', description='立刻插播（切掉目前歌曲）')
@app_commands.guild_only()
@app_commands.describe(search='YouTube／YouTube Music 網址或歌曲名稱')
async def interrupt(interaction: discord.Interaction, search: str):
    await add_music(interaction, search, 'interrupt')


@bot.tree.command(name='nowplaying', description='顯示播放資訊')
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    current = bot.current_song.get(interaction.guild_id)
    if current:
        token = bot.playback_tokens.get(interaction.guild_id)
        await interaction.response.send_message(embed=get_now_playing_embed(current), view=bot.get_control_view())
        bot.register_controls(interaction.guild_id, await interaction.original_response(), token)
    else:
        await interaction.response.send_message('❌ 沒在播歌', ephemeral=True)


@bot.tree.command(name='queue', description='顯示排隊清單')
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    queue = bot.queues.get(interaction.guild_id)
    if not queue:
        return await interaction.response.send_message('📭 佇列是空的')
    msg = [f'📜 **排隊清單（共 {len(queue)} 首）：**'] + [
        f'{i}. {track.get("title", "未知標題")[:150]}' for i, track in enumerate(list(queue)[:10], 1)
    ]
    if len(queue) > 10:
        msg.append(f'...還有 {len(queue) - 10} 首')
    await interaction.response.send_message('\n'.join(msg))


@bot.tree.command(name='skip', description='跳過目前歌曲')
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    await bot.handle_control_action(interaction, 'skip')


@bot.tree.command(name='shuffle', description='隨機排列等待中的歌曲，保留目前播放')
@app_commands.guild_only()
async def shuffle_cmd(interaction: discord.Interaction):
    await bot.handle_control_action(interaction, 'shuffle')


@bot.tree.command(name='remove', description='清空清單，保留目前播放')
@app_commands.guild_only()
async def remove(interaction: discord.Interaction):
    vc, guild_id = await ensure_voice(interaction)
    if not vc:
        return
    queue = bot.queues.setdefault(guild_id, deque())
    count = len(queue)
    queue.clear()
    bot.discard_preload(guild_id)
    bot.update_idle(guild_id)
    await interaction.response.send_message(f'🗑️ 已清空 **{count}** 首歌')


@bot.tree.command(name='stop', description='停止並清空')
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    await bot.handle_control_action(interaction, 'stop')


@bot.tree.command(name='pause', description='暫停')
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    await bot.handle_control_action(interaction, 'pause')


@bot.tree.command(name='resume', description='繼續')
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    await bot.handle_control_action(interaction, 'resume')


@bot.tree.command(name='leave', description='離開語音頻道')
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    vc, guild_id = await ensure_voice(interaction)
    if not vc:
        return
    await interaction.response.defer()
    bot.cleanup_guild_state(guild_id)
    await vc.disconnect()
    await interaction.followup.send('👋')


if __name__ == '__main__':
    token = os.getenv('BOT_TOKEN', '').strip()
    if not token or token in ('default', 'your_discord_bot_token'):
        raise RuntimeError('請在 .env 設定有效的 BOT_TOKEN。')
    bot.run(token)
