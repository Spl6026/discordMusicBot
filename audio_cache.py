"""Prepare complete audio files before handing them to the Discord player."""
import asyncio
import json
import logging
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

import discord

logger = logging.getLogger('MusicBot')


class AudioPreparationError(RuntimeError):
    pass


@dataclass
class CachedTrack:
    info: dict
    path: Path
    temporary: tempfile.TemporaryDirectory

    def cleanup(self):
        self.temporary.cleanup()


async def _terminate(process):
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.communicate(), timeout=5)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.communicate()


async def download_audio(url, base_command, *, timeout=300):
    """Download to an owned temporary directory; cancellation stops the downloader."""
    temporary = tempfile.TemporaryDirectory(prefix='ytbot-audio-')
    directory = Path(temporary.name)
    command = [*base_command,
               '--no-playlist', '--no-simulate', '-f', 'bestaudio/best',
               '--match-filters', '!is_live & !is_upcoming',
               '--max-filesize', '200M', '--http-chunk-size', '1M',
               '--retries', '5', '--fragment-retries', '5', '--abort-on-unavailable-fragments',
               '--print', 'after_move:%()j',
               '--output', str(directory / 'audio.%(ext)s'), '--', url]
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
        except asyncio.TimeoutError as exc:
            raise AudioPreparationError('歌曲下載逾時，請稍後重試。') from exc
        if process.returncode:
            detail = stderr.decode('utf-8', 'replace')[-3000:]
            logger.warning('音訊下載失敗: %s', detail)
            if 'sign in' in detail.lower() or 'not a bot' in detail.lower():
                raise AudioPreparationError('YouTube 要求登入驗證，請檢查伺服器網路或 cookies 設定。')
            raise AudioPreparationError('歌曲無法完整下載，請查看 bot 日誌。')
        lines = [line for line in stdout.decode('utf-8').splitlines() if line.strip()]
        if not lines:
            raise AudioPreparationError('這首歌無法完整快取；直播、尚未開始的影片及超過 200 MiB 的音訊暫不支援。')
        info = json.loads(lines[-1])
        requested = info.get('requested_downloads') or []
        filename = (requested[0].get('filepath') if requested else None) or info.get('_filename')
        if not filename:
            raise AudioPreparationError('下載完成但未取得音訊檔案。')
        audio_path = Path(filename).resolve()
        if not audio_path.is_relative_to(directory.resolve()) or not audio_path.is_file() or not audio_path.stat().st_size:
            raise AudioPreparationError('下載的音訊檔案無效。')
        logger.info('歌曲已完整快取: %s (%d bytes)', info.get('id'), audio_path.stat().st_size)
        return CachedTrack(info=info, path=audio_path, temporary=temporary)
    except BaseException:
        if process is not None:
            await _terminate(process)
        temporary.cleanup()
        raise


class LocalAudioSource(discord.PCMVolumeTransformer):
    """Own both FFmpeg and its local file. No network reads or inserted silence."""
    def __init__(self, cached, *, volume=0.5):
        self.cached = cached
        self._cleanup_lock = threading.Lock()
        self._cleaned = False
        self._first_frame = None
        try:
            source = discord.FFmpegPCMAudio(
                str(cached.path), before_options='-nostdin -protocol_whitelist file,pipe', options='-vn',
            )
            super().__init__(source, volume=volume)
            # Initialise the decoder before Discord starts its playback clock.
            self._first_frame = super().read()
            if not self._first_frame:
                raise AudioPreparationError('下載的檔案沒有可解碼的音訊。')
        except BaseException:
            self.cleanup()
            raise

    def read(self):
        if self._first_frame is not None:
            frame, self._first_frame = self._first_frame, None
            return frame
        return super().read()

    @property
    def _current_error(self):
        return getattr(getattr(self, 'original', None), '_current_error', None)

    def cleanup(self):
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._cleaned = True
            try:
                original = getattr(self, 'original', None)
                if original is not None:
                    stdout = getattr(original, '_stdout', None)
                    try:
                        original.cleanup()
                    finally:
                        if stdout is not None:
                            stdout.close()
            finally:
                self.cached.cleanup()
