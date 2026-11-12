"""Local-only integration: HTTP truncation, yt-dlp resume, then network-free playback."""
import asyncio
import hashlib
import http.server
import io
import math
import socket
import struct
import sys
import threading
import unittest
import wave

from audio_cache import download_audio
from playback_helpers import measure


def make_wav(seconds=3):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
        samples = (struct.pack('<hh', sample, sample) for i in range(48000 * seconds)
                   for sample in [int(10000 * math.sin(2 * math.pi * 440 * i / 48000))])
        audio.writeframes(b''.join(samples))
    return output.getvalue()


class InterruptedHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_disconnect_during_download_cannot_interrupt_local_playback(self):
        content = make_wav()
        status = {'interrupted': False, 'range_requests': 0, 'get_requests': 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self.respond(head=True)

            def do_GET(self):
                status['get_requests'] += 1
                self.respond()

            def respond(self, head=False):
                requested = self.headers.get('Range', '')
                offset, end = 0, len(content) - 1
                if requested.startswith('bytes='):
                    status['range_requests'] += 1
                    start_text, end_text = requested[6:].split('-', 1)
                    offset = int(start_text or 0)
                    if end_text:
                        end = min(end, int(end_text))
                self.send_response(206 if requested else 200)
                self.send_header('Content-Type', 'audio/wav')
                self.send_header('Content-Length', str(end - offset + 1))
                self.send_header('Accept-Ranges', 'bytes')
                if requested:
                    self.send_header('Content-Range', f'bytes {offset}-{end}/{len(content)}')
                self.end_headers()
                if head:
                    return
                body = content[offset:end + 1]
                # Probe requests may be partial. Interrupt the actual full download.
                if not status['interrupted'] and len(body) > 128 * 1024:
                    status['interrupted'] = True
                    self.wfile.write(body[:128 * 1024])
                    self.wfile.flush()
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    return
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cached = None
        try:
            cached = await download_audio(
                f'http://127.0.0.1:{server.server_port}/song.wav',
                [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--quiet', '--no-progress'],
            )
            self.assertTrue(status['interrupted'])
            self.assertGreater(status['range_requests'], 0)
            self.assertEqual(hashlib.sha256(cached.path.read_bytes()).digest(), hashlib.sha256(content).digest())
        finally:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
            thread.join(timeout=2)
        # The server is gone. Playback must still consume every real audio frame.
        try:
            result = await asyncio.to_thread(measure, cached)
            self.assertAlmostEqual(result['audio_seconds'], 3, delta=0.02)
            self.assertEqual(result['zero_pcm_frames'], 0)
            self.assertEqual(result['longest_run_of_intervals_under_5ms'], 0)
            self.assertTrue(result['temporary_file_cleaned'])
        finally:
            if cached:
                cached.cleanup()
