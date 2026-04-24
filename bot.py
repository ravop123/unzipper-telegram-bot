import os
import re
import asyncio
import tempfile
import logging
import shutil
import zipfile
import subprocess
from io import BytesIO
from typing import Dict, List, Optional

import patoolib
import py7zr
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
PORT = int(os.environ.get("PORT", 8080))
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB (Telegram limit)
CHANNEL_LINK = "https://t.me/+QP2gNqcUbSRiYTk1"

# Text file extensions to scan for branding
TEXT_EXTENSIONS = {".txt", ".md", ".cfg", ".ini", ".conf", ".json", ".xml", ".html", ".css", ".js", ".py", ".sh", ".bat"}

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Flask app for health checks
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# Store all message IDs per chat for /clean
chat_messages: Dict[int, List[int]] = {}

# ================= HELPER FUNCTIONS =================
async def store_message(chat_id: int, message_id: int) -> None:
    if chat_id not in chat_messages:
        chat_messages[chat_id] = []
    chat_messages[chat_id].append(message_id)

async def delete_stored_messages(chat_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if chat_id not in chat_messages:
        return
    for msg_id in chat_messages[chat_id]:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception as e:
            logger.warning(f"Could not delete message {msg_id}: {e}")
    chat_messages[chat_id].clear()

def get_archive_type(file_path: str) -> Optional[str]:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".zip":
        return "zip"
    if ext == ".rar":
        return "rar"
    if ext == ".7z":
        return "7z"
    return None

def is_rar_available() -> bool:
    """Check if 'rar' command is installed and accessible."""
    return shutil.which("rar") is not None

async def send_progress(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE, percent: int):
    bar_length = 20
    filled = int(bar_length * percent / 100)
    bar = "█" * filled + "░" * (bar_length - filled)
    text = f"🔄 Processing...\n`[{bar}] {percent}%`"
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown"
        )
    except Exception as e:
        logger.debug(f"Progress update failed: {e}")

def extract_archive_native(archive_path: str, extract_dir: str, archive_type: str) -> bool:
    """Extract archive using native libraries (no patool). Returns True on success."""
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(extract_dir)
        elif archive_type == "7z":
            with py7zr.SevenZipFile(archive_path, "r") as szf:
                szf.extractall(extract_dir)
        elif archive_type == "rar":
            # Use patool as fallback for RAR
            patoolib.extract_archive(archive_path, outdir=extract_dir)
        else:
            return False
        return True
    except Exception as e:
        logger.error(f"Native extraction failed: {e}")
        return False

