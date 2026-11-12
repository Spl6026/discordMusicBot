import math
import struct
import unittest
from unittest.mock import MagicMock

import discord

from voice_timing import StableAudioPlayer


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class EarlyTimer:
    def __init__(self, clock, rate):
        self.clock = clock
        self.rate = rate
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, duration):
        self.clock.now = max(self.clock.now + duration / self.rate, math.nextafter(self.clock.now, math.inf))
        return self.stopped


class Frames(discord.AudioSource):
    def __init__(self, count, before_read=None):
        self.index = 0
        self.count = count
        self.before_read = before_read
        self.cleaned = False

    def read(self):
        if self.before_read:
            self.before_read(self.index)
        if self.index == self.count:
            return b''
        result = struct.pack('<I', self.index) + bytes(3836)
        self.index += 1
        return result

    def cleanup(self):
        self.cleaned = True


class TimingTests(unittest.TestCase):
    def player(self, frames=500, rate=1.07, before_read=None):
        clock = FakeClock()
        source = Frames(frames, before_read)
        sent = []
        client = MagicMock()
        client.is_connected.return_value = True
        client.send_audio_packet.side_effect = lambda data, *, encode: sent.append((clock(), data)) if encode else None
        player = StableAudioPlayer(source, client, clock=clock)
        player._end = EarlyTimer(clock, rate)
        player._audio_sleep = player._end.wait
        player._speak = MagicMock()
        return player, clock, source, sent

    def test_fast_guest_timers_still_send_fifty_frames_per_real_second(self):
        for rate in (1, 1.07, 1.2):
            with self.subTest(rate=rate):
                player, clock, source, sent = self.player(frames=3000, rate=rate)
                player.run()
                self.assertEqual(len(sent), 3000)
                self.assertAlmostEqual(sent[-1][0] - sent[0][0] + 0.02, 60, places=7)
                self.assertTrue(source.cleaned)

    def test_source_stall_rebases_timing_without_skipping_or_bursting(self):
        player, clock, source, sent = self.player(frames=100)
        source.before_read = lambda index: setattr(clock, 'now', clock.now + 2) if index == 40 else None
        player.run()
        self.assertEqual([struct.unpack('<I', data[:4])[0] for _, data in sent], list(range(100)))
        self.assertEqual(player.timing_rebases, 1)
        self.assertGreaterEqual(min(b[0] - a[0] for a, b in zip(sent, sent[1:])), 0.015 - 1e-9)

    def test_pause_and_resume_do_not_catch_up_or_skip_audio(self):
        player, clock, source, sent = self.player(frames=100)

        def pause(index):
            if index == 40:
                player.pause(update_speaking=False)
                clock.now += 10
                player.resume(update_speaking=False)

        source.before_read = pause
        player.run()
        self.assertEqual(len(sent), 100)
        self.assertGreaterEqual(sent[40][0] - sent[39][0], 10)
        self.assertGreaterEqual(min(b[0] - a[0] for a, b in zip(sent, sent[1:])), 0.015 - 1e-9)

    def test_stop_interrupts_deadline_wait_and_cleans_source(self):
        player, clock, source, sent = self.player()
        def stop_during_wait(duration):
            player._end.set()
            return True
        player._end.wait = stop_during_wait
        player._audio_sleep = stop_during_wait
        player.run()
        self.assertEqual(len(sent), 1)
        self.assertTrue(source.cleaned)

    def test_decoder_error_reaches_after_callback(self):
        player, clock, source, sent = self.player()
        error = RuntimeError('decoder failed')
        source.read = MagicMock(side_effect=error)
        player.after = MagicMock()
        player.run()
        player.after.assert_called_once_with(error)
        self.assertTrue(source.cleaned)
