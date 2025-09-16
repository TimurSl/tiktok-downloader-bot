#!/usr/bin/env python3
"""
Simple Telegram Media Downloader Bot (No FFmpeg version)
Works on: Windows, Mac, Linux (Local Development)
"""
  
import os
import sys
import logging
import tempfile
import shutil
from pathlib import Path
import subprocess

# Install packages if missing
def install_packages():
    packages = ['python-telegram-bot==20.7', 'yt-dlp', 'requests', 'python-dotenv']
    for package in packages:
        try:
            __import__(package.split('==')[0].replace('-', '_'))
        except ImportError:
            print(f"Installing {package}...")
            subprocess.check_call([sys.executable, '-m', 'pip', 'install', package])

install_packages()

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import yt_dlp
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
BOT_TOKEN = os.getenv('BOT_TOKEN', 'YOUR_BOT_TOKEN_HERE')
AUTHORIZED_USERS = [int(x) for x in os.getenv('AUTHORIZED_USER', '123456789').split(',')]

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class MediaDownloader:
    def __init__(self):
        self.temp_dir = tempfile.mkdtemp()

    def download_media(self, url, quality='best', format_type='video'):
        output_path = os.path.join(self.temp_dir, '%(title)s.%(ext)s')

        if format_type == 'audio':
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': output_path,
            }
        else:
            format_map = {
                'best': 'best[height<=1080]',
                '720p': 'best[height<=720]',
                '480p': 'best[height<=480]',
                '360p': 'best[height<=360]'
            }

            ydl_opts = {
                'format': format_map.get(quality, 'best'),
                'outtmpl': output_path,
            }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                downloaded_file = ydl.prepare_filename(info)

            # Find downloaded file, sometimes the extension is not what ydl.prepare_filename gives
            base, _ = os.path.splitext(downloaded_file)
            for file in os.listdir(self.temp_dir):
                if file.startswith(os.path.basename(base)):
                    return os.path.join(self.temp_dir, file)
            return downloaded_file if os.path.exists(downloaded_file) else None

        except Exception as e:
            logger.error(f"Download error: {e}")
            return None

    def cleanup(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

downloader = MediaDownloader()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS:
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return

    await update.message.reply_text("""
🤖 **Telegram Media Downloader Bot (No FFmpeg)**

This version doesn't require FFmpeg. Audio is saved in its original format (e.g., .m4a, .webm).

📺 **Supported Platforms:**
- YouTube, Instagram, TikTok, Twitter, etc.

📱 **How to use:**
Just send me any media URL!

**Commands:**
/start - Show this message
""", parse_mode='Markdown')

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS:
        return

    url = update.message.text.strip()
    if not url.startswith(('http://', 'https://')):
        await update.message.reply_text("❌ Please send a valid URL")
        return

    # Quality selection keyboard
    keyboard = [
        [InlineKeyboardButton("🎥 Best Quality", callback_data=f"dl_best_{url}")],
        [
            InlineKeyboardButton("🎬 720p", callback_data=f"dl_720p_{url}"),
            InlineKeyboardButton("📱 480p", callback_data=f"dl_480p_{url}")
        ],
        [InlineKeyboardButton("🎵 Audio Only", callback_data=f"dl_audio_{url}")]
    ]

    await update.message.reply_text(
        "📹 **Choose download quality:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown'
    )

async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.from_user.id not in AUTHORIZED_USERS:
        return

    # Parse callback data
    parts = query.data.split('_', 2)
    if len(parts) != 3:
        return

    _, quality, url = parts

    await query.edit_message_text("⬇️ Downloading... Please wait.")

    # Download
    format_type = 'audio' if quality == 'audio' else 'video'
    file_path = downloader.download_media(url, quality, format_type)

    if not file_path or not os.path.exists(file_path):
        await query.edit_message_text("❌ Download failed")
        return

    # Check file size (50MB limit for Telegram)
    file_size = os.path.getsize(file_path)
    if file_size > 50 * 1024 * 1024:
        await query.edit_message_text("❌ File too large (>50MB). Try a lower quality.")
        os.remove(file_path)
        return

    # Send file
    try:
        await query.edit_message_text("📤 Uploading...")

        with open(file_path, 'rb') as file:
            if format_type == 'audio':
                await context.bot.send_audio(chat_id=query.message.chat_id, audio=file, title=os.path.basename(file_path))
            else:
                await context.bot.send_video(chat_id=query.message.chat_id, video=file)

        await query.edit_message_text("✅ Done!")

    except Exception as e:
        await query.edit_message_text("❌ Upload failed")
        logger.error(f"Upload error: {e}")
    finally:
        try:
            os.remove(file_path)
        except:
            pass

def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("❌ Please set BOT_TOKEN in .env file!")
        print("1. Copy .env.example to .env")
        print("2. Add your bot token from @BotFather")
        return

    print("🚀 Starting Telegram Media Downloader (No FFmpeg)...")
    print(f"👥 Authorized users: {AUTHORIZED_USERS}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.add_handler(CallbackQueryHandler(download_callback, pattern="^dl_"))

    print("✅ Bot is running! Press Ctrl+C to stop.")
    app.run_polling()

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped by user")
    except Exception as e:
        print(f"❌ Error: {e}")
    finally:
        downloader.cleanup()



