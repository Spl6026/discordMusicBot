"""Pace audio against an unadjusted clock, independently of Linux NTP tick rate."""
import logging
import threading
import time

import discord
from discord.enums import SpeakingState
from discord.opus import Encoder
from discord.player import AudioPlayer

logger = logging.getLogger('MusicBot')


def audio_time():
    if hasattr(time, 'CLOCK_MONOTONIC_RAW'):
        return time.clock_gettime(time.CLOCK_MONOTONIC_RAW)
    # Windows perf_counter uses QueryPerformanceCounter rather than guest time.
    return time.perf_counter()


class StableAudioPlayer(AudioPlayer):
    """Keep 20 ms frames on the raw clock and discard timing debt, never audio."""

    def __init__(self, source, client, *, after=None, clock=audio_time, sleep=time.sleep):
        super().__init__(source, client, after=after)
        self._audio_clock = clock
        self._audio_sleep = sleep
        self._reset_timing = threading.Event()
        self.packets_sent = 0
        self.timing_rebases = 0

    def resume(self, *, update_speaking=True):
        self._reset_timing.set()
        super().resume(update_speaking=update_speaking)

    def _wait_until(self, deadline):
        while not self._end.is_set():
            remaining = deadline - self._audio_clock()
            if remaining <= 0:
                return True
            # This Conda Python uses sem_timedwait(CLOCK_REALTIME) for Event.wait;
            # guest wall-clock steps can prolong even a 20 ms event wait by seconds.
            # time.sleep uses clock_nanosleep(CLOCK_MONOTONIC). Its rate can still
            # be wrong, so always recheck the RAW deadline after each short sleep.
            self._audio_sleep(min(remaining, 0.01))
        return False

    def _do_run(self):
        self.loops = 0
        self._start = self._audio_clock()
        deadline = self._start
        last_packet = None
        started = self._start
        client = self.client
        self._speak(SpeakingState.voice)
        try:
            while not self._end.is_set():
                if not self._resumed.is_set():
                    self.send_silence()
                    self._resumed.wait()
                    self._reset_timing.set()
                    continue
                data = self.source.read()
                if not data:
                    self._current_error = self._current_error or getattr(self.source, '_current_error', None)
                    self.stop()
                    break
                # A pause may arrive while decoding. Retain this frame until
                # resume instead of consuming or dropping any part of the song.
                if not self._resumed.is_set():
                    self.send_silence()
                    self._resumed.wait()
                    self._reset_timing.set()
                if self._end.is_set():
                    break
                if not client.is_connected():
                    connected = client.wait_until_connected(client.timeout)
                    if self._end.is_set() or not connected:
                        return
                    self._speak(SpeakingState.voice)
                    self._reset_timing.set()
                if self._reset_timing.is_set():
                    self._reset_timing.clear()
                    deadline = self._audio_clock()
                    last_packet = None
                now = self._audio_clock()
                if now - deadline > self.DELAY:
                    deadline = now
                    self.timing_rebases += 1
                if last_packet is not None:
                    deadline = max(deadline, last_packet + self.DELAY * 0.75)
                if not self._wait_until(deadline):
                    break
                if not self._resumed.is_set():
                    self.send_silence()
                    self._resumed.wait()
                    if self._end.is_set():
                        break
                    deadline = self._audio_clock()
                    self._reset_timing.clear()
                client.send_audio_packet(data, encode=not self.source.is_opus())
                last_packet = self._audio_clock()
                self.loops += 1
                self.packets_sent += 1
                deadline += self.DELAY
            if client.is_connected():
                self.send_silence()
        finally:
            logger.info('播放計時: clock=%s packets=%d elapsed=%.3fs rebases=%d',
                        'MONOTONIC_RAW' if hasattr(time, 'CLOCK_MONOTONIC_RAW') else 'perf_counter',
                        self.packets_sent, self._audio_clock() - started, self.timing_rebases)


class StableVoiceClient(discord.VoiceClient):
    def play(self, source, *, after=None, **encoder_options):
        if not self.is_connected():
            raise discord.ClientException('Not connected to voice.')
        if self.is_playing() or self.is_paused():
            raise discord.ClientException('Already playing audio.')
        if not isinstance(source, discord.AudioSource):
            raise TypeError('source must be an AudioSource')
        if not source.is_opus():
            self.encoder = Encoder(**encoder_options)
        self._player = StableAudioPlayer(source, self, after=after)
        self._player.start()
