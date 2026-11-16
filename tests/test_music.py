import asyncio
import json
import subprocess
import threading
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import main
import youtube


class ExtractionTests(unittest.TestCase):
    def test_complete_playlist_and_unavailable_entries(self):
        tracks = main.extract_tracks({'entries': [{'id': 'one'}, None, {'id': 'two'}]})
        self.assertEqual([t['id'] for t in tracks], ['one', 'two'])
        with self.assertRaises(main.ExtractionError):
            main.extract_tracks({'entries': []})

    @patch('youtube.subprocess.run')
    def test_cli_uses_current_python_and_does_not_force_web(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({'id': 'one'}))
        with patch.dict(youtube.os.environ, {'YTDLP_PLAYER_CLIENT': '', 'YTDLP_COOKIES_FILE': ''}):
            main.get_info_via_cli('-hello', is_search=True, flat=True)
        args = run.call_args.args[0]
        self.assertEqual(args[:3], [youtube.sys.executable, '-m', 'yt_dlp'])
        self.assertEqual(args[-2:], ['--', 'ytsearch1:-hello'])
        self.assertNotIn('youtube:player_client=web', args)
        self.assertIn('--dump-single-json', args)

    @patch('youtube.subprocess.run', side_effect=subprocess.TimeoutExpired('yt-dlp', 90))
    def test_timeout_has_actionable_error(self, run):
        with self.assertRaisesRegex(main.ExtractionError, '逾時'):
            main.get_info_via_cli('https://youtu.be/test')

    @patch('youtube.subprocess.run')
    def test_stream_resolution_checks_format_and_required_wait(self, run):
        run.return_value = SimpleNamespace(returncode=0, stdout=json.dumps({'id': 'one', 'url': 'audio'}))
        with patch.dict(youtube.os.environ, {'YTDLP_COOKIES_FILE': ''}):
            main.get_info_via_cli('https://youtu.be/one')
        self.assertIn('--check-formats', run.call_args.args[0])

