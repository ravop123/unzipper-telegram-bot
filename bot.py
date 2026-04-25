import os
import re
import asyncio
import tempfile
import logging
import shutil
import zipfile
import subprocess
import urllib.request
import tarfile
import stat
from io import BytesIO
from typing import Dict, List, Optional, Tuple

import patoolib
import py7zr
import rarfile
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

# Where we store downloaded RAR binaries (writable on Render)
BIN_DIR = os.path.join(os.getcwd(), "bin")
os.makedirs(BIN_DIR, exist_ok=True)

# URLs for official RARLAB binaries (Linux x64)
UNRAR_URL = "https://www.rarlab.com/rar/unrar-6.2.12.tar.gz"
RAR_URL = "https://www.rarlab.com/rar/rarlinux-x64-6.2.12.tar.gz"

TEXT_EXTENSIONS = {".txt", ".md", ".cfg", ".ini", ".conf", ".json", ".xml", ".html", ".css", ".js", ".py", ".sh", ".bat"}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running"

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

chat_messages: Dict[int, List[int]] = {}

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

# ------------------ RAR BINARY DOWNLOADER ------------------
def download_and_extract_tar_gz(url: str, dest_dir: str, binary_name: str) -> Optional[str]:
    """Download tar.gz, extract, and return path to binary."""
    try:
        archive_name = os.path.join(dest_dir, os.path.basename(url))
        # Download if not already present
        if not os.path.exists(archive_name):
            logger.info(f"Downloading {url} ...")
            urllib.request.urlretrieve(url, archive_name)
        # Extract
        with tarfile.open(archive_name, "r:gz") as tar:
            tar.extractall(dest_dir)
        # Find binary (usually inside a folder)
        extracted_dir = os.path.join(dest_dir, os.path.splitext(os.path.basename(url))[0].replace(".tar", ""))
        if not os.path.exists(extracted_dir):
            # fallback: scan for binary in dest_dir
            for root, _, files in os.walk(dest_dir):
                if binary_name in files:
                    return os.path.join(root, binary_name)
        # Typical structure: unrar -> unrar executable
        candidate = os.path.join(extracted_dir, binary_name)
        if os.path.exists(candidate):
            # Make executable
            st = os.stat(candidate)
            os.chmod(candidate, st.st_mode | stat.S_IEXEC)
            return candidate
        return None
    except Exception as e:
        logger.error(f"Failed to download/extract RAR tool: {e}")
        return None

def ensure_unrar() -> Optional[str]:
    unrar_path = os.path.join(BIN_DIR, "unrar")
    if os.path.exists(unrar_path):
        return unrar_path
    # Download and extract
    path = download_and_extract_tar_gz(UNRAR_URL, BIN_DIR, "unrar")
    if path:
        # symlink or move to expected path
        shutil.copy(path, unrar_path)
        os.chmod(unrar_path, os.stat(unrar_path).st_mode | stat.S_IEXEC)
        return unrar_path
    return None

def ensure_rar() -> Optional[str]:
    rar_path = os.path.join(BIN_DIR, "rar")
    if os.path.exists(rar_path):
        return rar_path
    path = download_and_extract_tar_gz(RAR_URL, BIN_DIR, "rar")
    if path:
        shutil.copy(path, rar_path)
        os.chmod(rar_path, os.stat(rar_path).st_mode | stat.S_IEXEC)
        return rar_path
    return None

# Configure rarfile and patool to use our binaries
UNRAR_BIN = ensure_unrar()
RAR_BIN = ensure_rar()

if UNRAR_BIN:
    rarfile.UNRAR_TOOL = UNRAR_BIN
    logger.info(f"✅ unrar configured at {UNRAR_BIN}")
else:
    logger.warning("⚠️ Could not setup unrar – RAR extraction may fail")

if RAR_BIN:
    # Patch patoolib to use our rar binary for creation
    from patoolib import programs
    programs.Programs['rar'].archive_cmd = [RAR_BIN, 'a', '-r', '-ep1']
    programs.Programs['rar'].extract_cmd = [UNRAR_BIN, 'x', '-o+']
    logger.info(f"✅ rar configured at {RAR_BIN}")
else:
    logger.warning("⚠️ Could not setup rar – RAR creation will be disabled")

def is_rar_writer_available() -> bool:
    return RAR_BIN is not None and os.path.exists(RAR_BIN)

def get_archive_type(file_path: str) -> Optional[str]:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".zip":
        return "zip"
    if ext == ".rar":
        return "rar"
    if ext == ".7z":
        return "7z"
    return None

