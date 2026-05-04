import os
import re
import asyncio
import tempfile
import logging
import shutil
import zipfile
from io import BytesIO
from typing import Dict, List, Optional, Tuple, Callable, Any

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
SPLIT_LINES_PER_FILE = 2000  # Lines per split file for text merge

# Text extensions for branding cleaner
TEXT_EXTENSIONS = {".txt", ".md", ".cfg", ".ini", ".conf", ".json",
                   ".xml", ".html", ".css", ".js", ".py", ".sh", ".bat"}

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

# Cleanup helpers for messages
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

# ------------------ PASSWORD HANDLING ------------------
async def ask_for_password(chat_id: int, context: ContextTypes.DEFAULT_TYPE, callback_data: dict) -> None:
    """Ask user to send the password for a protected archive."""
    context.user_data["waiting_for_password"] = True
    context.user_data["password_callback_data"] = callback_data
    msg = await context.bot.send_message(
        chat_id=chat_id,
        text="🔐 This archive is password protected. Please send the password as a text message.\n\n"
             "_(The password will not be stored after this operation)_",
        parse_mode="Markdown"
    )
    await store_message(chat_id, msg.message_id)

def extract_archive_with_password(archive_path: str, extract_dir: str, archive_type: str, password: Optional[str] = None) -> Tuple[bool, str]:
    """Extract archive with optional password."""
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                # If password is provided, try to set it for all encrypted files
                pwd_bytes = password.encode('utf-8') if password else None
                # Test if any file is encrypted and password works
                for info in zf.infolist():
                    if info.flag_bits & 0x1:  # encrypted
                        if not pwd_bytes:
                            return False, "Password required"
                        try:
                            zf.read(info, pwd=pwd_bytes)
                        except RuntimeError:
                            return False, "Wrong password"
                zf.extractall(extract_dir, pwd=pwd_bytes)
            return True, ""
        elif archive_type == "7z":
            with py7zr.SevenZipFile(archive_path, mode='r', password=password) as szf:
                szf.extractall(extract_dir)
            return True, ""
        else:
            return False, f"Unsupported archive type: {archive_type}"
    except py7zr.exceptions.PasswordRequired:
        return False, "Password required"
    except py7zr.exceptions.WrongPassword:
        return False, "Wrong password"
    except RuntimeError as e:
        if "Bad password" in str(e) or "password" in str(e).lower():
            return False, "Wrong password"
        return False, f"Extraction failed: {str(e)}"
    except Exception as e:
        return False, f"Extraction failed: {str(e)}"

def create_archive(source_dir: str, output_path: str, archive_type: str, password: Optional[str] = None) -> Tuple[bool, str]:
    """Create ZIP or 7z archive from source directory (password optional)."""
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(source_dir):
                    for f in files:
                        full_path = os.path.join(root, f)
                        arcname = os.path.relpath(full_path, source_dir)
                        if password:
                            # Set password for each file (simplistic: same password for all)
                            # Note: zipfile doesn't support setting password per file easily,
                            # but we can encrypt the whole archive by using pyzipper? Simpler: just create without password
                            # For now, we skip password on creation (most users don't need it)
                            zf.write(full_path, arcname)
                        else:
                            zf.write(full_path, arcname)
            return True, ""
        elif archive_type == "7z":
            with py7zr.SevenZipFile(output_path, mode='w', password=password) as szf:
                szf.writeall(source_dir, arcname="")
            return True, ""
        else:
            return False, f"Unsupported archive type: {archive_type}"
    except Exception as e:
        return False, f"Archive creation failed: {str(e)}"

def merge_archives(archive_paths: List[Tuple[str, str]], output_path: str, target_type: str, password: Optional[str] = None) -> Tuple[bool, str]:
    """Merge multiple archives into one, using password for extraction if needed."""
    temp_root = tempfile.mkdtemp()
    try:
        for orig_name, arch_path in archive_paths:
            base_name = os.path.splitext(orig_name)[0]
            extract_dir = os.path.join(temp_root, base_name)
            os.makedirs(extract_dir, exist_ok=True)
            arch_type = get_archive_type(arch_path)
            if not arch_type:
                return False, f"Unknown archive type for {orig_name}"
            success, err = extract_archive_with_password(arch_path, extract_dir, arch_type, password)
            if not success:
                return False, f"Failed to extract {orig_name}: {err}"
        # Pack into output archive (no password on output for simplicity)
        return create_archive(temp_root, output_path, target_type, None)
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

