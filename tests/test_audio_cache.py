import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from audio_cache import AudioPreparationError, CachedTrack, LocalAudioSource, download_audio


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def fake_process(self, *command, **kwargs):
        self.directory = Path(command[command.index('--output') + 1]).parent
        self.audio = self.directory / 'audio.webm'
        self.audio.write_bytes(b'complete audio')
        info = {'id': 'one', 'requested_downloads': [{'filepath': str(self.audio)}]}
        self.process = SimpleNamespace(
            returncode=0, communicate=AsyncMock(return_value=(json.dumps(info).encode(), b'')),
            terminate=MagicMock(), kill=MagicMock(),
        )
        return self.process

    async def test_complete_file_is_returned_and_owned(self):
        with patch('audio_cache.asyncio.create_subprocess_exec', side_effect=self.fake_process):
            cached = await download_audio('https://youtu.be/one', ['yt-dlp'])
        self.assertEqual(cached.path.read_bytes(), b'complete audio')
        self.assertTrue(cached.path.is_relative_to(Path(cached.temporary.name)))
        cached.cleanup()
        self.assertFalse(self.directory.exists())

    async def test_failed_download_removes_partial_file(self):
        async def fail(*command, **kwargs):
            process = await self.fake_process(*command, **kwargs)
            process.returncode = 1
            process.communicate.return_value = (b'', b'HTTP error 403')
            return process
        with patch('audio_cache.asyncio.create_subprocess_exec', side_effect=fail), self.assertLogs('MusicBot', level='WARNING'):
            with self.assertRaises(AudioPreparationError):
                await download_audio('https://youtu.be/one', ['yt-dlp'])
        self.assertFalse(self.directory.exists())

    async def test_live_or_oversize_rejection_never_reaches_playback(self):
        async def reject(*command, **kwargs):
            process = await self.fake_process(*command, **kwargs)
            process.communicate.return_value = (b'', b'')
            return process
        with patch('audio_cache.asyncio.create_subprocess_exec', side_effect=reject):
            with self.assertRaisesRegex(AudioPreparationError, '直播'):
                await download_audio('https://youtu.be/one', ['yt-dlp'])
        self.assertFalse(self.directory.exists())

    async def test_cancellation_terminates_downloader_and_cleans_partial_file(self):
        started, terminated = asyncio.Event(), asyncio.Event()

        async def start(*command, **kwargs):
            process = await self.fake_process(*command, **kwargs)
            process.returncode = None

            async def communicate():
                started.set()
                await terminated.wait()
                process.returncode = -15
                return b'', b''
            process.communicate = communicate
            process.terminate.side_effect = terminated.set
            return process

        with patch('audio_cache.asyncio.create_subprocess_exec', side_effect=start):
            task = asyncio.create_task(download_audio('https://youtu.be/one', ['yt-dlp']))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.process.terminate.assert_called_once()
        self.assertFalse(self.directory.exists())

    async def test_timeout_terminates_downloader_and_cleans_partial_file(self):
        terminated = asyncio.Event()

        async def start(*command, **kwargs):
            process = await self.fake_process(*command, **kwargs)
            process.returncode = None

            async def communicate():
                await terminated.wait()
                process.returncode = -15
                return b'', b''
            process.communicate = communicate
            process.terminate.side_effect = terminated.set
            return process

        with patch('audio_cache.asyncio.create_subprocess_exec', side_effect=start):
            with self.assertRaisesRegex(AudioPreparationError, '逾時'):
                await download_audio('https://youtu.be/one', ['yt-dlp'], timeout=0.01)
        self.process.terminate.assert_called_once()
        self.assertFalse(self.directory.exists())


class LocalPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='ytbot-test-')
        self.path = Path(self.temporary.name) / 'audio.webm'
        self.path.write_bytes(b'audio')
        self.cached = CachedTrack({'id': 'one'}, self.path, self.temporary)
        self.addCleanup(self.cached.cleanup)

    def test_source_uses_local_file_without_network_options_and_preserves_samples(self):
        original = MagicMock(spec=discord.AudioSource)
        original.is_opus.return_value = False
        original.read.side_effect = [b'\x10\x00' * 1920, b'']
        original._current_error = None
        with patch('audio_cache.discord.FFmpegPCMAudio', return_value=original) as ffmpeg:
            source = LocalAudioSource(self.cached)
        self.assertEqual(ffmpeg.call_args.args, (str(self.path),))
        self.assertEqual(ffmpeg.call_args.kwargs, {
            'before_options': '-nostdin -protocol_whitelist file,pipe', 'options': '-vn',
        })
        self.assertEqual(source.read(), b'\x08\x00' * 1920)
        self.assertEqual(source.read(), b'')
        source.cleanup()
        source.cleanup()
        original.cleanup.assert_called_once()
        self.assertFalse(self.path.exists())

    def test_ffmpeg_failure_is_exposed_and_cached_file_is_cleaned(self):
        with patch('audio_cache.discord.FFmpegPCMAudio', side_effect=RuntimeError('ffmpeg failed')):
            with self.assertRaisesRegex(RuntimeError, 'ffmpeg failed'):
                LocalAudioSource(self.cached)
        self.assertFalse(self.path.exists())

    def test_undecodable_file_is_rejected_before_playback(self):
        original = MagicMock(spec=discord.AudioSource)
        original.is_opus.return_value = False
        original.read.return_value = b''
        with patch('audio_cache.discord.FFmpegPCMAudio', return_value=original):
            with self.assertRaisesRegex(AudioPreparationError, '沒有可解碼'):
                LocalAudioSource(self.cached)
        original.cleanup.assert_called_once()
        self.assertFalse(self.path.exists())
