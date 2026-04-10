import os
import tempfile
import asyncio
import logging
from io import BytesIO
from flask import Flask
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import patoolib
from patoolib import extract_archive

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB (Telegram limit)

# Setup logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app for health checks
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running"

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# Store bot message IDs per chat for /clean
chat_messages = {}

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Simple welcome message"""
    await update.message.reply_text(
        "📦 **Archive Extractor Bot**\n\n"
        "Send me a `.zip`, `.rar` or `.7z` file and I will extract and send back all inner files.\n\n"
        "Use `/clean` to delete all messages in this chat (both yours and mine)."
    )

async def clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Delete all messages from this chat (user + bot)"""
    chat_id = update.effective_chat.id
    user_msg_id = update.message.message_id

    # Delete the command message itself
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=user_msg_id)
    except Exception as e:
        logger.warning(f"Could not delete user command: {e}")

    # Delete all stored bot messages in this chat
    if chat_id in chat_messages:
        for msg_id in chat_messages[chat_id]:
            try:
                await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as e:
                logger.warning(f"Could not delete bot message {msg_id}: {e}")
        chat_messages[chat_id].clear()
    else:
        # If no messages tracked, try to delete the bot's last reply (if any)
        pass

    # Optional: send a confirmation that gets deleted after 2 seconds
    confirm = await update.message.reply_text("🧹 Chat cleared")
    await asyncio.sleep(2)
    try:
        await confirm.delete()
    except:
        pass

async def handle_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Extract .zip/.rar/.7z and send inner files (no extra messages)"""
    user = update.effective_user
    document = update.message.document

    if not document:
        return

    filename = document.file_name.lower()
    if not (filename.endswith('.zip') or filename.endswith('.rar') or filename.endswith('.7z')):
        # Not an archive – ignore silently (no message)
        return

    if document.file_size > MAX_FILE_SIZE:
        await update.message.reply_text("❌ Archive too large (max 50MB)")
        return

    # Download the archive to a temporary file
    file = await context.bot.get_file(document.file_id)
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(document.file_name)[1], delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    # Create a temporary directory for extraction
    extract_dir = tempfile.mkdtemp()

    try:
        # Extract using patool (supports zip, rar, 7z)
        extract_archive(tmp_path, outdir=extract_dir)
    except Exception as e:
        logger.error(f"Extraction failed: {e}")
        # Send error only once
        await update.message.reply_text(f"❌ Extraction failed: {str(e)[:100]}")
        os.unlink(tmp_path)
        os.rmdir(extract_dir)
        return

    # Walk through extracted files and send each one
    sent_count = 0
    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            file_path = os.path.join(root, f)
            file_size = os.path.getsize(file_path)
            if file_size > MAX_FILE_SIZE:
                continue  # Skip files too large for Telegram

            with open(file_path, 'rb') as fp:
                data = fp.read()
            bio = BytesIO(data)
            bio.name = f  # keep original filename

            try:
                sent_msg = await update.message.reply_document(document=bio, filename=f)
                sent_count += 1
                # Store bot message ID for future /clean
                chat_id = update.effective_chat.id
                if chat_id not in chat_messages:
                    chat_messages[chat_id] = []
                chat_messages[chat_id].append(sent_msg.message_id)
                await asyncio.sleep(0.3)  # avoid flooding
            except Exception as e:
                logger.error(f"Failed to send file {f}: {e}")

    # Cleanup
    os.unlink(tmp_path)
    for root, dirs, files in os.walk(extract_dir, topdown=False):
        for f in files:
            os.remove(os.path.join(root, f))
        for d in dirs:
            os.rmdir(os.path.join(root, d))
    os.rmdir(extract_dir)

    # If no files were sent (e.g., empty archive, all too large), notify once
    if sent_count == 0:
        err_msg = await update.message.reply_text("⚠️ No valid files found inside archive (all empty or >50MB).")
        chat_id = update.effective_chat.id
        if chat_id not in chat_messages:
            chat_messages[chat_id] = []
        chat_messages[chat_id].append(err_msg.message_id)

# ================= MAIN =================
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("❌ Please set BOT_TOKEN environment variable.")
        return

    # Start Flask thread
    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    # Build bot
    app_bot = Application.builder().token(BOT_TOKEN).build()
    app_bot.add_handler(CommandHandler("start", start))
    app_bot.add_handler(CommandHandler("clean", clean))
    app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_archive))

    print("✅ Bot started. Waiting for archive files...")
    app_bot.run_polling()

if __name__ == "__main__":
    main()