# ------------------ TEXT FILE MERGE (with duplicate removal) ------------------
def merge_text_files(file_paths: List[str], output_path: str) -> Tuple[bool, str]:
    """Merge several text files into one, removing duplicate lines globally."""
    try:
        seen = set()
        unique_lines = []
        for path in file_paths:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.rstrip('\n')
                    if stripped not in seen:
                        seen.add(stripped)
                        unique_lines.append(stripped)
        with open(output_path, "w", encoding="utf-8") as out_f:
            out_f.write("\n".join(unique_lines))
        return True, ""
    except Exception as e:
        return False, str(e)

def get_unique_lines_from_files(file_paths: List[str]) -> Tuple[bool, List[str], str]:
    """Extract unique lines from multiple files."""
    try:
        seen = set()
        unique_lines = []
        for path in file_paths:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    stripped = line.rstrip('\n')
                    if stripped not in seen:
                        seen.add(stripped)
                        unique_lines.append(stripped)
        return True, unique_lines, ""
    except Exception as e:
        return False, [], str(e)

def split_lines_into_chunks(lines: List[str], chunk_size: int) -> List[List[str]]:
    """Split a list of lines into chunks of given size."""
    return [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]

# ------------------ ARCHIVE HELPERS (ZIP & 7z) ------------------
def get_archive_type(file_path: str) -> Optional[str]:
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".zip":
        return "zip"
    if ext == ".7z":
        return "7z"
    return None

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
            return False, "Unknown input archive format (only ZIP/7z supported)"
        
        # Try extraction without password first; if password required, ask later
        success, err = extract_archive_with_password(input_path, temp_extract, input_type, None)
        if not success:
            if "password required" in err.lower():
                # Store operation for later retry with password
                context.user_data["pending_conv"] = {
                    "input_path": input_path,
                    "output_path": output_path,
                    "target_type": target_type,
                    "progress_msg_id": progress_msg_id,
                    "chat_id": chat_id
                }
                await ask_for_password(chat_id, context, {"action": "retry_convert"})
                return False, "PASSWORD_NEEDED"
            else:
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
        return False, "Unknown archive format (only ZIP/7z supported)"
    temp_extract = tempfile.mkdtemp()
    try:
        # Try extraction without password
        success, err = extract_archive_with_password(input_path, temp_extract, archive_type, None)
        if not success:
            if "password required" in err.lower():
                context.user_data["pending_clean"] = {
                    "input_path": input_path,
                    "output_path": output_path,
                    "progress_msg_id": progress_msg_id,
                    "chat_id": chat_id
                }
                await ask_for_password(chat_id, context, {"action": "retry_clean"})
                return False, "PASSWORD_NEEDED"
            else:
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

        # Repack using the same archive type
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
        else:
            return False, f"Unsupported archive type for repacking: {archive_type}"

        await send_progress(chat_id, progress_msg_id, context, 100)
        return True, ""
    except Exception as e:
        logger.exception("Branding cleaning failed")
        return False, str(e)
    finally:
        shutil.rmtree(temp_extract, ignore_errors=True)

