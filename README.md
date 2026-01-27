# A Bot for Discord to Play Music on YouTube

This is a simple, efficient, and fully containerized **Bot for Discord to play music on YouTube**.
It is built using **Python (main.py)** and **yt-dlp**, designed to run easily in Docker with **Slash Command** interface.

## Quick Start

### 1. Prerequisites
* Docker and Docker Compose installed.
* A Discord Bot Token (with **Message Content Intent** enabled).

### 2. Installation

1.  **Clone the repository**
    ```bash
    git clone https://github.com/Spl6026/discordMusicBot.git
    cd discordMusicBot
    ```

2.  **Configure Environment Variables**
    Create a `.env` file in the root directory:
    ```bash
    # Create .env file
    echo "BOT_TOKEN=your_discord_token_here" > .env
    ```

3.  **Start the Bot**
    ```bash
    docker-compose up -d --build
    ```

### 3. Developer Portal Setup
**Note:** For the bot to function correctly, you must enable intents:
1.  Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2.  Select your Bot -> **Bot** tab.
3.  Scroll to **Privileged Gateway Intents**.
4.  Enable **Message Content Intent**.

## Commands

| Command | Description |
| :--- | :--- |
| `/play [url/search]` | Plays music from YouTube (supports links or search terms). |
| `/pause` | Pauses the current track. |
| `/resume` | Resumes playback. |
| `/skip` | Skips the current track. |
| `/stop` | Stops playback and clears the queue. |
| `/queue` | Displays the current music queue. |
| `/nowplaying` | Shows details of the song currently playing. |
| `/remove` | Clears the queue but finishes the current song. |
| `/leave` | Disconnects the bot from the voice channel. |

## Tech Stack

* **main.py**: The interface for Discord.
* **yt-dlp**: The core engine to handle YouTube streams.
* **FFmpeg**: Audio processing and conversion.
* **Docker**: Containerization.

## Environment Variables

| Variable | Description |
| :--- | :--- |
| `BOT_TOKEN` | Your Discord Bot Token (Required) |