# Discord YouTube 音樂機器人

支援 YouTube／YouTube Music 網址、歌名搜尋與播放清單，使用 Slash Commands 控制。

依賴設定使用 Python 3.12、discord.py >=2.7.1,<3、davey 0.1.7、yt-dlp >=2026.10.26、FFmpeg、Deno >=2.9.8 與 bgutil PO Token 服務 2.0.2。實際安裝版本由建置時解析，詳見 `environment.yml` 與 `requirements.txt`。

yt-dlp 的 default 套件群會安裝相容的 yt-dlp-ejs；Deno 負責執行 YouTube JavaScript challenges。預設讓 yt-dlp 選擇 YouTube client，可透過環境變數調整。

一般歌曲會先完整下載到暫存檔，再交給 FFmpeg 從本機播放；播放開始前也會先完成第一幀解碼，避免解碼器啟動時間造成追趕送包。目前歌曲播放期間會預載佇列的下一首。播放中不需等待 YouTube 重新連線，也不會因為下載卡住而額外補靜音。

Linux 下使用 `CLOCK_MONOTONIC_RAW` 控制每 20 毫秒音訊幀的送出時間，避開這台 WSL 的已校正系統時鐘倍率誤差。等待提早結束時會重新檢查原始時鐘；若程序曾卡頓或暫停，不會集中補送舊的計時欠額。音訊幀會保持原順序，不以丟棄歌曲內容來追趕時間。Windows 原生執行時使用 QueryPerformanceCounter。

第一首、尚未下載好的下一首或臨時插播，需要先等待完整下載；這不是無縫接歌功能。每首音訊上限為 200 MiB，下載逾時為 5 分鐘；直播與尚未開始的影片暫不支援。下載失敗或缺少音訊分段時會跳過，不播放不完整檔案。暫存音訊會在播放完畢、停止、離開或取消預載後清理。

## 程式架構

| 檔案 | 職責 |
| --- | --- |
| `main.py` | 啟動機器人、Slash Commands、歌曲佇列、預載與閒置離開 |
| `youtube.py` | yt-dlp 設定、影片資訊解析與播放清單處理 |
| `audio_cache.py` | 完整音訊下載、暫存清理與 FFmpeg 音訊來源 |
| `voice_timing.py` | 語音播放器與穩定送包計時 |
| `music_controls.py` | 播放控制按鈕與互動事件 |
| `discord_helpers.py` | 播放資訊訊息與語音頻道權限檢查 |
| `Dockerfile`、`docker-compose.yml` | 機器人與 PO Token 服務部署 |
| `environment.yml`、`requirements.txt` | Conda 與 Python 套件依賴 |

## Docker 啟動

需要 Docker Desktop／Docker Engine 與 Docker Compose。

1. 複製 `.env.example` 為 `.env`，將 `BOT_TOKEN` 改成你的機器人 Token，並依需要調整其他設定。
2. 在 Discord Developer Portal 的 OAuth2 URL Generator 選擇 bot 與 applications.commands，邀請機器人進入伺服器。授予 View Channels、Send Messages、Embed Links、Connect 與 Speak 權限。
3. 執行：

~~~powershell
docker compose up -d --build
~~~

使用 Slash Commands 不需要開啟 Message Content Intent。

加入一般語音頻道後，輸入 /play，search 填歌曲名稱、YouTube 網址或 YouTube Music 網址。清單每次最多加入前 100 首。/insert 會接在目前歌曲之後，/interrupt 會立刻切歌。

想隨機播放清單時，先用 /play 加入歌曲，再輸入 /shuffle；它會重新隨機排列目前等待中的歌曲，保留正在播放或載入中的歌曲。之後新增的歌曲仍依原本加入方式排隊，需要時可再次使用 /shuffle。

## 指令