def extract_archive_robust(archive_path: str, extract_dir: str, archive_type: str) -> Tuple[bool, str]:
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(extract_dir)
            return True, ""
        elif archive_type == "7z":
            with py7zr.SevenZipFile(archive_path, "r") as szf:
                szf.extractall(extract_dir)
            return True, ""
        elif archive_type == "rar":
            # Use rarfile with our patched UNRAR_TOOL
            with rarfile.RarFile(archive_path, "r") as rf:
                rf.extractall(extract_dir)
            return True, ""
        else:
            return False, f"Unsupported archive type: {archive_type}"
    except rarfile.RarCannotExec as e:
        # fallback to patoolib if rarfile fails (RAR5 compatibility)
        try:
            from patoolib import extract_archive
            extract_archive(archive_path, outdir=extract_dir)
            return True, ""
        except Exception as e2:
            return False, f"RAR extraction failed: {str(e2)}"
    except Exception as e:
        return False, f"Extraction failed: {str(e)}"

async def send_progress(chat_id: int, message_id: int, context: ContextTypes.DEFAULT_TYPE, percent: int):
    bar_length = 20
    filled = int(bar_length * percent / 100)
    bar = "█" * filled + "░" * (bar_length - filled)
    text = f"🔄 Processing...\n`[{bar}] {percent}%`"
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, parse_mode="Markdown"
        )
    except Exception:
        pass