async def convert_archive(
    input_path: str,
    output_path: str,
    target_type: str,
    progress_msg_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[bool, str]:
    """
    Convert archive to target_type (zip/rar/7z) with progress.
    Returns (success, error_message).
    """
    temp_extract = tempfile.mkdtemp()
    try:
        # Determine input archive type
        input_type = get_archive_type(input_path)
        if not input_type:
            return False, "Unknown input archive format"

        # Extract using native method first
        if not extract_archive_native(input_path, temp_extract, input_type):
            # Fallback to patool
            try:
                patoolib.extract_archive(input_path, outdir=temp_extract)
            except Exception as e:
                return False, f"Extraction failed: {str(e)}"

        await send_progress(chat_id, progress_msg_id, context, 30)

        # Count total files for progress
        total_files = sum(1 for root, _, files in os.walk(temp_extract) for f in files)
        processed = 0

        # Create new archive
        if target_type == "zip":
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(temp_extract):
                    for f in files:
                        full_path = os.path.join(root, f)
                        arcname = os.path.relpath(full_path, temp_extract)
                        zf.write(full_path, arcname)
                        processed += 1
                        if total_files > 0:
                            percent = 30 + int(60 * processed / total_files)
                            await send_progress(chat_id, progress_msg_id, context, min(percent, 90))
        elif target_type == "7z":
            with py7zr.SevenZipFile(output_path, "w") as szf:
                szf.writeall(temp_extract, arcname="")
            await send_progress(chat_id, progress_msg_id, context, 90)
        elif target_type == "rar":
            if not is_rar_available():
                return False, "RAR command not installed. Cannot create RAR archives."
            # patoolib.create_archive expects a list of source files/dirs
            patoolib.create_archive(output_path, [temp_extract])
            await send_progress(chat_id, progress_msg_id, context, 90)
        else:
            return False, f"Unsupported target type: {target_type}"

        await send_progress(chat_id, progress_msg_id, context, 100)
        return True, ""

    except Exception as e:
        logger.exception("Conversion failed")
        return False, str(e)
    finally:
        shutil.rmtree(temp_extract, ignore_errors=True)

async def clean_branding_in_archive(
    input_path: str,
    output_path: str,
    progress_msg_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[bool, str]:
    """
    Extract archive, remove lines matching skip_patterns, keep lines containing 'cookie',
    append channel link to each modified text file. If no modifications, add JOIN_CHANNEL.txt.
    Returns (success, error_message).
    """
    skip_patterns = [
        r'^[#/;*]', r'checker', r'Checker', r'CHECKER', r'bypass', r'Bypass', r'BYPASS',
        r'crack', r'Crack', r'CRACK', r'premium', r'Premium', r'PREMIUM',
        r'free', r'Free', r'FREE', r'hits', r'Hits', r'HITS',
        r'working', r'Working', r'WORKING', r'valid', r'Valid', r'VALID',
        r'github', r'GitHub', r'GITHUB', r'telegram', r'Telegram', r'TELEGRAM',
        r'discord', r'Discord', r'DISCORD', r'join', r'Join', r'JOIN',
        r'channel', r'Channel', r'CHANNEL', r'group', r'Group', r'GROUP',
        r'@', r'http', r'www\.', r'\.com', r'\.org', r'\.net',
        r'Plan:', r'Country:', r'Autopay:', r'Trial:', r'Owner:', r'Email:',
        r'Max Streams:', r'Payment Method:', r'Member Since:', r'Extra members:',
        r'Checker By', r'Cookie 👇'
    ]
    compiled = [re.compile(p, re.IGNORECASE) for p in skip_patterns]

    archive_type = get_archive_type(input_path)
    if not archive_type:
        return False, "Unknown archive format"

    temp_extract = tempfile.mkdtemp()
    try:
        # Extract using native method first
        if not extract_archive_native(input_path, temp_extract, archive_type):
            try:
                patoolib.extract_archive(input_path, outdir=temp_extract)
            except Exception as e:
                return False, f"Extraction failed: {str(e)}"

        await send_progress(chat_id, progress_msg_id, context, 30)

        total_files = sum(1 for root, _, files in os.walk(temp_extract) for f in files)
        processed = 0
        modified_any = False

        for root, _, files in os.walk(temp_extract):
            for file in files:
                file_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1].lower()
                if ext in TEXT_EXTENSIONS:
                    try:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            lines = f.readlines()

                        new_lines = []
                        for line in lines:
                            # Keep lines containing 'cookie' (case-insensitive)
                            if re.search(r"cookie", line, re.IGNORECASE):
                                new_lines.append(line.rstrip('\n'))
                                continue
                            # Skip if matches any pattern
                            if any(p.search(line) for p in compiled):
                                modified_any = True
                                continue
                            new_lines.append(line.rstrip('\n'))

                        # If any line was removed, append channel link at the end
                        if len(new_lines) != len(lines):
                            new_lines.append(CHANNEL_LINK)
                            modified_any = True

                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write("\n".join(new_lines))
                    except Exception as e:
                        logger.warning(f"Could not process {file_path}: {e}")

                processed += 1
                if total_files > 0:
                    percent = 30 + int(40 * processed / total_files)
                    await send_progress(chat_id, progress_msg_id, context, min(percent, 70))

        # If no modification happened, create a new file with the channel link
        if not modified_any:
            join_file = os.path.join(temp_extract, "JOIN_CHANNEL.txt")
            with open(join_file, "w", encoding="utf-8") as f:
                f.write(CHANNEL_LINK)

        # Repack
        if archive_type == "zip":
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(temp_extract):
                    for f in files:
                        full_path = os.path.join(root, f)
                        arcname = os.path.relpath(full_path, temp_extract)
                        zf.write(full_path, arcname)
        elif archive_type == "7z":
            with py7zr.SevenZipFile(output_path, "w") as szf:
                szf.writeall(temp_extract, arcname="")
        elif archive_type == "rar":
            if not is_rar_available():
                return False, "RAR command not installed. Cannot repack RAR archive after cleaning."
            patoolib.create_archive(output_path, [temp_extract])
        else:
            return False, f"Unsupported archive type: {archive_type}"

        await send_progress(chat_id, progress_msg_id, context, 100)
        return True, ""

    except Exception as e:
        logger.exception("Branding cleaning failed")
        return False, str(e)
    finally:
        shutil.rmtree(temp_extract, ignore_errors=True)

# ================= TELEGRAM HANDLERS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "📦 **Archive Extractor & Converter Bot**\n\n"
        "Send me a `.zip`, `.rar` or `.7z` file.\n"
        "I will show you three options:\n"
        "• **Convert** – change archive format with live progress\n"
        "• **Unzip** – extract all files\n"
        "• **Branding Cleaner** – remove Discord/Telegram/etc. branding lines, keep cookie lines, and add our channel link\n\n"
        "Use `/clean` to delete all messages in this chat (no trace left).",
        parse_mode="Markdown",
    )
    await store_message(update.effective_chat.id, msg.message_id)

