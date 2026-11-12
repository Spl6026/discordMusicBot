"""Measure local playback for the download integration test."""
import json
import statistics

from discord.opus import Encoder

from audio_cache import LocalAudioSource
from voice_timing import StableAudioPlayer, audio_time


class MeasuringVoice:
    timeout = 5

    def __init__(self):
        self.times = []
        self.zero_frames = 0
        self.encoder = Encoder()

    def is_connected(self):
        return True

    def send_audio_packet(self, data, *, encode=True):
        if encode:
            # Exercise the real Opus encoder, but discard every packet locally.
            self.encoder.encode(data, 960)
            self.times.append(audio_time())
            self.zero_frames += not any(data)


def measure(cached):
    voice = MeasuringVoice()
    errors = []

    def after(error):
        if error:
            errors.append(str(error))

    source = LocalAudioSource(cached)
    player = StableAudioPlayer(source, voice, after=after)
    player._speak = lambda state: None  # No Discord websocket is created.
    started = audio_time()
    player.start()
    last_report = started
    try:
        while player.is_alive():
            player.join(timeout=1)
            now = audio_time()
            if now - last_report >= 30:
                print(f'Playing locally: {len(voice.times) * 0.02:.1f}s of audio', flush=True)
                last_report = now
            if now - started > (cached.info.get('duration') or 1800) + 60:
                raise RuntimeError('Playback exceeded its expected duration')
    finally:
        player.stop()
        player.join(timeout=5)
        source.cleanup()
    if errors:
        raise RuntimeError('; '.join(errors))
    if not voice.times:
        raise RuntimeError('No audio frames were decoded')
    gaps = [b - a for a, b in zip(voice.times, voice.times[1:])]
    rapid_run = longest_run = 0
    for gap in gaps:
        rapid_run = rapid_run + 1 if gap < 0.005 else 0
        longest_run = max(longest_run, rapid_run)
    result = {
        'frames': len(voice.times), 'audio_seconds': round(len(voice.times) * 0.02, 3),
        'wall_seconds': round(voice.times[-1] - voice.times[0] + 0.02, 3),
        'median_interval_ms': round(statistics.median(gaps) * 1000, 3) if gaps else None,
        'max_interval_ms': round(max(gaps) * 1000, 3) if gaps else None,
        'longest_run_of_intervals_under_5ms': longest_run,
        'zero_pcm_frames': voice.zero_frames,
        'temporary_file_cleaned': not cached.path.exists(),
    }
    expected_duration = cached.info.get('duration')
    if expected_duration:
        result['expected_audio_seconds'] = expected_duration
    print(json.dumps(result, ensure_ascii=False), flush=True)
    # Scheduling jitter is possible on a shared host, but a sustained burst or
    # duration mismatch signals the original catch-up problem.
    if longest_run >= 10 or abs(result['audio_seconds'] - result['wall_seconds']) > 0.2:
        raise RuntimeError('Playback timing did not remain stable')
    if expected_duration and abs(result['audio_seconds'] - expected_duration) > 1.5:
        raise RuntimeError('Decoded audio duration does not match the complete song')
    return result