class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = main.MusicBot()
        self.vc = MagicMock()
        self.vc.is_connected.return_value = True
        self.vc.is_playing.return_value = False
        self.vc.is_paused.return_value = False
        self.guild = SimpleNamespace(voice_client=self.vc)
        self.bot.get_guild = MagicMock(return_value=self.guild)
        self.bot.send_music_message = AsyncMock()
        self.bot.queues[1] = deque([{'id': 'one'}, {'id': 'two'}])
        self.download = self.enterContext(patch('main.download_audio', new_callable=AsyncMock))
        self.download.side_effect = self.fake_download
        self.local_source = self.enterContext(patch('main.LocalAudioSource'))
        self.vc.play.side_effect = lambda *a, **kw: setattr(self.vc.is_playing, 'return_value', True)
        self.downloads = []

    async def fake_download(self, url, command):
        cached = MagicMock()
        cached.info = {'id': url.rsplit('=', 1)[-1]}
        self.downloads.append(cached)
        return cached

    async def asyncTearDown(self):
        await self.bot.close()

    async def test_stop_while_downloading_never_starts_old_track(self):
        started = asyncio.Event()

        async def resolve(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.download.side_effect = resolve
        task = asyncio.create_task(self.bot.play_next(1))
        await asyncio.wait_for(started.wait(), 1)
        self.bot.cleanup_guild_state(1)
        await asyncio.wait_for(task, 1)
        self.vc.play.assert_not_called()
        self.assertFalse(self.bot.queues[1])

    async def test_failed_song_advances_to_next(self):
        cached = MagicMock(info={'id': 'two'})
        self.download.side_effect = [main.AudioPreparationError('unavailable'), cached]
        with self.assertLogs('MusicBot', level='ERROR'):
            await self.bot.play_next(1)
        self.vc.play.assert_called_once()
        self.assertEqual(self.bot.current_song[1]['id'], 'two')
        self.assertFalse(self.bot.queues[1])

    async def test_missing_message_permission_does_not_skip_playing_song(self):
        channel = SimpleNamespace(send=AsyncMock(side_effect=main.discord.Forbidden(
            SimpleNamespace(status=403, reason='Forbidden'), 'Missing permissions',
        )))
        self.bot.music_channels[1] = channel
        self.bot.send_music_message = main.MusicBot.send_music_message.__get__(self.bot)
        with self.assertLogs('MusicBot', level='WARNING'):
            await self.bot.play_next(1)
        self.vc.play.assert_called_once()
        self.assertEqual(self.bot.current_song[1]['id'], 'one')
        self.assertEqual(len(self.bot.queues[1]), 1)

    async def test_simultaneous_play_requests_only_start_one_song(self):
        await asyncio.gather(self.bot.play_next(1), self.bot.play_next(1))
        self.vc.play.assert_called_once()
        self.assertEqual(len(self.bot.queues[1]), 1)

    async def test_preloaded_next_song_is_reused_once(self):
        await self.bot.play_next(1)
        await self.bot.preloads[1][1]
        self.assertEqual(self.download.await_count, 2)
        self.vc.is_playing.return_value = False
        await self.bot.finish_track(1, self.bot.playback_tokens[1], None)
        self.assertEqual(self.download.await_count, 2)
        self.assertEqual(self.bot.current_song[1]['id'], 'two')
        self.assertEqual(self.local_source.call_args.args[0], self.downloads[1])

    async def test_inserting_song_discards_old_preload_and_preserves_queue(self):
        await self.bot.play_next(1)
        await self.bot.preloads[1][1]
        old_preload = self.downloads[1]
        self.bot.queues[1].appendleft({'id': 'inserted'})
        await self.bot.play_next(1)
        await self.bot.preloads[1][1]
        old_preload.cleanup.assert_called_once()
        self.assertEqual(self.bot.preloads[1][0]['id'], 'inserted')
        self.assertEqual([t['id'] for t in self.bot.queues[1]], ['inserted', 'two'])

    async def test_stop_releases_completed_preload(self):
        await self.bot.play_next(1)
        await self.bot.preloads[1][1]
        cached = self.downloads[1]
        self.bot.cleanup_guild_state(1)
        cached.cleanup.assert_called_once()
        self.assertNotIn(1, self.bot.preloads)

    async def test_shuffle_preserves_playing_song_duplicates_and_queue_reference(self):
        self.bot.queues[1].extend([{'id': 'three'}, {'id': 'two'}])
        await self.bot.play_next(1)
        await self.bot.preloads[1][1]
        queue = self.bot.queues[1]
        waiting = list(queue)
        song = self.bot.current_song[1]
        token = self.bot.playback_tokens[1]
        old_cached = self.downloads[1]
        with patch('main.random.shuffle', side_effect=lambda tracks: tracks.reverse()):
            self.assertEqual(self.bot.shuffle_queue(1), 3)
        await self.bot.preloads[1][1]
        self.assertIs(self.bot.queues[1], queue)
        self.assertEqual([id(track) for track in queue], [id(track) for track in reversed(waiting)])
        self.assertIs(self.bot.current_song[1], song)
        self.assertIs(self.bot.playback_tokens[1], token)
        self.vc.play.assert_called_once()
        old_cached.cleanup.assert_called_once()
        self.assertIs(self.bot.preloads[1][0], waiting[-1])

    async def test_shuffle_reuses_preload_when_next_song_stays_first(self):
        self.bot.queues[1].extend([{'id': 'three'}, {'id': 'four'}])
        await self.bot.play_next(1)
        await self.bot.preloads[1][1]
        original_task = self.bot.preloads[1][1]
        cached = self.downloads[1]
        def shuffle_tail(tracks):
            tracks[1:] = reversed(tracks[1:])
        with patch('main.random.shuffle', side_effect=shuffle_tail):
            self.bot.shuffle_queue(1)
        self.assertIs(self.bot.preloads[1][1], original_task)
        cached.cleanup.assert_not_called()
        self.assertEqual([track['id'] for track in self.bot.queues[1]], ['two', 'four', 'three'])

    async def test_skip_during_download_can_play_the_next_song(self):
        started = asyncio.Event()

        async def resolve(url, command):
            if url.endswith('one'):
                started.set()
                await asyncio.Event().wait()
            return await self.fake_download(url, command)

        self.download.side_effect = resolve
        playing = asyncio.create_task(self.bot.play_next(1))
        await asyncio.wait_for(started.wait(), 1)
        self.bot.invalidate_playback(1)
        next_song = asyncio.create_task(self.bot.play_next(1))
        await asyncio.wait_for(asyncio.gather(playing, next_song), 1)
        self.vc.play.assert_called_once()
        self.assertEqual(self.bot.current_song[1]['id'], 'two')

    async def test_stop_while_decoder_is_starting_cleans_source(self):
        started, release = threading.Event(), threading.Event()
        source = MagicMock()

        def construct(cached):
            started.set()
            release.wait(2)
            return source

        self.local_source.side_effect = construct
        playing = asyncio.create_task(self.bot.play_next(1))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            self.bot.cleanup_guild_state(1)
        finally:
            release.set()
            await asyncio.wait_for(playing, 1)
        self.vc.play.assert_not_called()
        source.cleanup.assert_called_once()

    async def test_stale_audio_callback_does_not_consume_queue(self):
        old_token = object()
        self.bot.playback_tokens[1] = old_token
        self.bot.invalidate_playback(1)
        self.bot.play_next = AsyncMock()
        await self.bot.finish_track(1, old_token, None)
        self.bot.play_next.assert_not_awaited()

    async def test_temporary_voice_leave_preserves_queue_and_playback(self):
        self.bot._connection.user = SimpleNamespace(id=99)
        token = self.bot.playback_tokens[1] = object()
        self.bot.play_next = AsyncMock()
        member = SimpleNamespace(id=99, guild=SimpleNamespace(id=1))
        with patch('main.asyncio.sleep', new_callable=AsyncMock):
            await self.bot.on_voice_state_update(member, SimpleNamespace(channel=object()), SimpleNamespace(channel=None))
        self.assertEqual(len(self.bot.queues[1]), 2)
        self.assertIs(self.bot.playback_tokens[1], token)

    async def test_reconnect_preserves_callback_and_advances_to_next_track(self):
        self.bot._connection.user = SimpleNamespace(id=99)
        await self.bot.play_next(1)
        token = self.bot.playback_tokens[1]
        self.vc.is_connected.return_value = False
        member = SimpleNamespace(id=99, guild=SimpleNamespace(id=1))

        async def reconnect(_):
            self.vc.is_connected.return_value = True

        with patch('main.asyncio.sleep', side_effect=reconnect):
            await self.bot.on_voice_state_update(member, SimpleNamespace(channel=object()), SimpleNamespace(channel=None))
        self.assertIs(self.bot.playback_tokens[1], token)
        self.vc.is_playing.return_value = False
        await self.bot.finish_track(1, token, None)
        self.assertEqual(self.bot.current_song[1]['id'], 'two')
        self.assertEqual(self.vc.play.call_count, 2)

    async def test_reconnect_restarts_queue_when_previous_player_already_finished(self):
        self.bot._connection.user = SimpleNamespace(id=99)
        self.vc.is_connected.return_value = False
        member = SimpleNamespace(id=99, guild=SimpleNamespace(id=1))

        async def reconnect(_):
            self.vc.is_connected.return_value = True

        with patch('main.asyncio.sleep', side_effect=reconnect):
            await self.bot.on_voice_state_update(member, SimpleNamespace(channel=object()), SimpleNamespace(channel=None))
        self.vc.play.assert_called_once()
        self.assertEqual(self.bot.current_song[1]['id'], 'one')

    async def test_permanent_voice_leave_cleans_state_after_grace_period(self):
        self.bot._connection.user = SimpleNamespace(id=99)
        self.vc.is_connected.return_value = False
        member = SimpleNamespace(id=99, guild=SimpleNamespace(id=1))
        with patch('main.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await self.bot.on_voice_state_update(member, SimpleNamespace(channel=object()), SimpleNamespace(channel=None))
        self.assertEqual(sleep.await_count, 45)
        sleep.assert_awaited_with(1)
        self.assertFalse(self.bot.queues[1])


if __name__ == '__main__':
    unittest.main()