async def clean(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    command_msg_id = update.message.message_id
    await store_message(chat_id, command_msg_id)
    await delete_stored_messages(chat_id, context)
    confirm = await update.message.reply_text("🧹 Chat cleared completely.")
    await asyncio.sleep(2)
    try:
        await confirm.delete()
    except:
        pass

async def handle_archive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message
    chat_id = user_msg.chat_id
    document = user_msg.document

    await store_message(chat_id, user_msg.message_id)

    if not document:
        return

    filename = document.file_name.lower()
    if not (filename.endswith(".zip") or filename.endswith(".rar") or filename.endswith(".7z")):
        return

    if document.file_size > MAX_FILE_SIZE:
        error_msg = await user_msg.reply_text("❌ Archive too large (max 50MB)")
        await store_message(chat_id, error_msg.message_id)
        return

    # Download archive
    file = await context.bot.get_file(document.file_id)
    with tempfile.NamedTemporaryFile(suffix=os.path.splitext(document.file_name)[1], delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    context.user_data["current_archive"] = tmp_path
    context.user_data["original_archive_name"] = document.file_name

    keyboard = [
        [
            InlineKeyboardButton("🔄 Convert", callback_data="convert"),
            InlineKeyboardButton("📂 Unzip", callback_data="unzip"),
            InlineKeyboardButton("🧹 Branding Cleaner", callback_data="cleaner"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    prompt_msg = await user_msg.reply_text(
        "✅ Archive received. Choose an action:",
        reply_markup=reply_markup,
    )
    await store_message(chat_id, prompt_msg.message_id)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    action = query.data

    archive_path = context.user_data.get("current_archive")
    original_name = context.user_data.get("original_archive_name", "archive")

    if not archive_path or not os.path.exists(archive_path):
        await query.edit_message_text("❌ Archive not found. Please send the file again.")
        return

    if action == "unzip":
        await query.edit_message_text("📂 Extracting and sending files...")
        extract_dir = tempfile.mkdtemp()
        try:
            # Use native extraction first
            input_type = get_archive_type(archive_path)
            if not extract_archive_native(archive_path, extract_dir, input_type):
                patoolib.extract_archive(archive_path, outdir=extract_dir)

            sent_count = 0
            for root, _, files in os.walk(extract_dir):
                for f in files:
                    file_path = os.path.join(root, f)
                    if os.path.getsize(file_path) > MAX_FILE_SIZE:
                        continue
                    with open(file_path, "rb") as fp:
                        bio = BytesIO(fp.read())
                        bio.name = f
                    sent_msg = await query.message.reply_document(document=bio, filename=f)
                    await store_message(chat_id, sent_msg.message_id)
                    sent_count += 1
                    await asyncio.sleep(0.3)
            if sent_count == 0:
                warn = await query.message.reply_text("⚠️ No valid files found (empty or >50MB).")
                await store_message(chat_id, warn.message_id)
        except Exception as e:
            await query.message.reply_text(f"❌ Extraction failed: {str(e)}")
        finally:
            shutil.rmtree(extract_dir, ignore_errors=True)
            os.unlink(archive_path)
            context.user_data.pop("current_archive", None)
            await query.message.delete()

    elif action == "convert":
        # Show format selection
        keyboard = [
            [
                InlineKeyboardButton("📦 ZIP", callback_data="conv_zip"),
                InlineKeyboardButton("🗜️ RAR", callback_data="conv_rar"),
                InlineKeyboardButton("📀 7Z", callback_data="conv_7z"),
            ],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🔄 **Select target format:**",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )

    elif action.startswith("conv_"):
        target = action.split("_")[1]  # zip, rar, 7z
        if target == "rar" and not is_rar_available():
            await query.edit_message_text(
                "❌ **RAR creation is not available on this server.**\n"
                "Please install `rar` command-line tool or choose ZIP/7z.",
                parse_mode="Markdown",
            )
            return

        await query.edit_message_text(f"🔄 Converting to **{target.upper()}**...")
        progress_msg = await query.message.reply_text("Starting conversion...")
        await store_message(chat_id, progress_msg.message_id)

        output_ext = f".{target}"
        with tempfile.NamedTemporaryFile(suffix=output_ext, delete=False) as out_tmp:
            out_path = out_tmp.name

        success, error_msg = await convert_archive(
            archive_path, out_path, target,
            progress_msg.message_id, chat_id, context
        )
        if success:
            with open(out_path, "rb") as fp:
                bio = BytesIO(fp.read())
                new_name = f"{os.path.splitext(original_name)[0]}.{target}"
                bio.name = new_name
            result_msg = await query.message.reply_document(document=bio, filename=new_name)
            await store_message(chat_id, result_msg.message_id)
            await progress_msg.delete()
            os.unlink(archive_path)
            context.user_data.pop("current_archive", None)
            await query.message.delete()
        else:
            await progress_msg.edit_text(f"❌ Conversion to {target.upper()} failed.\nReason: {error_msg[:200]}")
        os.unlink(out_path)

    elif action == "back_to_menu":
        keyboard = [
            [
                InlineKeyboardButton("🔄 Convert", callback_data="convert"),
                InlineKeyboardButton("📂 Unzip", callback_data="unzip"),
                InlineKeyboardButton("🧹 Branding Cleaner", callback_data="cleaner"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ Archive received. Choose an action:",
            reply_markup=reply_markup,
        )

    elif action == "cleaner":
        await query.edit_message_text("🧹 Cleaning branding from archive...")
        progress_msg = await query.message.reply_text("Starting branding removal...")
        await store_message(chat_id, progress_msg.message_id)

        output_ext = os.path.splitext(archive_path)[1]
        with tempfile.NamedTemporaryFile(suffix=output_ext, delete=False) as out_tmp:
            out_path = out_tmp.name

        success, error_msg = await clean_branding_in_archive(
            archive_path, out_path,
            progress_msg.message_id, chat_id, context
        )
        if success:
            with open(out_path, "rb") as fp:
                bio = BytesIO(fp.read())
                cleaned_name = f"cleaned_{original_name}"
                bio.name = cleaned_name
            result_msg = await query.message.reply_document(document=bio, filename=cleaned_name)
            await store_message(chat_id, result_msg.message_id)
            await progress_msg.delete()
            os.unlink(archive_path)
            context.user_data.pop("current_archive", None)
            await query.message.delete()
        else:
            await progress_msg.edit_text(f"❌ Branding cleaning failed.\nReason: {error_msg[:200]}")

# ================= MAIN =================
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("❌ Please set BOT_TOKEN environment variable.")
        return

    import threading
    threading.Thread(target=run_flask, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clean", clean))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_archive))
    app.add_handler(CallbackQueryHandler(button_callback))

    print("✅ Bot started. Waiting for archives...")
    app.run_polling()

if __name__ == "__main__":
    main()
