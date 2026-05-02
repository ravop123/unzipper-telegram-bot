import os
import re
import asyncio
import tempfile
import logging
import shutil
import zipfile
from io import BytesIO
from typing import Dict, List, Optional, Tuple

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

def extract_archive(archive_path: str, extract_dir: str, archive_type: str) -> Tuple[bool, str]:
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(extract_dir)
        elif archive_type == "7z":
            with py7zr.SevenZipFile(archive_path, "r") as szf:
                szf.extractall(extract_dir)
        else:
            return False, f"Unsupported archive type: {archive_type}"
        return True, ""
    except Exception as e:
        return False, f"Extraction failed: {str(e)}"

def create_archive(source_dir: str, output_path: str, archive_type: str) -> Tuple[bool, str]:
    """Create ZIP or 7z archive from source directory."""
    try:
        if archive_type == "zip":
            with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(source_dir):
                    for f in files:
                        full_path = os.path.join(root, f)
                        arcname = os.path.relpath(full_path, source_dir)
                        zf.write(full_path, arcname)
        elif archive_type == "7z":
            with py7zr.SevenZipFile(output_path, "w") as szf:
                szf.writeall(source_dir, arcname="")
        else:
            return False, f"Unsupported archive type: {archive_type}"
        return True, ""
    except Exception as e:
        return False, f"Archive creation failed: {str(e)}"

def merge_archives(archive_paths: List[Tuple[str, str]], output_path: str, target_type: str) -> Tuple[bool, str]:
    """
    Merge multiple archives into one.
    Each archive is extracted into a subfolder named after the original file name (without extension).
    Then everything is packed into target_type archive.
    archive_paths: list of (original_filename, file_path)
    """
    temp_root = tempfile.mkdtemp()
    try:
        for orig_name, arch_path in archive_paths:
            # Create subfolder named after original file (without extension)
            base_name = os.path.splitext(orig_name)[0]
            extract_dir = os.path.join(temp_root, base_name)
            os.makedirs(extract_dir, exist_ok=True)
            arch_type = get_archive_type(arch_path)
            if not arch_type:
                return False, f"Unknown archive type for {orig_name}"
            success, err = extract_archive(arch_path, extract_dir, arch_type)
            if not success:
                return False, f"Failed to extract {orig_name}: {err}"
        # Now pack temp_root into output archive
        return create_archive(temp_root, output_path, target_type)
    except Exception as e:
        return False, str(e)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)

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
        success, err = extract_archive(input_path, temp_extract, input_type)
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
        success, err = extract_archive(input_path, temp_extract, archive_type)
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

        # Download this text file
        file = await context.bot.get_file(document.file_id)
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            await file.download_to_drive(tmp.name)
            tmp_path = tmp.name

        # Initialize merge session if not exists
        if "merge_files" not in context.user_data:
            context.user_data["merge_files"] = []  # list of (original_name, file_path)
            context.user_data["merge_msg_id"] = None

        # Add current file
        context.user_data["merge_files"].append((document.file_name, tmp_path))

        # Build message with list of files and buttons
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

        # Edit previous merge prompt if exists, otherwise send new
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

        # Delete the "document received" message to keep chat clean
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

    # Download the archive
    file = await context.bot.get_file(document.file_id)
    with tempfile.NamedTemporaryFile(suffix=file_ext, delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    # Initialize archive merge session if not exists
    if "pending_archives" not in context.user_data:
        context.user_data["pending_archives"] = []  # list of (original_name, file_path)
        context.user_data["archive_prompt_msg_id"] = None

    # Add current archive
    context.user_data["pending_archives"].append((document.file_name, tmp_path))

    # Build message showing list of collected archives
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

    # Edit previous prompt or send new
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

    # Delete the "document received" message
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

    # ------------------ TEXT MERGE ACTIONS ------------------
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
            # Cleanup
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
        
        sent_files = 0
        for idx, chunk in enumerate(chunks, start=1):
            with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode='w', encoding='utf-8') as tmp:
                tmp.write("\n".join(chunk))
                chunk_path = tmp.name
            with open(chunk_path, "rb") as fp:
                bio = BytesIO(fp.read())
                if num_chunks == 1:
                    bio.name = "merged_texts.txt"
                else:
                    bio.name = f"merged_part{idx}.txt"
                result_msg = await query.message.reply_document(document=bio, filename=bio.name)
                await store_message(chat_id, result_msg.message_id)
            os.unlink(chunk_path)
            sent_files += 1
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

    # ------------------ ARCHIVE MERGE ACTIONS ------------------
    elif action == "process_single_archive":
        pending = context.user_data.get("pending_archives", [])
        if not pending:
            await query.edit_message_text("❌ No archive found.")
            return
        # Take the last archive as the one to process
        orig_name, arch_path = pending[-1]
        context.user_data["current_archive"] = arch_path
        context.user_data["original_archive_name"] = orig_name
        # Clear pending archives list and delete prompt
        for _, p in pending:
            if p != arch_path:  # keep only the one we use? Actually we should delete others
                os.unlink(p)
        context.user_data.pop("pending_archives", None)
        if context.user_data.get("archive_prompt_msg_id"):
            try:
                await context.bot.delete_message(chat_id, context.user_data["archive_prompt_msg_id"])
            except:
                pass
            context.user_data.pop("archive_prompt_msg_id", None)
        # Show conversion menu
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
        # Ask for target format
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
        target_format = action.split("_")[2]  # "zip" or "7z"
        pending = context.user_data.get("pending_archives", [])
        if len(pending) < 2:
            await query.edit_message_text("❌ Not enough archives to merge.")
            return
        await query.edit_message_text(f"🔄 Merging {len(pending)} archives into one {target_format.upper()} file...")
        progress_msg = await query.message.reply_text("Processing, please wait...")
        with tempfile.NamedTemporaryFile(suffix=f".{target_format}", delete=False) as out_tmp:
            out_path = out_tmp.name
        success, err = merge_archives(pending, out_path, target_format)
        if success:
            with open(out_path, "rb") as fp:
                bio = BytesIO(fp.read())
                bio.name = f"merged_archives.{target_format}"
            result_msg = await query.message.reply_document(document=bio, filename=bio.name)
            await store_message(chat_id, result_msg.message_id)
            await progress_msg.delete()
            # Clean up all collected archives
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
        else:
            await progress_msg.edit_text(f"❌ Merge failed: {err}")

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

    # ------------------ SINGLE ARCHIVE ACTIONS (convert, unzip, cleaner, back) ------------------
    elif action in ("convert", "unzip", "cleaner", "back_to_menu", "conv_zip", "conv_7z"):
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
                if not archive_type:
                    await query.message.reply_text("❌ Unsupported archive type (only ZIP/7z).")
                    return
                success, err = extract_archive(archive_path, extract_dir, archive_type)
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
                "✅ Archive ready. Choose an action:",
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
    print("✅ Bot started. Features: Text merge (with split), Archive merge (ZIP/7z), Archive convert/unzip/cleaner.")
    app.run_polling()

if __name__ == "__main__":
    main()
