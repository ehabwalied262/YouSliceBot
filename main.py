import logging
import os
import re
import subprocess
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes
from telegram.ext.filters import Text, Command
from telegram.error import TimedOut
import yt_dlp
import asyncio
from datetime import datetime, timedelta

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Telegram bot token from BotFather
TOKEN = "7730693256:AAGgF3uqlGjDRelFNVz12lcAePX_Y0LhHVI"

# Webhook URL (replace with your ngrok or server URL)
WEBHOOK_URL = "https://a0dc-156-203-214-152.ngrok-free.app"

# Create a queue for processing video requests
request_queue = asyncio.Queue()

# Dictionary to track user states: {user_id: {"last_request": datetime, "daily_count": int, "active_requests": int}}
user_states = {}

# Constants
#MIN_WAIT 5 Wait Time (5 minutes wait between requests)
MIN_WAIT_TIME = timedelta(minutes=5)
DAILY_LIMIT = 10  # Max 10 videos per day per user

def validate_time_format(time_str):
    """Validate the time format (e.g., MM:SS or HH:MM:SS)."""
    pattern = r"^(?:(?:[0-9]{1,2}:)?[0-9]{1,2}:[0-5][0-9]|[0-9]+)$"
    if not re.match(pattern, time_str):
        raise ValueError("Time must be in MM:SS or HH:MM:SS format (e.g., 5:00 or 1:05:30).")
    return time_str