async def process_cookie_txt(txt_path: str, output_zip_path: str) -> Tuple[bool, str]:
    try:
        with open(txt_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = [line.strip() for line in f if line.strip()]
        if not lines:
            return False, "File is empty"
        seen = set()
        unique_lines = []
        for line in lines:
            if line not in seen:
                seen.add(line)
                unique_lines.append(line)
        temp_dir = tempfile.mkdtemp()
        for idx, cookie in enumerate(unique_lines, start=1):
            cookie_file = os.path.join(temp_dir, f"cookie_{idx}.txt")
            with open(cookie_file, "w", encoding="utf-8") as cf:
                cf.write(cookie)
        with zipfile.ZipFile(output_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    full_path = os.path.join(root, f)
                    arcname = os.path.relpath(full_path, temp_dir)
                    zf.write(full_path, arcname)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return True, ""
    except Exception as e:
        logger.exception("Cookie splitting failed")
        return False, str(e)

async def convert_archive(
    input_path: str,
    output_path: str,
    target_type: str,
    progress_msg_id: int,
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
) -> Tuple[bool, str]:
    temp_extract = tempfile.mkdtemp()
    try:
        input_type = get_archive_type(input_path)
        if not input_type:
            return False, "Unknown input archive format"
        success, err = extract_archive_robust(input_path, temp_extract, input_type)
        if not success:
            return False, f"Extraction failed: {err}"
        await send_progress(chat_id, progress_msg_id, context, 30)
        total_files = sum(1 for root, _, files in os.walk(temp_extract) for f in files)
        processed = 0
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
            if not is_rar_writer_available():
                return False, "RAR command not available – cannot create RAR archive (use ZIP or 7z)"
            # Use patoolib with pre-configured rar binary
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
) -> Tuple[bool, str]:
    skip_patterns = [
        r'\bdiscord\b', r'\btelegram\b', r'\bt\.me\b', r'\bdiscord\.gg\b',
        r'\bjoin\b', r'\bchannel\b', r'\bgroup\b', r'\bChecker By\b',
        r'\bchecker\b', r'\bcrack\b', r'\bpremium\b', r'\bfree\b', r'\bhits\b',
        r'\bvalid\b', r'\bworking\b', r'\bgithub\b', r'Cookie 👇'
    ]
    compiled = [re.compile(p, re.IGNORECASE) for p in skip_patterns]
    archive_type = get_archive_type(input_path)
    if not archive_type:
        return False, "Unknown archive format"
    temp_extract = tempfile.mkdtemp()
    try:
        success, err = extract_archive_robust(input_path, temp_extract, archive_type)
        if not success:
            return False, f"Extraction failed: {err}"
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
                            if re.search(r"cookie", line, re.IGNORECASE):
                                new_lines.append(line.rstrip('\n'))
                                continue
                            if re.search(r'=[^;]+;', line) and '.' in line:
                                new_lines.append(line.rstrip('\n'))
                                continue
                            if any(p.search(line) for p in compiled):
                                modified_any = True
                                continue
                            new_lines.append(line.rstrip('\n'))
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
        if not modified_any:
            join_file = os.path.join(temp_extract, "JOIN_CHANNEL.txt")
            with open(join_file, "w", encoding="utf-8") as f:
                f.write(CHANNEL_LINK)
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
            if not is_rar_writer_available():
                return False, "RAR creation not available – cannot repack cleaned archive as RAR (use ZIP or 7z)"
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

# ------------------ TELEGRAM HANDLERS (unchanged except import note) ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "📦 **Archive Extractor & Converter Bot**\n\n"
        "**What I can do:**\n"
        "• Send me a `.zip`, `.rar` or `.7z` → choose Convert / Unzip / Branding Cleaner\n"
        "• Send me a `.txt` file containing cookies (one per line) → I'll ask if you want to split each cookie into a separate `.txt` file and send a ZIP.\n"
        "• `/clean` – delete all messages in this chat (no trace left).\n\n"
        "**Now with FULL RAR support** (including RAR5) – thanks to built-in binaries.",
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

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message
    chat_id = user_msg.chat_id
    document = user_msg.document
    await store_message(chat_id, user_msg.message_id)
    if not document:
        return
    filename = document.file_name.lower()
    file_ext = os.path.splitext(filename)[1]
    if file_ext == ".txt":
        if document.file_size > MAX_FILE_SIZE:
            error_msg = await user_msg.reply_text("❌ File too large (max 50MB)")
            await store_message(chat_id, error_msg.message_id)
            return
        file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            txt_path = tmp.name
        context.user_data["cookie_txt_path"] = txt_path
        keyboard = [[
            InlineKeyboardButton("✅ Split Cookies", callback_data="split_cookies"),
            InlineKeyboardButton("❌ Ignore", callback_data="ignore_cookie"),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        prompt_msg = await user_msg.reply_text(
            "📄 **Cookie file received.**\nDo you want to split each cookie into a separate .txt file and receive a ZIP?",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        await store_message(chat_id, prompt_msg.message_id)
        return
    if not (filename.endswith(".zip") or filename.endswith(".rar") or filename.endswith(".7z")):
        return
    if document.file_size > MAX_FILE_SIZE:
        error_msg = await user_msg.reply_text("❌ Archive too large (max 50MB)")
        await store_message(chat_id, error_msg.message_id)
        return
    file = await context.bot.get_file(document.file_id)
    with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
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
    if action == "split_cookies":
        txt_path = context.user_data.get("cookie_txt_path")
        if not txt_path or not os.path.exists(txt_path):
            await query.edit_message_text("❌ Cookie file not found. Please send again.")
            return
        await query.edit_message_text("🔄 Splitting cookies into separate files...")
        progress_msg = await query.message.reply_text("Processing...")
        await store_message(chat_id, progress_msg.message_id)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as zip_tmp:
            zip_path = zip_tmp.name
        success, error = await process_cookie_txt(txt_path, zip_path)
        if success:
            with open(zip_path, "rb") as fp:
                bio = BytesIO(fp.read())
                bio.name = "cookies_split.zip"
            result_msg = await query.message.reply_document(document=bio, filename="cookies_split.zip")
            await store_message(chat_id, result_msg.message_id)
            await progress_msg.delete()
            os.unlink(txt_path)
            os.unlink(zip_path)
            context.user_data.pop("cookie_txt_path", None)
            await query.message.delete()
        else:
            await progress_msg.edit_text(f"❌ Failed: {error}")
            os.unlink(txt_path)
            context.user_data.pop("cookie_txt_path", None)
        return
    elif action == "ignore_cookie":
        txt_path = context.user_data.get("cookie_txt_path")
        if txt_path and os.path.exists(txt_path):
            os.unlink(txt_path)
        context.user_data.pop("cookie_txt_path", None)
        await query.edit_message_text("❌ Cookie processing cancelled.")
        await asyncio.sleep(2)
        try:
            await query.message.delete()
        except:
            pass
        return
    archive_path = context.user_data.get("current_archive")
    original_name = context.user_data.get("original_archive_name", "archive")
    if not archive_path or not os.path.exists(archive_path):
        await query.edit_message_text("❌ Archive not found. Please send the file again.")
        return
    if action == "unzip":
        await query.edit_message_text("📂 Extracting and sending files...")
        extract_dir = tempfile.mkdtemp()
        try:
            archive_type = get_archive_type(archive_path)
            success, err = extract_archive_robust(archive_path, extract_dir, archive_type)
            if not success:
                await query.message.reply_text(f"❌ Extraction failed: {err}")
                return
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
        buttons = [
            InlineKeyboardButton("📦 ZIP", callback_data="conv_zip"),
            InlineKeyboardButton("📀 7Z", callback_data="conv_7z"),
        ]
        if is_rar_writer_available():
            buttons.insert(1, InlineKeyboardButton("🗜️ RAR", callback_data="conv_rar"))
        buttons_row = [buttons]
        buttons_row.append([InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")])
        reply_markup = InlineKeyboardMarkup(buttons_row)
        await query.edit_message_text(
            "🔄 **Select target format:**",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
    elif action.startswith("conv_"):
        target = action.split("_")[1]
        if target == "rar" and not is_rar_writer_available():
            await query.edit_message_text(
                "❌ **RAR creation is not available – but RAR extraction works fine.**",
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
            await progress_msg.edit_text(f"❌ Conversion to {target.upper()} failed.\n{error_msg[:200]}")
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
            await progress_msg.edit_text(f"❌ Branding cleaning failed.\n{error_msg[:200]}")

# ------------------ MAIN ------------------
def main():
    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN":
        print("❌ Please set BOT_TOKEN environment variable.")
        return
    import threading
    threading.Thread(target=run_flask, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clean", clean))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ Bot started. FULL RAR SUPPORT ACTIVE (extraction and creation).")
    if not is_rar_writer_available():
        print("⚠️ RAR creation disabled (no rar binary) – but extraction works.")
    app.run_polling()

if __name__ == "__main__":
    main()