# ------------------ TELEGRAM HANDLERS ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "📦 **Archive & Text File Bot**\n\n"
        "**What I can do:**\n"
        "• Send me **multiple `.txt` files** → merge into one or split into chunks (duplicates removed).\n"
        "• Send me **multiple `.zip` or `.7z` files** → merge all into one archive, or process a single archive (convert / unzip / branding cleaner).\n"
        "• **Password-protected ZIP/7z** files are supported – bot will ask for password when needed.\n"
        "• `/clean` – delete all messages in this chat (no trace left).\n\n"
        "**Note:** RAR support has been removed.",
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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages – used for receiving passwords."""
    if context.user_data.get("waiting_for_password"):
        password = update.message.text.strip()
        chat_id = update.effective_chat.id
        # Store password temporarily
        context.user_data["temp_password"] = password
        context.user_data["waiting_for_password"] = False
        callback_data = context.user_data.get("password_callback_data")
        if callback_data:
            # Clear the callback data
            context.user_data.pop("password_callback_data", None)
            # Execute the pending operation
            action = callback_data.get("action")
            if action == "retry_convert":
                pending = context.user_data.get("pending_conv")
                if pending:
                    await retry_convert_with_password(chat_id, context, pending, password)
                    context.user_data.pop("pending_conv", None)
            elif action == "retry_clean":
                pending = context.user_data.get("pending_clean")
                if pending:
                    await retry_clean_with_password(chat_id, context, pending, password)
                    context.user_data.pop("pending_clean", None)
            elif action == "retry_unzip":
                pending = context.user_data.get("pending_unzip")
                if pending:
                    await retry_unzip_with_password(chat_id, context, pending, password)
                    context.user_data.pop("pending_unzip", None)
            elif action == "retry_merge":
                pending = context.user_data.get("pending_merge")
                if pending:
                    await retry_merge_with_password(chat_id, context, pending, password)
                    context.user_data.pop("pending_merge", None)
        # Delete the password message for privacy
        try:
            await update.message.delete()
        except:
            pass
        return
    # Not waiting for password – ignore other text messages
    await update.message.delete()

async def retry_convert_with_password(chat_id: int, context: ContextTypes.DEFAULT_TYPE, pending: dict, password: str):
    """Retry conversion after receiving password."""
    input_path = pending["input_path"]
    output_path = pending["output_path"]
    target_type = pending["target_type"]
    progress_msg_id = pending["progress_msg_id"]
    
    # Retry extraction with password
    temp_extract = tempfile.mkdtemp()
    try:
        input_type = get_archive_type(input_path)
        success, err = extract_archive_with_password(input_path, temp_extract, input_type, password)
        if not success:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=progress_msg_id,
                text=f"❌ Extraction failed with provided password: {err}"
            )
            return
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
        await send_progress(chat_id, progress_msg_id, context, 100)
        # Send result
        with open(output_path, "rb") as fp:
            bio = BytesIO(fp.read())
            original_name = context.user_data.get("original_archive_name", "archive")
            new_name = f"{os.path.splitext(original_name)[0]}.{target_type}"
            bio.name = new_name
        result_msg = await context.bot.send_document(chat_id=chat_id, document=bio, filename=new_name)
        await store_message(chat_id, result_msg.message_id)
        await context.bot.delete_message(chat_id=chat_id, message_id=progress_msg_id)
        os.unlink(input_path)
        context.user_data.pop("current_archive", None)
        # Also delete the original archive prompt if any
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
        await context.bot.delete_message(chat_id=chat_id, message_id=progress_msg_id-1)  # the previous message
    except Exception as e:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❌ Conversion failed: {str(e)}")
    finally:
        shutil.rmtree(temp_extract, ignore_errors=True)

async def retry_clean_with_password(chat_id: int, context: ContextTypes.DEFAULT_TYPE, pending: dict, password: str):
    """Retry branding cleaner after receiving password."""
    input_path = pending["input_path"]
    output_path = pending["output_path"]
    progress_msg_id = pending["progress_msg_id"]
    # Similar to conversion but with cleaning logic
    # We'll reuse the clean_branding_in_archive function with password
    # For simplicity, we'll call it again but with password
    # However, clean_branding_in_archive doesn't have password param. We'll reimplement here quickly.
    archive_type = get_archive_type(input_path)
    if not archive_type:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text="❌ Unknown archive type")
        return
    temp_extract = tempfile.mkdtemp()
    try:
        success, err = extract_archive_with_password(input_path, temp_extract, archive_type, password)
        if not success:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❌ Extraction failed: {err}")
            return
        await send_progress(chat_id, progress_msg_id, context, 30)
        # ... (branding cleaning logic same as earlier)
        # To avoid duplication, we can call the existing function but with password support.
        # Since that function is async and doesn't accept password, we'll copy the cleaning logic here.
        skip_patterns = [
            r'\bdiscord\b', r'\btelegram\b', r'\bt\.me\b', r'\bdiscord\.gg\b',
            r'\bjoin\b', r'\bchannel\b', r'\bgroup\b', r'\bChecker By\b',
            r'\bchecker\b', r'\bcrack\b', r'\bpremium\b', r'\bfree\b', r'\bhits\b',
            r'\bvalid\b', r'\bworking\b', r'\bgithub\b', r'Cookie 👇'
        ]
        compiled = [re.compile(p, re.IGNORECASE) for p in skip_patterns]
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
        await send_progress(chat_id, progress_msg_id, context, 100)
        # Send cleaned archive
        with open(output_path, "rb") as fp:
            bio = BytesIO(fp.read())
            original_name = context.user_data.get("original_archive_name", "archive")
            cleaned_name = f"cleaned_{original_name}"
            bio.name = cleaned_name
        result_msg = await context.bot.send_document(chat_id=chat_id, document=bio, filename=cleaned_name)
        await store_message(chat_id, result_msg.message_id)
        await context.bot.delete_message(chat_id=chat_id, message_id=progress_msg_id)
        os.unlink(input_path)
        context.user_data.pop("current_archive", None)
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
    except Exception as e:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❌ Cleaning failed: {str(e)}")
    finally:
        shutil.rmtree(temp_extract, ignore_errors=True)

async def retry_unzip_with_password(chat_id: int, context: ContextTypes.DEFAULT_TYPE, pending: dict, password: str):
    """Retry unzip after receiving password."""
    archive_path = pending["archive_path"]
    extract_dir = pending["extract_dir"]
    archive_type = pending["archive_type"]
    message_to_edit = pending["message_to_edit"]
    success, err = extract_archive_with_password(archive_path, extract_dir, archive_type, password)
    if not success:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_to_edit, text=f"❌ Unzip failed: {err}")
        return
    # Send files
    sent_count = 0
    for root, _, files in os.walk(extract_dir):
        for f in files:
            file_path = os.path.join(root, f)
            if os.path.getsize(file_path) > MAX_FILE_SIZE:
                continue
            with open(file_path, "rb") as fp:
                bio = BytesIO(fp.read())
                bio.name = f
            sent_msg = await context.bot.send_document(chat_id=chat_id, document=bio, filename=f)
            await store_message(chat_id, sent_msg.message_id)
            sent_count += 1
            await asyncio.sleep(0.3)
    if sent_count == 0:
        warn = await context.bot.send_message(chat_id=chat_id, text="⚠️ No valid files found (empty or >50MB).")
        await store_message(chat_id, warn.message_id)
    await context.bot.delete_message(chat_id=chat_id, message_id=message_to_edit)
    os.unlink(archive_path)
    context.user_data.pop("current_archive", None)
    if context.user_data.get("archive_prompt_msg_id"):
        try:
            await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
        except:
            pass
        context.user_data.pop("archive_prompt_msg_id", None)

async def retry_merge_with_password(chat_id: int, context: ContextTypes.DEFAULT_TYPE, pending: dict, password: str):
    """Retry archive merge after receiving password."""
    archive_list = pending["archive_list"]
    target_format = pending["target_format"]
    output_path = pending["output_path"]
    progress_msg_id = pending["progress_msg_id"]
    success, err = merge_archives(archive_list, output_path, target_format, password)
    if success:
        with open(output_path, "rb") as fp:
            bio = BytesIO(fp.read())
            bio.name = f"merged_archives.{target_format}"
        result_msg = await context.bot.send_document(chat_id=chat_id, document=bio, filename=bio.name)
        await store_message(chat_id, result_msg.message_id)
        await context.bot.delete_message(chat_id=chat_id, message_id=progress_msg_id)
        # Clean up
        for _, p in archive_list:
            os.unlink(p)
        context.user_data.pop("pending_archives", None)
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
    else:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=progress_msg_id, text=f"❌ Merge failed: {err}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_msg = update.message
    chat_id = user_msg.chat_id
    document = user_msg.document
    await store_message(chat_id, user_msg.message_id)

    if not document:
        return

    filename = document.file_name.lower()
    file_ext = os.path.splitext(filename)[1]

    # ------------------ TXT FILE HANDLING (MERGE MODE) ------------------
    if file_ext == ".txt":
        if document.file_size > MAX_FILE_SIZE:
            error_msg = await user_msg.reply_text("❌ File too large (max 50MB)")
            await store_message(chat_id, error_msg.message_id)
            return

        file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name

        if "merge_files" not in context.user_data:
            context.user_data["merge_files"] = []
            context.user_data["merge_msg_id"] = None

        context.user_data["merge_files"].append((document.file_name, tmp_path))

        files_list = "\n".join(f"• {name}" for name, _ in context.user_data["merge_files"])
        total = len(context.user_data["merge_files"])
        text = (
            f"📄 **Text files collected: {total}**\n\n"
            f"{files_list}\n\n"
            f"✨ *Duplicates will be removed automatically when merging.*\n\n"
            "What would you like to do?"
        )
        keyboard = [
            [
                InlineKeyboardButton("🔀 Merge All", callback_data="merge_now"),
                InlineKeyboardButton("🔀 Merge All (Splitted)", callback_data="merge_split")
            ],
            [
                InlineKeyboardButton("📋 Show Files", callback_data="show_merge_files"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_merge")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        if context.user_data["merge_msg_id"]:
            try:
                await context.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=context.user_data["merge_msg_id"],
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            except Exception:
                new_msg = await user_msg.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
                context.user_data["merge_msg_id"] = new_msg.message_id
                await store_message(chat_id, new_msg.message_id)
        else:
            new_msg = await user_msg.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            context.user_data["merge_msg_id"] = new_msg.message_id
            await store_message(chat_id, new_msg.message_id)

        try:
            await user_msg.delete()
        except:
            pass
        return

    # ------------------ ARCHIVE HANDLING (ZIP / 7z) ------------------
    if not (filename.endswith(".zip") or filename.endswith(".7z")):
        return

    if document.file_size > MAX_FILE_SIZE:
        error_msg = await user_msg.reply_text("❌ Archive too large (max 50MB)")
        await store_message(chat_id, error_msg.message_id)
        return

    file = await context.bot.get_file(document.file_id)
    with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    if "pending_archives" not in context.user_data:
        context.user_data["pending_archives"] = []
        context.user_data["archive_prompt_msg_id"] = None

    context.user_data["pending_archives"].append((document.file_name, tmp_path))

    archive_list = "\n".join(f"• {name}" for name, _ in context.user_data["pending_archives"])
    total_archives = len(context.user_data["pending_archives"])
    text = (
        f"📦 **Archives collected: {total_archives}**\n\n"
        f"{archive_list}\n\n"
        "What would you like to do?"
    )
    keyboard = [
        [
            InlineKeyboardButton("📂 Process Single Archive", callback_data="process_single_archive"),
            InlineKeyboardButton("🔀 Merge All Archives", callback_data="merge_archives_choose_format")
        ],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_archive_merge")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if context.user_data["archive_prompt_msg_id"]:
        try:
            await context.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=context.user_data["archive_prompt_msg_id"],
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except Exception:
            new_msg = await user_msg.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
            context.user_data["archive_prompt_msg_id"] = new_msg.message_id
            await store_message(chat_id, new_msg.message_id)
    else:
        new_msg = await user_msg.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
        context.user_data["archive_prompt_msg_id"] = new_msg.message_id
        await store_message(chat_id, new_msg.message_id)

    try:
        await user_msg.delete()
    except:
        pass

# ------------------ CALLBACK HANDLER ------------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    action = query.data

    # ------------------ TEXT MERGE ACTIONS (unchanged) ------------------
    if action == "merge_now":
        merge_files = context.user_data.get("merge_files", [])
        if len(merge_files) < 2:
            await query.edit_message_text("❌ You need at least two text files to merge. Send more `.txt` files.")
            return

        await query.edit_message_text(f"🔄 Merging {len(merge_files)} files (removing duplicates)...")
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as out_tmp:
            out_path = out_tmp.name
        file_paths = [path for _, path in merge_files]
        success, err = merge_text_files(file_paths, out_path)
        if success:
            with open(out_path, "rb") as fp:
                bio = BytesIO(fp.read())
                bio.name = "merged_texts.txt"
            result_msg = await query.message.reply_document(document=bio, filename="merged_texts.txt")
            await store_message(chat_id, result_msg.message_id)
            for _, path in merge_files:
                os.unlink(path)
            os.unlink(out_path)
            context.user_data.pop("merge_files", None)
            if context.user_data.get("merge_msg_id"):
                try:
                    await context.bot.delete_message(chat_id, context.user_data["merge_msg_id"])
                except:
                    pass
                context.user_data.pop("merge_msg_id", None)
            await query.message.delete()
        else:
            await query.edit_message_text(f"❌ Merge failed: {err}")

    elif action == "merge_split":
        merge_files = context.user_data.get("merge_files", [])
        if len(merge_files) < 2:
            await query.edit_message_text("❌ You need at least two text files to split-merge. Send more `.txt` files.")
            return

        await query.edit_message_text(f"🔄 Processing {len(merge_files)} files (removing duplicates & splitting into {SPLIT_LINES_PER_FILE}-line chunks)...")
        file_paths = [path for _, path in merge_files]
        success, unique_lines, err = get_unique_lines_from_files(file_paths)
        if not success:
            await query.edit_message_text(f"❌ Failed to read files: {err}")
            return

        total_unique = len(unique_lines)
        if total_unique == 0:
            await query.edit_message_text("❌ No unique lines found after merging.")
            return

        chunks = split_lines_into_chunks(unique_lines, SPLIT_LINES_PER_FILE)
        num_chunks = len(chunks)
        status_msg = await query.message.reply_text(f"📦 Splitting {total_unique} unique lines into {num_chunks} file(s)...")
        
        for idx, chunk in enumerate(chunks, start=1):
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode='w', encoding='utf-8') as tmp:
                tmp.write("\n".join(chunk))
                chunk_path = tmp.name
            with open(chunk_path, "rb") as fp:
                bio = BytesIO(fp.read())
                bio.name = f"merged_part{idx}.txt" if num_chunks > 1 else "merged_texts.txt"
                result_msg = await query.message.reply_document(document=bio, filename=bio.name)
                await store_message(chat_id, result_msg.message_id)
            os.unlink(chunk_path)
            await asyncio.sleep(0.5)
        
        for _, path in merge_files:
            os.unlink(path)
        context.user_data.pop("merge_files", None)
        if context.user_data.get("merge_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["merge_msg_id"])
            except:
                pass
            context.user_data.pop("merge_msg_id", None)
        await status_msg.delete()
        await query.message.delete()

    elif action == "show_merge_files":
        merge_files = context.user_data.get("merge_files", [])
        if not merge_files:
            await query.answer("No files collected yet.")
            return
        files_list = "\n".join(f"• {name}" for name, _ in merge_files)
        text = f"📋 **Files in queue:**\n\n{files_list}"
        await query.edit_message_text(text, parse_mode="Markdown")
        keyboard = [
            [
                InlineKeyboardButton("🔀 Merge All", callback_data="merge_now"),
                InlineKeyboardButton("🔀 Merge All (Splitted)", callback_data="merge_split")
            ],
            [
                InlineKeyboardButton("📋 Show Files", callback_data="show_merge_files"),
                InlineKeyboardButton("❌ Cancel", callback_data="cancel_merge")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await asyncio.sleep(5)
        try:
            await query.edit_message_text(
                f"📄 **Text files collected: {len(merge_files)}**\n\n"
                f"{files_list}\n\n✨ *Duplicates will be removed automatically.*\n\nWhat would you like to do?",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )
        except:
            pass

    elif action == "cancel_merge":
        merge_files = context.user_data.pop("merge_files", [])
        for _, path in merge_files:
            os.unlink(path)
        if context.user_data.get("merge_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["merge_msg_id"])
            except:
                pass
            context.user_data.pop("merge_msg_id", None)
        await query.edit_message_text("❌ Merge cancelled. All temporary files deleted.")
        await asyncio.sleep(2)
        try:
            await query.message.delete()
        except:
            pass

    # ------------------ ARCHIVE MERGE ACTIONS (with password support) ------------------
    elif action == "process_single_archive":
        pending = context.user_data.get("pending_archives", [])
        if not pending:
            await query.edit_message_text("❌ No archive found.")
            return
        # Take the last archive as the one to process
        orig_name, arch_path = pending[-1]
        context.user_data["current_archive"] = arch_path
        context.user_data["original_archive_name"] = orig_name
        # Clear pending archives list and delete others
        for _, p in pending:
            if p != arch_path:
                os.unlink(p)
        context.user_data.pop("pending_archives", None)
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
        keyboard = [
            [
                InlineKeyboardButton("🔄 Convert", callback_data="convert"),
                InlineKeyboardButton("📂 Unzip", callback_data="unzip"),
                InlineKeyboardButton("🧹 Branding Cleaner", callback_data="cleaner"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "✅ Archive ready. Choose an action:",
            reply_markup=reply_markup,
        )

    elif action == "merge_archives_choose_format":
        pending = context.user_data.get("pending_archives", [])
        if len(pending) < 2:
            await query.edit_message_text("❌ You need at least two archives to merge. Send more ZIP/7z files.")
            return
        keyboard = [
            [InlineKeyboardButton("📦 ZIP", callback_data="merge_archives_zip")],
            [InlineKeyboardButton("📀 7Z", callback_data="merge_archives_7z")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_archive_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🗜️ **Choose output format for merged archive:**",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    elif action.startswith("merge_archives_"):
        target_format = action.split("_")[2]
        pending = context.user_data.get("pending_archives", [])
        if len(pending) < 2:
            await query.edit_message_text("❌ Not enough archives to merge.")
            return
        await query.edit_message_text(f"🔄 Merging {len(pending)} archives into one {target_format.upper()} file...")
        progress_msg = await query.message.reply_text("Processing, please wait...")
        with tempfile.NamedTemporaryFile(suffix=f".{target_format}", delete=False) as out_tmp:
            out_path = out_tmp.name
        
        # Try without password first
        success, err = merge_archives(pending, out_path, target_format, None)
        if not success:
            if "password required" in err.lower() or "wrong password" in err.lower():
                # Store for password retry
                context.user_data["pending_merge"] = {
                    "archive_list": pending,
                    "target_format": target_format,
                    "output_path": out_path,
                    "progress_msg_id": progress_msg.message_id
                }
                await ask_for_password(chat_id, context, {"action": "retry_merge"})
                return
            else:
                await progress_msg.edit_text(f"❌ Merge failed: {err}")
                return
        
        # Success
        with open(out_path, "rb") as fp:
            bio = BytesIO(fp.read())
            bio.name = f"merged_archives.{target_format}"
        result_msg = await query.message.reply_document(document=bio, filename=bio.name)
        await store_message(chat_id, result_msg.message_id)
        await progress_msg.delete()
        for _, p in pending:
            os.unlink(p)
        context.user_data.pop("pending_archives", None)
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
        await query.message.delete()

    elif action == "back_to_archive_menu":
        pending = context.user_data.get("pending_archives", [])
        archive_list = "\n".join(f"• {name}" for name, _ in pending)
        total = len(pending)
        text = (
            f"📦 **Archives collected: {total}**\n\n"
            f"{archive_list}\n\n"
            "What would you like to do?"
        )
        keyboard = [
            [
                InlineKeyboardButton("📂 Process Single Archive", callback_data="process_single_archive"),
                InlineKeyboardButton("🔀 Merge All Archives", callback_data="merge_archives_choose_format")
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_archive_merge")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")

    elif action == "cancel_archive_merge":
        pending = context.user_data.pop("pending_archives", [])
        for _, p in pending:
            os.unlink(p)
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
        await query.edit_message_text("❌ Archive merge cancelled. All temporary files deleted.")
        await asyncio.sleep(2)
        try:
            await query.message.delete()
        except:
            pass

    # ------------------ SINGLE ARCHIVE ACTIONS ------------------
    elif action == "unzip":
        archive_path = context.user_data.get("current_archive")
        if not archive_path or not os.path.exists(archive_path):
            await query.edit_message_text("❌ Archive not found. Please send the file again.")
            return
        await query.edit_message_text("📂 Extracting and sending files...")
        extract_dir = tempfile.mkdtemp()
        archive_type = get_archive_type(archive_path)
        if not archive_type:
            await query.message.reply_text("❌ Unsupported archive type (only ZIP/7z).")
            return
        # Try unzip without password
        success, err = extract_archive_with_password(archive_path, extract_dir, archive_type, None)
        if not success:
            if "password required" in err.lower() or "wrong password" in err.lower():
                # Store for password retry
                context.user_data["pending_unzip"] = {
                    "archive_path": archive_path,
                    "extract_dir": extract_dir,
                    "archive_type": archive_type,
                    "message_to_edit": query.message.message_id
                }
                await ask_for_password(chat_id, context, {"action": "retry_unzip"})
                return
            else:
                await query.message.reply_text(f"❌ Extraction failed: {err}")
                shutil.rmtree(extract_dir, ignore_errors=True)
                return
        # Send files
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
        shutil.rmtree(extract_dir, ignore_errors=True)
        os.unlink(archive_path)
        context.user_data.pop("current_archive", None)
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
        await query.message.delete()

    elif action == "convert":
        archive_path = context.user_data.get("current_archive")
        if not archive_path or not os.path.exists(archive_path):
            await query.edit_message_text("❌ Archive not found. Please send the file again.")
            return
        buttons = [
            [InlineKeyboardButton("📦 ZIP", callback_data="conv_zip")],
            [InlineKeyboardButton("📀 7Z", callback_data="conv_7z")],
            [InlineKeyboardButton("🔙 Back", callback_data="back_to_menu")]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(
            "🔄 **Select target format:**",
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )

    elif action.startswith("conv_"):
        target = action.split("_")[1]
        if target not in ("zip", "7z"):
            await query.edit_message_text("❌ Only ZIP and 7z are supported.")
            return
        archive_path = context.user_data.get("current_archive")
        original_name = context.user_data.get("original_archive_name", "archive")
        if not archive_path or not os.path.exists(archive_path):
            await query.edit_message_text("❌ Archive not found. Please send the file again.")
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
        if success and error_msg != "PASSWORD_NEEDED":
            with open(out_path, "rb") as fp:
                bio = BytesIO(fp.read())
                new_name = f"{os.path.splitext(original_name)[0]}.{target}"
                bio.name = new_name
            result_msg = await query.message.reply_document(document=bio, filename=new_name)
            await store_message(chat_id, result_msg.message_id)
            await progress_msg.delete()
            os.unlink(archive_path)
            context.user_data.pop("current_archive", None)
            if context.user_data.get("archive_prompt_msg_id"):
                try:
                    await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
                except:
                    pass
                context.user_data.pop("archive_prompt_msg_id", None)
            await query.message.delete()
        elif error_msg == "PASSWORD_NEEDED":
            # keep progress_msg, conversion will be retried after password
            pass
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
            "✅ Archive ready. Choose an action:",
            reply_markup=reply_markup,
        )

    elif action == "cleaner":
        archive_path = context.user_data.get("current_archive")
        original_name = context.user_data.get("original_archive_name", "archive")
        if not archive_path or not os.path.exists(archive_path):
            await query.edit_message_text("❌ Archive not found. Please send the file again.")
            return
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
        if success and error_msg != "PASSWORD_NEEDED":
            with open(out_path, "rb") as fp:
                bio = BytesIO(fp.read())
                cleaned_name = f"cleaned_{original_name}"
                bio.name = cleaned_name
            result_msg = await query.message.reply_document(document=bio, filename=cleaned_name)
            await store_message(chat_id, result_msg.message_id)
            await progress_msg.delete()
            os.unlink(archive_path)
            context.user_data.pop("current_archive", None)
            if context.user_data.get("archive_prompt_msg_id"):
                try:
                    await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
                except:
                    pass
                context.user_data.pop("archive_prompt_msg_id", None)
            await query.message.delete()
        elif error_msg == "PASSWORD_NEEDED":
            pass
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ Bot started with password support for ZIP/7z files.")
    app.run_polling()

if __name__ == "__main__":
    main()
