# YTDL Bot - Telegram Media Downloader

A high-performance Telegram bot for downloading media from popular platforms (YouTube, TikTok, Pinterest, VK, etc.) using `yt-dlp`. Built with **FastAPI**, **python-telegram-bot**, and **asyncio** for maximum concurrency and efficiency.

## 🚀 Key Features

*   **Multi-Platform Support**: Downloads videos from YouTube, TikTok (no watermark), Pinterest, VK, and many others supported by `yt-dlp`.
*   **Smart Quality Selection**:
    *   **Private Chat**: Offers an interactive menu to choose video quality (1080p, 720p, etc.) or Audio only.
    *   **Group Mode**: Automatically downloads the best quality video (<45MB) to ensure extensive compatibility and fast sharing without spamming the chat.
*   **GIF Conversion**:
    *   Easily convert any downloaded video to a GIF with a single click in Group chats.
    *   Smart caching reuses the downloaded video file for instant conversion.
*   **High Performance**:
    *   **Async/Await**: Fully asynchronous architecture to handle multiple downloads simultaneously.
    *   **Aria2c Support**: Integrated `aria2c` for accelerated downloads.
    *   **Smart Caching**: In-memory caching of video metadata (`TTLCache`) to reduce duplicate API calls to platforms.
*   **Robust Error Handling**: Handles regional restrictions, private content, and large file limits gracefully.
*   **Admin Tools**: Rate limiting and user management features (configurable).

## 🛠 Tech Stack

*   **Language**: Python 3.10+
*   **Framework**: [FastAPI](https://fastapi.tiangolo.com/) (Web Server & Webhook handling)
*   **Bot Framework**: [python-telegram-bot](https://python-telegram-bot.org/) (v20+)
*   **Core Engine**: [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Media extraction)
*   **Processing**: [FFmpeg](https://ffmpeg.org/) (Video/Audio processing & GIF conversion)
*   **Containerization**: Docker & Docker Compose

## 📂 Project Structure

```
.
├── app
│   ├── api              # FastAPI routes (webhooks, health checks)
│   ├── bot              # Telegram Bot logic
│   │   ├── callbacks.py # Button interactions (Download, Cancel, GIF)
│   │   ├── commands.py  # /start, /help handlers
│   │   ├── group_logic.py # Group chat specific logic
│   │   ├── keyboards.py # Inline keyboard builders
│   │   └── messages.py  # Private chat message handlers
│   ├── core             # Core configurations & utilities
│   │   ├── config.py    # Environment variables settings
│   │   ├── state.py     # Global state (locks, caches)
│   │   └── utils.py     # Helper functions
│   ├── services
│   │   ├── downloader.py # MediaSender service (Download/Send/Convert)
│   │   └── ytdlp        # yt-dlp wrapper service
│   └── main.py          # Application entry point
├── Dockerfile           # Docker build instructions
├── requirements.txt     # Python dependencies
└── README.md            # Project documentation
```

## ⚙️ Installation & Setup

### Prerequisites

*   Python 3.10+
*   FFmpeg (installed and in system PATH)
*   Aria2c (optional, recommended for speed)

### Local Development

1.  **Clone the repository**:
    ```bash
    git clone https://github.com/yourusername/ytdlbot.git
    cd ytdlbot
    ```

2.  **Create a virtual environment**:
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install dependencies**:
    ```bash
    pip install -r requirements.txt
    ```

4.  **Configure Environment**:
    Create a `.env` file in the root directory:
    ```env
    BOT_TOKEN=your_telegram_bot_token
    TELEGRAM_SECRET_TOKEN=random_string_for_security
    # Optional: Webhook URL (if defined, runs in Webhook mode, else Polling)
    # WEBHOOK_URL=https://your-domain.com
    ```

5.  **Run the bot**:
    ```bash
    uvicorn app.main:api --reload
    ```
    The bot will start in Polling mode if `WEBHOOK_URL` is not set.

## 🐳 Docker Deployment

1.  **Build the image**:
    ```bash
    docker build -t ytdlbot .
    ```

2.  **Run the container**:
    ```bash
    docker run -d --name ytdlbot \
      -e BOT_TOKEN=your_token \
      -v $(pwd)/downloads:/app/downloads \
      ytdlbot
    ```

## 🎮 Usage

### Private Chat
1.  Send a link (e.g., TikTok, YouTube).
2.  Wait for the bot to fetch formats.
3.  Choose your desired quality or format (Audio/Video).
4.  Receive the file!

### Group Chat
1.  Add the bot to a group.
2.  Send a link.
3.  The bot automatically downloads the best suitable video and sends it.
4.  Click **"Send GIF"** on the video reply to instantly get a GIF version.

## 🤝 Contributing

Contributions are welcome! Please follow these steps:
1.  Fork the repository.
2.  Create a feature branch (`git checkout -b feature/AmazingFeature`).
3.  Commit your changes (`git commit -m 'Add some AmazingFeature'`).
4.  Push to the branch (`git push origin feature/AmazingFeature`).
5.  Open a Pull Request.

## 📄 License

Distributed under the MIT License. See `LICENSE` for more information.