def convert_to_seconds(time_str):
    """Convert time in MM:SS or HH:MM:SS format to seconds."""
    parts = time_str.split(":")
    if len(parts) == 1:  # Only seconds
        return int(parts[0])
    elif len(parts) == 2:  # MM:SS
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:  # HH:MM:SS
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    else:
        raise ValueError("Invalid time format.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the /start command with a lively welcome message."""
    await update.message.reply_text(
        "🎉 Hey there! Welcome to YouSliceBot! 🎥✨\n"
        "I’m here to help you grab specific clips from YouTube videos! 🚀\n"
        "Just send me a message like this:\n"
        "📌 <YouTube URL> <start time> <end time>\n"
        "For example: https://www.youtube.com/watch?v=PVGeM40dABA 00:37 00:44\n"
        "Let’s get started! 😊"
    )

async def download_and_trim_video(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, start_time: str, end_time: str, output_filename: str):
    """Download a specific segment of a YouTube video with controlled quality."""
    user_id = update.message.from_user.id
    temp_file = f"temp_video_{user_id}.mp4"  # Unique temp file per user
    video_sent = False
    try:
        # Validate time inputs
        start_time = validate_time_format(start_time)
        end_time = validate_time_format(end_time)

        # Convert times to seconds for duration calculation
        start_seconds = convert_to_seconds(start_time)
        end_seconds = convert_to_seconds(end_time)
        if start_seconds >= end_seconds:
            raise ValueError("⏰ Oops! The end time must be greater than the start time. Try again! 😅")
        duration = end_seconds - start_seconds

        # Step 1: Download the video using yt-dlp
        await update.message.reply_text("⬇️ Downloading your video now... Hang tight! 🎬")
        ydl_opts = {
            'format': 'bestvideo[height<=480]+bestaudio/best[height<=480]',  # Max 480p resolution
            'outtmpl': temp_file,  # Temporary file name
            'merge_output_format': 'mp4',  # Ensure output is in mp4 format
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_duration = info.get('duration', 0)  # Get video duration in seconds

        # Step 2: Verify the downloaded video duration
        if video_duration <= duration + 5:  # Allow 5 seconds of tolerance
            logger.info("yt-dlp trimmed the video correctly.")
            os.rename(temp_file, output_filename)
        else:
            # Step 3: Trim and compress the video using ffmpeg
            await update.message.reply_text(f"✂️ Trimming and compressing your clip from {start_time} to {end_time}... 🛠️")
            ffmpeg_cmd = [
                'ffmpeg',
                '-i', temp_file,  # Input file
                '-ss', start_time,  # Start time
                '-t', str(duration),  # Duration
                '-c:v', 'libx264',  # Re-encode video with H.264
                '-crf', '23',  # Constant Rate Factor (lower = better quality, higher = smaller size)
                '-preset', 'slow',  # Slower preset for better compression
                '-c:a', 'aac',  # Re-encode audio with AAC
                '-b:a', '128k',  # 128 kbps audio bitrate
                '-y',  # Overwrite output file if it exists
                output_filename  # Output file
            ]
            result = subprocess.run(ffmpeg_cmd, check=True, capture_output=True, text=True)
            logger.info(f"ffmpeg output: {result.stdout}")
            logger.error(f"ffmpeg error (if any): {result.stderr}")

        # Step 4: Check file size
        file_size = os.path.getsize(output_filename) / (1024 * 1024)  # Size in MB
        logger.info(f"File size: {file_size:.2f} MB")
        if file_size > 50:
            raise ValueError("📏 Oh no! The video is too big for Telegram (over 50 MB). Try a shorter clip! 📉")

        # Step 5: Send the video to the user with retry on timeout
        await update.message.reply_text("🚀 Uploading your video now... Almost there! 📤")
        max_retries = 3
        for attempt in range(max_retries):
            if video_sent:
                break  # Skip if the video has already been sent
            try:
                with open(output_filename, 'rb') as video:
                    await update.message.reply_video(video)
                video_sent = True
                logger.info("Video uploaded successfully.")
                await update.message.reply_text("🎉 All done! Your video is ready! Enjoy! 😊")
                break  # Success, exit the retry loop
            except TimedOut as e:
                if attempt == max_retries - 1:  # Last attempt
                    raise e
                await update.message.reply_text("⏳ Upload timed out, retrying... Please wait! 🔄")
                logger.warning(f"Upload attempt {attempt + 1} timed out, retrying...")

    except Exception as e:
        await update.message.reply_text(f"😓 Sorry, something went wrong: {str(e)}\nLet’s try again! 🔄")
    finally:
        # Clean up files
        for file in [temp_file, output_filename]:
            if os.path.exists(file):
                os.remove(file)
        # Decrease active requests count
        user_states[user_id]["active_requests"] -= 1

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle user messages by adding them to the queue with limits."""
    user_id = update.message.from_user.id
    current_time = datetime.now()

    # Initialize user state if not exists
    if user_id not in user_states:
        user_states[user_id] = {"last_request": None, "daily_count": 0, "active_requests": 0, "day": current_time.date()}

    # Reset daily count if it's a new day
    if user_states[user_id]["day"] != current_time.date():
        user_states[user_id] = {"last_request": None, "daily_count": 0, "active_requests": 0, "day": current_time.date()}

    # Check if user has an active request
    if user_states[user_id]["active_requests"] > 0:
        await update.message.reply_text("⏳ You already have a request being processed! Wait until it’s done before sending another.")
        return

    # Check 5-minute wait
    if user_states[user_id]["last_request"] and (current_time - user_states[user_id]["last_request"]) < MIN_WAIT_TIME:
        wait_seconds = (MIN_WAIT_TIME - (current_time - user_states[user_id]["last_request"])).seconds
        await update.message.reply_text(f"⏲️ Please wait {wait_seconds // 60} minutes and {wait_seconds % 60} seconds before sending another request!")
        return

    # Check daily limit
    if user_states[user_id]["daily_count"] >= DAILY_LIMIT:
        await update.message.reply_text(f"🚫 You’ve reached your daily limit of {DAILY_LIMIT} videos! Try again tomorrow.")
        return

    # Parse input
    user_input = update.message.text.split()
    if len(user_input) != 3:
        await update.message.reply_text(
            "🤔 Hmm, that’s not quite right! Please use this format:\n"
            "📌 <YouTube URL> <start time> <end time>\n"
            "For example: https://www.youtube.com/watch?v=PVGeM40dABA 00:37 00:44\n"
            "Give it another shot! 🚀"
        )
        return

    url, start_time, end_time = user_input
    output_filename = f"output_{user_id}.mp4"

    # Update user state
    user_states[user_id]["last_request"] = current_time
    user_states[user_id]["daily_count"] += 1
    user_states[user_id]["active_requests"] += 1

    # Add the request to the queue
    await request_queue.put((update, context, url, start_time, end_time, output_filename))
    await update.message.reply_text(f"📥 Your request has been queued! Daily usage: {user_states[user_id]['daily_count']}/{DAILY_LIMIT}. Please wait... ⏳")

async def process_queue(worker_id):
    """Background worker to process queued video requests."""
    logger.info(f"Worker {worker_id} started")
    while True:
        # Get the next request from the queue
        update, context, url, start_time, end_time, output_filename = await request_queue.get()
        try:
            # Process the request
            await download_and_trim_video(update, context, url, start_time, end_time, output_filename)
        finally:
            # Mark the task as done
            request_queue.task_done()

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors."""
    logger.error(f"Update {update} caused error {context.error}")
    if update and update.message:
        await update.message.reply_text("😱 Uh-oh! Something broke on my end. Let’s try again! 🔄")

def main():
    """Set up the bot and webhook."""
    # Create the Application with a custom request timeout
    application = Application.builder().token(TOKEN).read_timeout(180).write_timeout(180).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(Text() & ~Command(), handle_message))
    application.add_error_handler(error_handler)

    # Start 5 service workers for true parallelism
    for i in range(5):
        asyncio.ensure_future(process_queue(i))

    # Set up webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=8443,
        url_path=TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{TOKEN}"
    )

if __name__ == "__main__":
    main()