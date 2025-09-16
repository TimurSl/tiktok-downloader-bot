#!/usr/bin/env python3
"""
Showcase Telegram Bot
A polished, public-facing bot designed to showcase media downloading
capabilities, optimized for free deployment on PythonAnywhere.
"""


import os
import tempfile
import logging
import sys
import subprocess
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, constants
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
from dotenv import load_dotenv

# --- Package Installation ---
# Ensure required packages are installed, especially for environments like PythonAnywhere
def install_packages():
    packages = ['python-telegram-bot==20.7', 'yt-dlp', 'python-dotenv']
    for package in packages:
        try:
            # A simple way to check for module existence
            __import__(package.split('==')[0].replace('-', '_'))
        except ImportError:
            print(f"Installing {package}...")
            # Using   --user for environments like PythonAnywhere
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', package])

install_packages()

# --- Configuration ---
load_dotenv()
BOT_TOKEN = os.getenv('BOT_TOKEN')
if not BOT_TOKEN:
    print("FATAL: BOT_TOKEN not found in environment variables or .env file.")
    sys.exit(1)

# --- Logging ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Downloader Class ---
class MediaDownloader:
    def __init__(self):
        # Using a temporary directory for downloads
        self.temp_dir = tempfile.mkdtemp(prefix="showcase_bot_")
        logger.info(f"Using temporary directory: {self.temp_dir}")

    def download_media(self, url, quality='best'):
        """Download media with PythonAnywhere optimizations."""
        output_template = os.path.join(self.temp_dir, '%(title).50s.%(ext)s')

        ydl_opts = {
            'outtmpl': output_template,
            'format': self._get_format_string(quality),
            'noplaylist': True,
            'max_filesize': 48 * 1024 * 1024,  # 48MB to stay under Telegram's 50MB limit
            'socket_timeout': 30,
            'retries': 2,
            'proxy': 'http://proxy.server:3128', # Essential for PythonAnywhere free tier
            'postprocessors': [],
        }

        if quality == 'audio':
            ydl_opts['postprocessors'].append({
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'm4a', # Use m4a as it's widely supported and doesn't require heavy encoding
            })

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info_dict = ydl.extract_info(url, download=True)
                # After download, find the correct file path
                filename = ydl.prepare_filename(info_dict)
                # The actual file might have a different extension after postprocessing
                base, _ = os.path.splitext(filename)
                for f in os.listdir(self.temp_dir):
                    if f.startswith(os.path.basename(base)):
                        return os.path.join(self.temp_dir, f)
                return filename # Fallback
        except Exception as e:
            logger.error(f"Download failed for URL {url}: {e}")
            return None

    def _get_format_string(self, quality):
        """Get the yt-dlp format string based on user's choice."""
        size_limit = "[filesize<48M]"
        formats = {
            'best': f'bestvideo[height<=720]{size_limit}+bestaudio/best{size_limit}',
            '480p': f'bestvideo[height<=480]{size_limit}+bestaudio/best{size_limit}',
            '360p': f'best[height<=360]{size_limit}',
            'audio': f'bestaudio/best{size_limit}'
        }
        return formats.get(quality, formats['best'])

    def get_media_info(self, url):
        """Extract media title without downloading."""
        ydl_opts = {
            'quiet': True,
            'extract_flat': True,
            'force_generic_extractor': True,
            'proxy': 'http://proxy.server:3128',
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return info.get('title', 'Unknown Title')
        except Exception as e:
            logger.warning(f"Could not extract info from {url}: {e}")
            return None

    def cleanup_file(self, file_path):
        """Clean up a single downloaded file."""
        try:
            if file_path and os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Cleaned up {file_path}")
        except Exception as e:
            logger.error(f"Error cleaning up file {file_path}: {e}")

downloader = MediaDownloader()

# --- Bot Command Handlers (Will be implemented in next steps) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    start_message = """
🤖 **Welcome to the Media Downloader Showcase!**

This bot is a demonstration of what you can build with Python and deploy for **100% free** on platforms like PythonAnywhere.

It can download videos and audio from many popular sites.

**How to use:**
1. Just send me a link to a video (e.g., from YouTube, Twitter, TikTok).
2. Choose your desired quality.
3. The bot will send you the file!

---

Want to build your own version of this bot?
Check out the complete source code and tutorial on GitHub:
➡️ [yomazini/telegram-automation-bot-framework](https://github.com/yomazini/telegram-automation-bot-framework)

Use /help to see all commands.
"""
    await update.message.reply_text(
        start_message,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_message = """
❓ **Help & Commands**

This bot can download media (video or audio) from many popular websites.

**How it works:**
1. Send a link to a video.
2. Choose the quality you want.
3. The bot sends you the file.

**Available Commands:**
`/start` - Shows the welcome message.
`/help` - Shows this help message.
`/about` - Learn more about the creator.

If you encounter any issues, please try a different link or quality option.
"""
    await update.message.reply_text(
        help_message,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    about_message = """
This bot was created by **Youssef Mazini**. I'm passionate about building automation tools that are powerful, efficient, and easy to use.

If you're interested in software development, AI, automation, or just want to connect, you can find me here:

🔗 **LinkedIn:** [Youssef Mazini](https://www.linkedin.com/in/youssef-mazini/)
🐦 **Twitter (X):** [@mazini_youssef](https://x.com/mazini_youssef)
🐙 **GitHub:** [yomazini](https://github.com/yomazini)

Feel free to reach out—I'd love to hear from you!
"""
    await update.message.reply_text(
        about_message,
        parse_mode=constants.ParseMode.MARKDOWN,
        disable_web_page_preview=True
    )

# --- Core Media Handling Logic ---
async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("Please send a valid URL.")
        return

    # Let the user know the bot is working
    processing_message = await update.message.reply_text("🔎 *Fetching media info...*")

    title = downloader.get_media_info(url)
    if not title:
        await processing_message.edit_text("❌ *Could not fetch info.* Please check the link or try another one.")
        return

    # Truncate title if it's too long
    display_title = (title[:70] + '...') if len(title) > 70 else title

    response_text = f"✅ **Media Found:**\n\n`{display_title}`\n\n✨ Choose your desired quality:"

    keyboard = [
        [InlineKeyboardButton("🎥 720p (Best)", callback_data=f"dl_best_{url}")],
        [
            InlineKeyboardButton("🎬 480p", callback_data=f"dl_480p_{url}"),
            InlineKeyboardButton("📱 360p", callback_data=f"dl_360p_{url}")
        ],
        [InlineKeyboardButton("🎵 Audio (M4A)", callback_data=f"dl_audio_{url}")]
    ]

    await processing_message.edit_text(
        response_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, quality, url = query.data.split('_', 2)
    except ValueError:
        await query.edit_message_text("❌ Error: Invalid request. Please try again.")
        return

    await query.edit_message_text("⏳ *Downloading, please wait...*")

    file_path = downloader.download_media(url, quality)

    if not file_path:
        await query.edit_message_text("❌ *Download failed.* This might be due to platform restrictions or an invalid link.")
        return

    try:
        await query.edit_message_text("⬆️ *Uploading to Telegram...*")

        with open(file_path, 'rb') as file:
            if quality == 'audio':
                await context.bot.send_audio(chat_id=query.message.chat_id, audio=file, write_timeout=60)
            else:
                await context.bot.send_video(chat_id=query.message.chat_id, video=file, write_timeout=60)

        await query.delete_message() # Clean up the "Uploading..." message
    except Exception as e:
        await query.edit_message_text("❌ *Upload failed.* The file might be too large or there was a network issue.")
        logger.error(f"Upload error: {e}")
    finally:
        downloader.cleanup_file(file_path)

# --- Main Application Setup ---
def main():
    """Starts the bot."""
    app = Application.builder().token(BOT_TOKEN).build()

    # Register command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("about", about_command))

    # Register message and callback handlers
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(download_callback, pattern="^dl_"))

    print("🚀 Showcase bot is starting...")
    app.run_polling()
    print("👋 Showcase bot has stopped.")

if __name__ == '__main__':
    main()

