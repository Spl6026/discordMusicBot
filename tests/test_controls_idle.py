import asyncio
import unittest
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import main
from music_controls import PlaybackControls


class MusicFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = main.MusicBot(idle_timeout=300)
        self.channel = SimpleNamespace(id=10, members=[SimpleNamespace(bot=False)])
        self.vc = MagicMock()
        self.vc.channel = self.channel
        self.vc.is_connected.return_value = True
        self.vc.is_playing.return_value = False
        self.vc.is_paused.return_value = False
        self.vc.disconnect = AsyncMock()
        self.guild = SimpleNamespace(id=1, voice_client=self.vc)
        self.bot.get_guild = MagicMock(return_value=self.guild)
        self.bot.send_music_message = AsyncMock(return_value=None)
        self.expire = asyncio.Event()
        self.bot.wait_idle_timeout = AsyncMock(side_effect=self.expire.wait)

    async def asyncTearDown(self):
        await self.bot.close()

    def interaction(self, message_id=20, *, same_channel=True):
        return SimpleNamespace(
            guild_id=1, guild=self.guild,
            message=SimpleNamespace(id=message_id),
            user=SimpleNamespace(voice=SimpleNamespace(
                channel=self.channel if same_channel else object())),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

    def panel(self, message_id=20):
        message = SimpleNamespace(id=message_id, edit=AsyncMock())
        token = object()
        self.bot.playback_tokens[1] = token
        self.bot.register_controls(1, message, token)
        return message, token

    async def finish_idle(self):
        task = self.bot.idle_tasks[1]
        self.expire.set()
        await asyncio.wait_for(task, 1)

    async def test_four_persistent_buttons_route_to_shared_handlers(self):
        owner = SimpleNamespace(handle_control_action=AsyncMock(), authorize_controls=AsyncMock(return_value=True))
        view = PlaybackControls(owner)
        self.addCleanup(view.stop)
        self.assertTrue(view.is_persistent())
        self.assertEqual(len(view.to_components()[0]['components']), 4)
        interaction = self.interaction()
        self.assertTrue(await view.interaction_check(interaction))
        for button in view.children:
            await button.callback(interaction)
        self.assertEqual(owner.handle_control_action.await_args_list, [
            call(interaction, action, private=True)
            for action in ('toggle', 'skip', 'shuffle', 'stop')
        ])

    async def test_controls_require_same_voice_channel(self):
        self.panel()
        interaction = self.interaction(same_channel=False)
        self.assertFalse(await self.bot.authorize_controls(interaction))
        self.vc.pause.assert_not_called()
        self.assertTrue(interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_current_panel_allowed_but_old_message_and_token_rejected(self):
        self.panel()
        self.assertTrue(await self.bot.authorize_controls(self.interaction()))
        self.assertFalse(await self.bot.authorize_controls(self.interaction(19)))
        self.bot.playback_tokens[1] = object()
        self.assertFalse(await self.bot.authorize_controls(self.interaction()))

    async def test_old_panel_after_restart_rejected(self):
        self.assertFalse(await self.bot.authorize_controls(self.interaction()))

    async def test_replaced_and_late_panels_lose_buttons(self):
        old, token = self.panel()
        new = SimpleNamespace(id=21, edit=AsyncMock())
        self.bot.register_controls(1, new, token)
        late = SimpleNamespace(id=22, edit=AsyncMock())
        self.bot.register_controls(1, late, object())
        await asyncio.sleep(0)
        old.edit.assert_awaited_once_with(view=None)
        late.edit.assert_awaited_once_with(view=None)
        new.edit.assert_not_awaited()
        self.assertIs(self.bot.control_messages[1][0], new)

    async def test_track_finish_retires_controls(self):
        message, token = self.panel()
        self.bot.play_next = AsyncMock()
        await self.bot.finish_track(1, token, None)
        await asyncio.sleep(0)
        self.assertNotIn(1, self.bot.control_messages)
        message.edit.assert_awaited_once_with(view=None)
        self.bot.play_next.assert_awaited_once_with(1)

    async def test_toggle_pauses_and_resumes(self):
        self.vc.is_playing.return_value = True
        await self.bot.handle_control_action(self.interaction(), 'toggle', private=True)
        self.vc.pause.assert_called_once()
        self.vc.is_playing.return_value = False
        self.vc.is_paused.return_value = True
        await self.bot.handle_control_action(self.interaction(), 'toggle', private=True)
        self.vc.resume.assert_called_once()
        self.assertNotIn(1, self.bot.idle_tasks)

    async def test_skip_invalidates_old_player_and_starts_next_song(self):
        message, _ = self.panel()
        self.bot.play_next = AsyncMock()
        await self.bot.handle_control_action(self.interaction(), 'skip', private=True)
        self.vc.stop.assert_called_once()
        self.bot.play_next.assert_awaited_once_with(1)
        self.assertNotIn(1, self.bot.playback_tokens)
        await asyncio.sleep(0)
        message.edit.assert_awaited_once_with(view=None)

    async def test_stop_clears_queue_and_retires_panel(self):
        message, _ = self.panel()
        self.bot.queues[1] = deque([{'id': 'next'}])
        self.bot.current_song[1] = {'id': 'current'}
        await self.bot.handle_control_action(self.interaction(), 'stop', private=True)
        self.assertFalse(self.bot.queues[1])
        self.assertNotIn(1, self.bot.current_song)
        self.vc.stop.assert_called_once()
        await asyncio.sleep(0)
        message.edit.assert_awaited_once_with(view=None)
        self.assertIn(1, self.bot.idle_tasks)

    async def test_nowplaying_can_publish_fresh_controls(self):
        self.bot.current_song[1] = {'title': 'Test', 'id': 'test'}
        self.bot.playback_tokens[1] = token = object()
        interaction = self.interaction()
        message = SimpleNamespace(id=30, edit=AsyncMock())
        interaction.original_response = AsyncMock(return_value=message)
        with patch.object(main, 'bot', self.bot):
            await main.nowplaying.callback(interaction)
        self.assertEqual(self.bot.control_messages[1], (message, token))
        self.assertIs(interaction.response.send_message.call_args.kwargs['view'], self.bot.get_control_view())

    async def test_finished_queue_disconnects_and_cleans_state(self):
        self.panel()
        self.bot.update_idle(1)
        await self.finish_idle()
        self.vc.disconnect.assert_awaited_once()
        self.assertNotIn(1, self.bot.playback_tokens)
        self.assertNotIn(1, self.bot.idle_tasks)
        self.assertIn('清單已播完', self.bot.send_music_message.call_args.kwargs['content'])

    async def test_alone_disconnects_even_while_playing_and_clears_queue(self):
        self.channel.members = [SimpleNamespace(bot=True)]
        self.vc.is_playing.return_value = True
        self.bot.queues[1] = deque([{'id': 'next'}])
        self.bot.update_idle(1)
        await self.finish_idle()
        self.vc.disconnect.assert_awaited_once()
        self.vc.stop.assert_called_once()
        self.assertFalse(self.bot.queues[1])

    async def test_returning_listener_cancels_alone_countdown(self):
        self.channel.members = []
        self.vc.is_playing.return_value = True
        self.bot.update_idle(1)
        task = self.bot.idle_tasks[1]
        await asyncio.sleep(0)
        self.channel.members = [SimpleNamespace(bot=False)]
        member = SimpleNamespace(id=123, guild=self.guild)
        await self.bot.on_voice_state_update(member, SimpleNamespace(channel=None), SimpleNamespace(channel=self.channel))
        await asyncio.wait_for(task, 1)
        self.assertNotIn(1, self.bot.idle_tasks)
        self.vc.disconnect.assert_not_awaited()

    async def test_loading_new_request_cancels_finished_countdown(self):
        self.bot.update_idle(1)
        task = self.bot.idle_tasks[1]
        await asyncio.sleep(0)
        self.bot.pending_requests[1] = 1
        self.bot.update_idle(1)
        await asyncio.wait_for(task, 1)
        self.assertNotIn(1, self.bot.idle_tasks)
        self.vc.disconnect.assert_not_awaited()

    async def test_playing_paused_queued_and_loading_are_not_finished(self):
        for attr in ('is_playing', 'is_paused'):
            getattr(self.vc, attr).return_value = True
            self.assertIsNone(self.bot.idle_reason(1))
            getattr(self.vc, attr).return_value = False
        for mapping in (self.bot.queues, self.bot.preparations, self.bot.pending_requests):
            mapping[1] = True
            self.assertIsNone(self.bot.idle_reason(1))
            mapping.pop(1)
        async with self.bot.guild_lock(1):
            self.assertIsNone(self.bot.idle_reason(1))

    async def test_repeated_updates_do_not_reset_continuous_idle(self):
        self.bot.update_idle(1)
        task = self.bot.idle_tasks[1]
        self.bot.update_idle(1)
        self.assertIs(self.bot.idle_tasks[1], task)
        await self.finish_idle()
        self.vc.disconnect.assert_awaited_once()

    async def test_expiry_rechecks_state_before_disconnect(self):
        self.bot.update_idle(1)
        self.vc.is_playing.return_value = True
        await self.finish_idle()
        self.vc.disconnect.assert_not_awaited()

    async def test_expiry_does_not_disconnect_replaced_connection(self):
        self.bot.update_idle(1)
        self.guild.voice_client = MagicMock()
        await self.finish_idle()
        self.vc.disconnect.assert_not_awaited()
        self.guild.voice_client.disconnect.assert_not_called()

    async def test_manual_cleanup_cancels_countdown(self):
        self.bot.update_idle(1)
        task = self.bot.idle_tasks[1]
        await asyncio.sleep(0)
        self.bot.cleanup_guild_state(1)
        await asyncio.wait_for(task, 1)
        self.assertNotIn(1, self.bot.idle_tasks)
        self.vc.disconnect.assert_not_awaited()

    async def test_zero_disables_automatic_leave(self):
        self.bot.idle_timeout = 0
        self.bot.update_idle(1)
        self.assertNotIn(1, self.bot.idle_tasks)

    async def test_timeout_uses_raw_deadline_despite_early_async_wakeups(self):
        self.bot.idle_timeout = 10
        with patch('main.audio_time', side_effect=[100, 100, 103, 110]), patch('main.asyncio.sleep', new_callable=AsyncMock) as sleep:
            await main.MusicBot.wait_idle_timeout(self.bot)
        self.assertEqual(sleep.await_args_list, [call(5), call(5)])
