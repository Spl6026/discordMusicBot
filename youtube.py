"""YouTube metadata lookup and yt-dlp configuration."""
import json
import logging
import os
import subprocess
import sys

logger = logging.getLogger("MusicBot")

class ExtractionError(RuntimeError):
    pass


def ytdlp_command():
    """Use the same YouTube authentication/runtime settings for lookup and downloads."""
    cmd = [
        sys.executable, '-m', 'yt_dlp', '--ignore-config',
        '--quiet', '--no-progress', '--no-colors',
        '--socket-timeout', '15', '--retries', '2', '--extractor-retries', '2',
        '--no-js-runtimes', '--js-runtimes', os.getenv('YTDLP_JS_RUNTIME') or 'deno',
    ]
    provider = os.getenv('BGUTIL_PROVIDER_URL', 'http://127.0.0.1:4416').strip()
    if provider:
        cmd.extend(['--extractor-args', f'youtubepot-bgutilhttp:base_url={provider}'])
    clients = os.getenv('YTDLP_PLAYER_CLIENT', '').strip()
    if clients:
        cmd.extend(['--extractor-args', f'youtube:player_client={clients}'])
    cookies = os.getenv('YTDLP_COOKIES_FILE', '').strip()
    if cookies:
        if not os.path.isfile(cookies):
            raise ExtractionError('找不到 YTDLP_COOKIES_FILE 指定的 cookies 檔案。')
        cmd.extend(['--cookies', cookies])
    return cmd


def get_info_via_cli(url, is_search=False, flat=False):
    """Look up queue metadata; playback uses a separately downloaded local file."""
    cmd = [*ytdlp_command(), '--dump-single-json']
    if flat:
        cmd.extend(['--flat-playlist', '--playlist-end', '100', '--ignore-errors'])
    else:
        cmd.extend(['--no-playlist', '--check-formats', '-f', 'bestaudio/best'])
    cmd.extend(['--', f'ytsearch1:{url}' if is_search else url])
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=120 if flat else 90,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError('YouTube 解析逾時，請稍後重試。') from exc
    if result.returncode:
        # Keep actionable errors in server logs; never send signed stream URLs/cookies to Discord.
        detail = result.stderr.strip()
        logger.warning('yt-dlp: %s', detail[-3000:])
        lowered = detail.lower()
        if 'sign in' in lowered or 'login_required' in lowered or 'not a bot' in lowered:
            raise ExtractionError('YouTube 要求登入驗證，請檢查伺服器 IP 或設定 YTDLP_COOKIES_FILE。')
        if '403' in lowered:
            raise ExtractionError('YouTube 拒絕音訊存取（403），請更新 yt-dlp 並檢查 PO Token 服務。')
        raise ExtractionError('無法取得 YouTube 影片；可能已下架、私人或有地區／年齡限制。詳細原因請查看 bot 日誌。')
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExtractionError('YouTube 沒有回傳有效的影片資訊。') from exc
    if not isinstance(data, dict):
        raise ExtractionError('找不到可播放的影片。')
    return data


def extract_tracks(data):
    entries = data.get('entries') if 'entries' in data else [data]
    tracks = [entry for entry in (entries or []) if entry and entry.get('id')]
    if not tracks:
        raise ExtractionError('找不到可播放的歌曲，清單可能為空或不公開。')
    return tracks


def track_url(track):
    # Queue entries come from YouTube. Never retain a temporary googlevideo URL.
    return f"https://www.youtube.com/watch?v={track['id']}"