| 指令 | 功能 |
| --- | --- |
| /play search | 播放歌曲或加入排隊清單 |
| /insert search | 插入下一首，不打斷目前歌曲 |
| /interrupt search | 立刻插播 |
| /pause、/resume | 暫停、繼續 |
| /skip | 跳過目前歌曲（包含暫停或正在解析的歌曲） |
| /queue | 查看佇列前 10 首 |
| /shuffle | 隨機排列等待中的歌曲，保留目前播放 |
| /nowplaying | 查看目前播放資訊 |
| /remove | 清空佇列，保留目前歌曲 |
| /stop | 停止並清空 |
| /leave | 清空並離開語音頻道 |

控制播放時，使用者需與機器人在相同語音頻道。

「正在播放」訊息附有 **暫停／繼續、下一首、隨機排序、停止** 按鈕；也可用 `/nowplaying` 取得最新面板。換歌或停止後，舊面板會失效。停止會清空等待清單，隨機排序只影響等待中的歌曲。

清單播完，或語音頻道只剩機器人時，連續閒置 **5 分鐘**會自動離開並清空清單。有人回來或開始新的播放時會重新檢查並取消不再適用的倒數；有人在頻道內暫停歌曲、載入歌曲時，不視為播完。

## 查看日誌

~~~powershell
docker compose logs --tail 100 bot bgutil-provider
~~~

## 更新 YouTube 解析器

YouTube 可能改變播放機制，遇到解析失敗可重新建立環境，取得符合 requirements.txt 的新版套件：

~~~powershell
docker compose build --pull --no-cache bot
docker compose up -d
~~~

bgutil 的 Python plugin 與服務映像固定為相同的 2.0.2 版；日後升級兩者應一起調整。

## 可選設定

修改 .env 後以 docker compose up -d --force-recreate bot 套用。

| 變數 | 用途 |
| --- | --- |
| YTDLP_JS_RUNTIME | 預設 deno；也可指定 deno:/path/to/deno |
| YTDLP_PLAYER_CLIENT | 預設留白，使用 yt-dlp 預設值；需要排查時可嘗試 mweb,tv,web_safari |
| YTDLP_COOKIES_FILE | 可選的 Netscape 格式 cookies 檔案路徑；Docker 內需能讀取，例如 /app/cookies.txt |
| BGUTIL_PROVIDER_URL | 原生 Python 預設 http://127.0.0.1:4416；Compose 固定使用內部服務 http://bgutil-provider:4416 |
| IDLE_TIMEOUT_SECONDS | 播完或無聽眾時自動離開的秒數，預設 300；設為 0 關閉 |

PO Token 無法保證每個 IP 都能通過 YouTube 的登入／機器人檢查。遇到 Sign in to confirm you're not a bot 時，先查日誌及目前網路狀況，必要時自行提供 cookies；不要把 cookies 或 Bot Token 提交到 Git。

## 不使用 Docker 執行 Python

~~~powershell
conda env create -f environment.yml
conda activate discord_yt_bot
docker run -d --init --name bgutil-provider-local -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider:2.0.2
python main.py
~~~

程式會自動讀取 .env。若原本已有舊的 Conda 環境，可用 conda env update -f environment.yml --prune 更新。

## 測試

`tests/` 目前依專案設定由 Git 忽略，測試檔保留於本機。已有測試檔時，可在上述 Conda 環境執行以下指令。

```powershell
python -m unittest discover -s tests -v
```

測試涵蓋下載取消與逾時、完整音訊快取、預載重用、插播替換、停止與跳過期間的下載競態，播放按鈕的權限與過期面板、閒置倒數，以及本機 HTTP 傳輸中斷後的恢復。整合測試會關閉來源伺服器，再驗證整段音訊仍以正常速度播放、不額外插入靜音。不會連線 Discord。

## 官方文件

- [yt-dlp EJS 安裝說明](https://github.com/yt-dlp/yt-dlp/wiki/EJS)
- [yt-dlp PO Token 指南](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide)
- [bgutil provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
- [discord.py 更新記錄](https://discordpy.readthedocs.io/en/stable/whats_new.html)
