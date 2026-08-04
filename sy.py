import time
import logging
import random
import asyncio
import json
import os
import re
import aiohttp
from datetime import datetime
from aiohttp import web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions, InputMediaPhoto
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from aiogram.enums import ButtonStyle

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- تنظیمات اولیه ---
BOT_TOKEN = "8698090320:AAHQZbpaF0-BZ2Lnl6RJZ31Jb9Xe_QSKaYk"
OWNER_ID = 7531540968
DB_FILE = "database.json"

DEFAULT_BOT_DATA = {
    "messages": [],           
    "medias": [],            
    "interval": 10,           
    "is_running": False,      
    "attack_mode": "random",  
    "tag_text": "شخص پدر مرده", 
    "unauth_msg": "به توپم دست نزن", 
    "lock_msg": "کصمادرت اگر لف بدی مادرجنده",
    "saved_users": {},       
    "admins": {
        str(OWNER_ID): {
            "type": "permanent",
            "username": "OWNER",
            "permissions": ["admins", "messages", "commands"]
        }
    },
    "user_logs": {},          
    "history": [], 
    "undo_stack": [],           
    "joined_groups": {},
    "username_cache": {},
    "known_users": {},
    "group_members": {},
    "locked_users": [],
    "lock_paused": False,
    "temp_admin_data": {}
}

bot_data = dict(DEFAULT_BOT_DATA)

(
    WAITING_FOR_MSG, 
    WAITING_FOR_CUSTOM_TIME, 
    WAITING_FOR_ADMIN_ID, 
    WAITING_FOR_ADMIN_TIME,
    WAITING_FOR_TAG_TEXT,
    WAITING_FOR_UNAUTH_MSG,
    WAITING_FOR_LOCK_MSG,
    WAITING_FOR_MEDIA
) = range(8)

db_lock = asyncio.Lock()
active_attack_task = None
web_runner = None
keep_alive_task = None

def escape_markdown(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

async def save_db_async():
    async with db_lock:
        temp_file = DB_FILE + ".tmp"
        try:
            def _write():
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(bot_data, f, ensure_ascii=False, indent=4)
            
            await asyncio.to_thread(_write)
            await asyncio.to_thread(os.replace, temp_file, DB_FILE)
        except Exception as e:
            logging.error(f"Error saving DB atomically: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except:
                    pass

def save_db():
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(save_db_async())
    except RuntimeError:
        temp_file = DB_FILE + ".tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(bot_data, f, ensure_ascii=False, indent=4)
            os.replace(temp_file, DB_FILE)
        except Exception as e:
            logging.error(f"Error in sync save_db: {e}")

def create_undo_point():
    try:
        data_copy = dict(bot_data)
        data_copy["undo_stack"] = []

        snapshot = json.loads(json.dumps(data_copy))

        stack = bot_data.setdefault("undo_stack", [])
        stack.append(snapshot)

        if len(stack) > 10:
            stack.pop(0)

        save_db()

    except Exception as e:
        logging.error(f"Undo save error: {e}")          

def validate_and_merge_data(loaded_data: dict) -> dict:
    if not isinstance(loaded_data, dict):
        raise ValueError("Loaded data is not a valid dictionary.")
    
    merged = dict(DEFAULT_BOT_DATA)
    for key, default_val in DEFAULT_BOT_DATA.items():
        if key in loaded_data:
            val = loaded_data[key]
            if isinstance(default_val, list) and isinstance(val, list):
                merged[key] = val
            elif isinstance(default_val, dict) and isinstance(val, dict):
                merged[key] = val
            elif isinstance(default_val, (int, float, str, bool)) and isinstance(val, type(default_val)):
                merged[key] = val
    return merged

def load_db():
    global bot_data
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                bot_data = validate_and_merge_data(loaded)
        except Exception as e:
            logging.error(f"Error loading DB (file might be corrupted): {e}")

load_db()

def log_event(event_text: str):
    now = time.time()
    bot_data["history"].append({"time": now, "event": event_text})
    bot_data["history"] = [h for h in bot_data["history"] if now - h["time"] <= 86400]
    save_db()

def is_admin(user_id: int) -> bool:
    uid_str = str(user_id)
    return uid_str in bot_data["admins"] or user_id == OWNER_ID

def has_permission(user_id: int, perm: str) -> bool:
    if user_id == OWNER_ID: return True
    if not is_admin(user_id): return False
    return perm in bot_data["admins"][str(user_id)].get("permissions", [])

def estimate_creation_year(user_id: int) -> str:
    if user_id < 100000000: return "2013-2015"
    elif user_id < 300000000: return "2016"
    elif user_id < 550000000: return "2017"
    elif user_id < 850000000: return "2018"
    elif user_id < 1100000000: return "2019"
    elif user_id < 1600000000: return "2020"
    elif user_id < 2100000000: return "2021"
    elif user_id < 5400000000: return "2022"
    elif user_id < 6300000000: return "2023"
    elif user_id < 7200000000: return "2024"
    elif user_id < 8000000000: return "2025"
    else: return "2026"

async def resolve_user_input(input_str: str, context: ContextTypes.DEFAULT_TYPE):
    input_str = input_str.strip()
    if input_str.isdigit():
        uid = int(input_str)
        uname = bot_data.get("username_cache", {}).get(str(uid), "Unknown")
        try:
            chat = await context.bot.get_chat(uid)
            if chat.username:
                uname = chat.username
                cache = bot_data.setdefault("username_cache", {})
                cache[str(uid)] = uname
                if len(cache) > 1000:
                    cache.pop(next(iter(cache)))
                save_db()
            return chat.id, uname, chat.full_name or "کاربر"
        except Exception:
            return uid, uname, "کاربر"
            
    elif input_str.startswith("@"):
        clean_uname = input_str.replace("@", "").strip().lower()
        
        for uid_str, cached_uname in bot_data.get("username_cache", {}).items():
            if cached_uname.lower() == clean_uname:
                return int(uid_str), cached_uname, "کاربر"

        try:
            chat = await context.bot.get_chat(f"@{clean_uname}")
            if chat.username:
                cache = bot_data.setdefault("username_cache", {})
                cache[str(chat.id)] = chat.username
                if len(cache) > 1000:
                    cache.pop(next(iter(cache)))
                save_db()
            return chat.id, chat.username or clean_uname, chat.full_name or "کاربر"
        except Exception:
            return None, clean_uname, None
            
    return None, None, None

# منوهای شیشه‌ای با ترکیب رنگ‌های آبی (PRIMARY)، قرمز (DANGER) و سبز (SUCCESS)
def get_main_menu(owner_user_id: int):
    keyboard = [
        [
            InlineKeyboardButton("💬 تنظیم پیام‌ها", callback_data=f"menu_set_msg:{owner_user_id}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("🖼 تنظیم مدیا", callback_data=f"menu_set_media:{owner_user_id}", style=ButtonStyle.SUCCESS)
        ],
        [
            InlineKeyboardButton("⏱ زمان ارسال", callback_data=f"menu_time:{owner_user_id}", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton("🏷 کلمه تگ", callback_data=f"menu_tag_text:{owner_user_id}", style=ButtonStyle.PRIMARY)
        ],
        [
            InlineKeyboardButton("📢 متن غیرادمین", callback_data=f"menu_unauth_msg:{owner_user_id}", style=ButtonStyle.PRIMARY),
            InlineKeyboardButton("🔒 تنظیم پیام قفل", callback_data=f"menu_lock_msg:{owner_user_id}", style=ButtonStyle.DANGER)
        ],
        [
            InlineKeyboardButton("👥 مدیریت ادمین‌ها", callback_data=f"menu_admins:{owner_user_id}", style=ButtonStyle.SUCCESS),
            InlineKeyboardButton("📖 راهنما", callback_data=f"menu_help:{owner_user_id}", style=ButtonStyle.PRIMARY)
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_attack_mode_menu(owner_user_id: int):
    keyboard = [
        [InlineKeyboardButton("🎲 تصادفی (Random)", callback_data=f"mode_random:{owner_user_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton("🔢 ترتیبی (Sequential)", callback_data=f"mode_sequential:{owner_user_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton("💣 خشاب تک‌پیامی (Single Bomb)", callback_data=f"mode_bomb:{owner_user_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton("🔒 اتک قفلی (Lock & Mute)", callback_data=f"mode_lock:{owner_user_id}", style=ButtonStyle.DANGER)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_time_menu(owner_user_id: int):
    keyboard = [
        [InlineKeyboardButton("2 ثانیه", callback_data=f"time_2:{owner_user_id}", style=ButtonStyle.PRIMARY), InlineKeyboardButton("5 ثانیه", callback_data=f"time_5:{owner_user_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton("10 ثانیه", callback_data=f"time_10:{owner_user_id}", style=ButtonStyle.SUCCESS), InlineKeyboardButton("30 ثانیه", callback_data=f"time_30:{owner_user_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton("⏱ دلخواه", callback_data=f"time_custom:{owner_user_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"menu_main:{owner_user_id}", style=ButtonStyle.DANGER)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_menu(owner_user_id: int):
    keyboard = [
        [InlineKeyboardButton("➕ افزودن ادمین", callback_data=f"admin_add:{owner_user_id}", style=ButtonStyle.SUCCESS), InlineKeyboardButton("➖ حذف ادمین", callback_data=f"admin_del:{owner_user_id}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton("📋 لیست ادمین‌ها", callback_data=f"admin_list:{owner_user_id}", style=ButtonStyle.PRIMARY), InlineKeyboardButton("⚠️ پاکسازی همه ادمین‌ها", callback_data=f"admin_delall_confirm:{owner_user_id}", style=ButtonStyle.DANGER)],
        [InlineKeyboardButton("👑 مالک‌ها", callback_data=f"admin_owners:{owner_user_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"menu_main:{owner_user_id}", style=ButtonStyle.DANGER)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_permissions_menu(owner_user_id: int, target_id: int):
    temp_data = bot_data.get("temp_admin_data", {})
    perms = temp_data.get("permissions", []) if isinstance(temp_data, dict) else []
    p1 = "✅" if "admins" in perms else "❌"
    p2 = "✅" if "messages" in perms else "❌"
    p3 = "✅" if "commands" in perms else "❌"

    keyboard = [
        [InlineKeyboardButton(f"{p1} دسترسی ادمین‌ها", callback_data=f"perm_admins:{owner_user_id}:{target_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton(f"{p2} دسترسی پیام/مدیا", callback_data=f"perm_messages:{owner_user_id}:{target_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton(f"{p3} دسترسی دستورات", callback_data=f"perm_commands:{owner_user_id}:{target_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton("💾 ثبت و نهایی‌سازی", callback_data=f"perm_save:{owner_user_id}:{target_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"menu_admins:{owner_user_id}", style=ButtonStyle.DANGER)]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_backup_menu(owner_user_id: int):
    keyboard = [
        [InlineKeyboardButton("🎬 گیف‌ها", callback_data=f"backup_animation:{owner_user_id}", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🎭 استیکرها", callback_data=f"backup_sticker:{owner_user_id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton("📷 عکس‌ها", callback_data=f"backup_photo:{owner_user_id}", style=ButtonStyle.SUCCESS), InlineKeyboardButton("🎙 ویس‌ها", callback_data=f"backup_voice:{owner_user_id}", style=ButtonStyle.PRIMARY)],
        [InlineKeyboardButton("📦 کل دیتابیس", callback_data=f"backup_full:{owner_user_id}", style=ButtonStyle.SUCCESS)]
    ]
    return InlineKeyboardMarkup(keyboard)

async def check_panel_owner(query, owner_user_id: int) -> bool:
    if query.from_user.id != owner_user_id:
        await query.answer("کصخل این پنل برای تو نیست! ادم باش 🤥", show_alert=True)
        return False
    return True

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username_str = f"@{user.username}" if user.username else str(user.id)
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    
    welcome_text = (
        f"سلام {username_str} عزیز! 👋\n"
        f"برای کار با ربات باید دسترسی داشته باشی. اول به پشتیبانی @Anotherger پیام بده ردیفت کنه، بعد بیا فعالیت کن!\n\n"
        f"⚙️ جهت ورود به تنظیمات دستور /panel رو بفرست."
    )
    await update.message.reply_text(welcome_text, message_thread_id=thread_id)

async def setallmember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ندارید")
        return


    chat = update.effective_chat

    if chat.type not in ["group", "supergroup"]:
        await update.message.reply_text(
            "❌ این دستور فقط داخل گروه کار می‌کند"
        )
        return


    group_id = str(chat.id)

    members = bot_data.get("group_members", {}).get(group_id, {})


    if not members:
        await update.message.reply_text(
            "❌ هنوز هیچ کاربری از این گروه ثبت نشده"
        )
        return


    text = "👥 لیست اعضای ثبت شده:\n\n"


    for uid, info in members.items():

        username = info.get("username", "NoUsername")

        if username != "NoUsername":
            text += f"@{username} - {uid}\n"

        else:
            text += f"{info.get('first_name','Unknown')} - {uid}\n"


    keyboard = [
        [
            InlineKeyboardButton(
                "✅ اضافه کردن همه به تارگت",
                callback_data=f"addall_{group_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data="cancel_addall"
            )
        ]
    ]


    await update.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def save_group_member(update: Update):

    if not update.message:
        return

    user = update.effective_user
    chat = update.effective_chat

    if not user or not chat:
        return

    # فقط گروه و سوپرگروه
    if chat.type in ["group", "supergroup"]:

        group_id = str(chat.id)

        bot_data.setdefault("group_members", {})

        bot_data["group_members"].setdefault(group_id, {})


        bot_data["group_members"][group_id][str(user.id)] = {
            "username": user.username or "NoUsername",
            "first_name": user.first_name or "Unknown"
        }


        save_db()    

async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None

    if not is_admin(user_id):
        await update.message.reply_text(bot_data.get("unauth_msg", "به توپم دست نزن"), message_thread_id=thread_id)
        return

    panel_text = (
        "👋 به پنل مدیریت ربات خوش آمدید.\n\n"
        f"🏷 متن تگ فعلی:\n«{escape_markdown(bot_data['tag_text'])}»\n"
        "-------------------\n"
        f"💬 متن غیرادمین فعلی:\n«{escape_markdown(bot_data.get('unauth_msg', 'به توپم دست نزن'))}»\n"
        "-------------------\n"
        f"🔒 متن اتک قفلی:\n«{escape_markdown(bot_data.get('lock_msg', 'کصمادرت اگر لف بدی مادرجنده'))}»\n\n"
        "لطفاً یک بخش را انتخاب کنید:"
    )

    await update.message.reply_text(
        panel_text,
        parse_mode="Markdown",
        reply_markup=get_main_menu(user_id),
        message_thread_id=thread_id
    )

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text(
            bot_data.get("unauth_msg", "به توپم دست نزن"),
            message_thread_id=thread_id
        )
        return

    await update.message.reply_text(
        "❌ عملیات جاری لغو شد. برگشتیم به منوی مدیریت:",
        reply_markup=get_main_menu(user_id),
        message_thread_id=thread_id
    )
    return ConversationHandler.END

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data_parts = query.data.split(":")
    action = data_parts[0]
    owner_user_id = int(data_parts[1]) if len(data_parts) > 1 else query.from_user.id

    if not await check_panel_owner(query, owner_user_id): return

    await query.answer()
    user_id = query.from_user.id

    if not is_admin(user_id):
        await query.answer("دسترسی نداری!", show_alert=True)
        return

    global active_attack_task

    if action == "menu_main":
        await query.edit_message_text("👋 پنل اصلی مدیریت:", reply_markup=get_main_menu(owner_user_id))

    elif action == "menu_set_msg":
        if not has_permission(user_id, "messages"):
            await query.edit_message_text("❌ دسترسی نداری حاجی!", reply_markup=get_main_menu(owner_user_id))
            return
        await query.edit_message_text("📝 متون مورد نظرت رو یکی‌یکی بفرست.\nوقتی کارت تموم شد /done یا /cancel رو بزن.")
        return WAITING_FOR_MSG

    elif action == "menu_set_media":
        if not has_permission(user_id, "messages"):
            await query.edit_message_text("❌ دسترسی نداری حاجی!", reply_markup=get_main_menu(owner_user_id))
            return
        await query.edit_message_text("🖼 عکس، ویس، گیف یا استیکرهات رو بفرست.\nوقتی کارت تموم شد /done یا /cancel رو بزن.")
        return WAITING_FOR_MEDIA

    elif action == "menu_tag_text":
        await query.edit_message_text(f"🏷 کلمه تگ فعلی: {bot_data['tag_text']}\n\nکلمه جدید رو بفرست (یا /cancel برای انصراف):")
        return WAITING_FOR_TAG_TEXT

    elif action == "menu_unauth_msg":
        await query.edit_message_text(f"💬 متن جواب به غریبه‌ها: {bot_data.get('unauth_msg', 'به توپم دست نزن')}\n\nمتن جدید رو بفرست (یا /cancel برای انصراف):")
        return WAITING_FOR_UNAUTH_MSG

    elif action == "menu_lock_msg":
        await query.edit_message_text(f"🔒 متن قفلی فعلی: {bot_data.get('lock_msg', 'کصمادرت اگر لف بدی مادرجنده')}\n\nمتن جدید رو بفرست (یا /cancel برای انصراف):")
        return WAITING_FOR_LOCK_MSG

    elif action == "menu_time":
        await query.edit_message_text(f"⏱ فاصله ارسال فعلی: {bot_data['interval']} ثانیه\nیکی رو انتخاب کن:", reply_markup=get_time_menu(owner_user_id))

    elif action.startswith("time_"):
        val = action.split("_")[1]
        if val == "custom":
            await query.edit_message_text("زمان مدنظرت رو به ثانیه (عدد) بفرست:")
            return WAITING_FOR_CUSTOM_TIME
        else:
            sec = int(val)
            create_undo_point()
            bot_data["interval"] = sec
            save_db()
            await query.edit_message_text(f"✅ زمان ارسال روی {sec} ثانیه تنظیم شد.", reply_markup=get_main_menu(owner_user_id))

    elif action.startswith("mode_"):
        mode = action.split("_")[1]
        create_undo_point()
        bot_data["attack_mode"] = mode
        bot_data["is_running"] = True
        save_db()
        
        chat_id = query.message.chat_id
        thread_id = query.message.message_thread_id if query.message.is_topic_message else None
        
        if active_attack_task and not active_attack_task.done():
            active_attack_task.cancel()
            try:
                await active_attack_task
            except asyncio.CancelledError:
                pass
            active_attack_task = None

        active_attack_task = asyncio.create_task(start_auto_sending(chat_id, thread_id, context))
        
        log_event(f"🚀 شروع اتک {mode} توسط {user_id}")
        await query.edit_message_text(f"🚀 اتک رو حالت **{mode}** با تایم {bot_data['interval']} ثانیه استارت خورد!", parse_mode="Markdown")

    elif action == "menu_admins":
        if user_id != OWNER_ID and not has_permission(user_id, "admins"):
            await query.edit_message_text("❌ فقط مالکا دسترسی دارن.", reply_markup=get_main_menu(owner_user_id))
            return
        await query.edit_message_text("👥 بخش مدیریت ادمین‌ها:", reply_markup=get_admin_menu(owner_user_id))

    elif action == "admin_list":
        admin_text = "📋 **لیست ادمین‌های ربات:**\n\n"
        for aid, ainfo in bot_data["admins"].items():
            uname = ainfo.get("username", "نامشخص")
            if uname != "OWNER" and uname != "نامشخص": uname = f"@{uname}"
            atype = "دائمی" if ainfo.get("type") == "permanent" else "ساعتی"
            admin_text += f"• `{aid}` ({uname}) ➔ {atype}\n"
        await query.edit_message_text(admin_text, parse_mode="Markdown", reply_markup=get_admin_menu(owner_user_id))

    elif action == "admin_delall_confirm":
        kb = [
            [InlineKeyboardButton("✅ آره پاک کن", callback_data=f"admin_delall_yes:{owner_user_id}", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton("❌ بیخیال", callback_data=f"admin_list:{owner_user_id}", style=ButtonStyle.PRIMARY)]
        ]
        await query.edit_message_text("⚠️ مطمئنی می‌خوای همه ادمین‌ها بپرن؟", reply_markup=InlineKeyboardMarkup(kb))

    elif action == "admin_delall_yes":
        create_undo_point()
        bot_data["admins"] = {
            str(OWNER_ID): {
                "type": "permanent",
                "username": "OWNER",
                "permissions": ["admins", "messages", "commands"]
            }
        }
        save_db()
        log_event("☣️ پاکسازی کامل ادمین‌ها")
        await query.edit_message_text("✅ همه ادمین‌ها پاک شدن.", reply_markup=get_admin_menu(owner_user_id))

    elif action == "admin_add":
        await query.edit_message_text("آیدی عددی یا یوزرنیم (@username) ادمین جدید رو بفرست:")
        return WAITING_FOR_ADMIN_ID

    elif action.startswith("perm_"):
        p = action.split("_")[1]
        target_id = data_parts[2] if len(data_parts) > 2 else "0"
        
        if p == "save":
            t_data = bot_data.get("temp_admin_data", {})
            create_undo_point()
            bot_data["admins"][str(target_id)] = {
                "type": "permanent",
                "username": t_data.get("username", "نامشخص"),
                "permissions": t_data.get("permissions", ["admins", "messages", "commands"])
            }
            bot_data["temp_admin_data"] = {}
            save_db()
            log_event(f"➕ ثبت ادمین جدید: {target_id}")
            await query.edit_message_text(f"✅ ادمین `{target_id}` ثبت شد.", parse_mode="Markdown", reply_markup=get_admin_menu(owner_user_id))
        else:
            perms = bot_data.setdefault("temp_admin_data", {}).setdefault("permissions", ["admins", "messages", "commands"])
            if p in perms: perms.remove(p)
            else: perms.append(p)
            await query.edit_message_text("⚙️ دسترسی‌های ادمین رو مشخص کن:", reply_markup=get_permissions_menu(owner_user_id, int(target_id)))

    elif action == "admin_owners":
        await query.edit_message_text(f"👑 مالک اصلی:\n• `{OWNER_ID}`", parse_mode="Markdown", reply_markup=get_admin_menu(owner_user_id))

    elif action.startswith("backup_"):
        b_type = action.split("_")[1]
        save_db()
        
        if b_type == "full":
            with open(DB_FILE, "rb") as f_doc:
                await context.bot.send_document(chat_id=query.message.chat_id, document=f_doc, filename="database.json", caption="📦 بکاپ کامل دیتابیس.")
        else:
            filtered = [m for m in bot_data.get("medias", []) if m["type"] == b_type]
            out_file = f"backup_{b_type}.json"
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(filtered, f, ensure_ascii=False, indent=4)
            with open(out_file, "rb") as f_doc:
                await context.bot.send_document(chat_id=query.message.chat_id, document=f_doc, filename=out_file, caption=f"📦 بکاپ بخش {b_type}")
            if os.path.exists(out_file): os.remove(out_file)

        await query.edit_message_text("✅ بفرما اینم فایل بکاپ.")

    elif action.startswith("addall_"):

        group_id = action.split("_", 1)[1]

        members = bot_data.get("group_members", {}).get(group_id, {})

        create_undo_point()

        bot_data.setdefault("saved_users", {})

        for uid, info in members.items():
            bot_data["saved_users"][uid] = {
                "username": info.get("username", "NoUsername"),
                "custom_tag": None
            }

        save_db()

        await query.edit_message_text(
            "✅ تمام اعضای ثبت شده به لیست تارگت اضافه شدند."
        )  

    elif action.startswith("target_add_"):
        target_uid = action.split("_")[2]
        fetched_username = bot_data.get("username_cache", {}).get(target_uid, "Unknown")
        try:
            c = await context.bot.get_chat(int(target_uid))
            if c.username: 
                fetched_username = c.username
                cache = bot_data.setdefault("username_cache", {})
                cache[target_uid] = fetched_username
                if len(cache) > 1000:
                    cache.pop(next(iter(cache)))
        except Exception: pass

        create_undo_point()
        bot_data["saved_users"][target_uid] = {"username": fetched_username, "custom_tag": None}
        save_db()
        
        uname_disp = f"@{fetched_username}" if fetched_username != "Unknown" else "بدون یوزرنیم"
        confirm_msg = f"✅ کاربر {uname_disp} با آیدی عددی `{target_uid}` به لیست اضافه شد."

        try:
            if query.message.photo:
                await query.edit_message_caption(caption=confirm_msg, parse_mode="Markdown")
            else:
                await query.edit_message_text(text=confirm_msg, parse_mode="Markdown")
        except Exception:
            await query.message.reply_text(confirm_msg, parse_mode="Markdown")

    elif action == "menu_help":
        await help_cmd(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    
    help_text = (
        "📖 راهنمای کامل دستورات:\n\n"
        "/panel - باز کردن پنل مدیریت\n"
        "/addadmin ID/@Username - افزودن ادمین جدید\n"
        "/deladmin ID/@Username - حذف ادمین\n"
        "/set ID1 @Username2 - افزودن دسته‌ای آیدی/یوزرنیم یا لقب بر روی ریپلی\n"
        "/del ID/@Username - حذف یک فرد یا حذف بر روی ریپلی\n"
        "/undo - بازگردانی آخرین عملیات (برگشت از تغییرات اخیر)\n" # <-- اضافه شده
        "/list - مشاهده افراد سیو شده\n"
        "/listmsg - مشاهده پیام‌ها و مدیاهای ثبت‌شده\n"
        "/delallsave - پاکسازی کامل افراد\n"
        "/deltext - پاکسازی پیام‌های متنی خشاب\n"
        "/delmedia - پاکسازی مدیاهای خشاب\n"
        "/deldata - پاکسازی دیتابیس پیام‌ها و مدیاها\n"
        "/go - شروع اتک با منوی انتخاب حالت\n"
        "/stop - توقف اتک\n"
        "/cancel - لغو عملیات جاری و بازگشت به منوی پنل\n"
        "/recent - گزارش اتفاقات ۲۴ ساعت اخیر\n"
        "/report - ارسال گزارش زنده ربات به پیوی مالک\n"
        "/info - شناسنامه و مشخصات کامل کاربر بر روی ریپلی\n"
        "/history_user ID - تاریخچه پیام‌های ثبت‌شده تارگت\n"
        "/backup - دریافت فایل بکاپ دیتابیس\n"
        "/restore - ریستور بکاپ با آپلود فایل\n"
        "/status - وضعیت فنی ربات"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(help_text, reply_markup=get_main_menu(update.effective_user.id))
    else:
        await update.message.reply_text(help_text, message_thread_id=thread_id)

async def collect_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        create_undo_point()
        bot_data["messages"].append(update.message.text)
        save_db()
        thread_id = update.message.message_thread_id if update.message.is_topic_message else None
        await update.message.reply_text(f"✅ ذخیره شد. (تعداد: {len(bot_data['messages'])})\nبعدی رو بفرست یا /done رو بزن.", message_thread_id=thread_id)
    return WAITING_FOR_MSG

async def collect_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    media_item = None
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    
    if msg.photo: media_item = {"type": "photo", "file_id": msg.photo[-1].file_id, "caption": msg.caption or ""}
    elif msg.voice: media_item = {"type": "voice", "file_id": msg.voice.file_id}
    elif msg.animation: media_item = {"type": "animation", "file_id": msg.animation.file_id, "caption": msg.caption or ""}
    elif msg.sticker: media_item = {"type": "sticker", "file_id": msg.sticker.file_id}

    if media_item:
        create_undo_point()
        bot_data["medias"].append(media_item)
        save_db()
        await update.message.reply_text(f"✅ مدیا ذخیره شد! (تعداد: {len(bot_data['medias'])})\nبعدی رو بفرست یا /done رو بزن.", message_thread_id=thread_id)
    return WAITING_FOR_MEDIA

async def done_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    await update.message.reply_text("✅ ثبت متوقف و نهایی شد.", reply_markup=get_main_menu(update.effective_user.id), message_thread_id=thread_id)
    return ConversationHandler.END

async def receive_tag_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        new_tag = update.message.text.strip()
        create_undo_point()
        bot_data["tag_text"] = new_tag
        save_db()
        thread_id = update.message.message_thread_id if update.message.is_topic_message else None
        await update.message.reply_text(f"✅ کلمه تگ شد: {new_tag}", reply_markup=get_main_menu(update.effective_user.id), message_thread_id=thread_id)
    return ConversationHandler.END

async def receive_unauth_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        new_unauth = update.message.text.strip()
        create_undo_point()
        bot_data["unauth_msg"] = new_unauth
        save_db()
        thread_id = update.message.message_thread_id if update.message.is_topic_message else None
        await update.message.reply_text(f"✅ جواب به غریبه‌ها تغییر کرد به: {new_unauth}", reply_markup=get_main_menu(update.effective_user.id), message_thread_id=thread_id)
    return ConversationHandler.END

async def receive_lock_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        new_lock = update.message.text.strip()
        create_undo_point()
        bot_data["lock_msg"] = new_lock
        save_db()
        thread_id = update.message.message_thread_id if update.message.is_topic_message else None
        await update.message.reply_text(f"✅ متن قفلی تغییر کرد به: {new_lock}", reply_markup=get_main_menu(update.effective_user.id), message_thread_id=thread_id)
    return ConversationHandler.END

async def receive_custom_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message and update.message.text:
        text = update.message.text
        thread_id = update.message.message_thread_id if update.message.is_topic_message else None
        if text.isdigit():
            sec = int(text)
            create_undo_point()
            bot_data["interval"] = sec
            save_db()
            await update.message.reply_text(f"✅ تایم ارسال شد {sec} ثانیه.", reply_markup=get_main_menu(update.effective_user.id), message_thread_id=thread_id)
            return ConversationHandler.END
    return WAITING_FOR_CUSTOM_TIME

async def receive_admin_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return WAITING_FOR_ADMIN_ID

    text = msg.text.strip()
    thread_id = msg.message_thread_id if msg.is_topic_message else None
    
    uid, uname, fname = await resolve_user_input(text, context)
    
    if uid:
        bot_data["temp_admin_data"] = {
            "id": uid,
            "username": uname,
            "permissions": ["admins", "messages", "commands"]
        }

        await msg.reply_text(
            f"👤 کاربر `{uid}` (@{uname}) پیدا شد.\nمی‌تونی دسترسی‌هاش رو سفارشی کنی و بعد ثبت نهایی رو بزنی:",
            parse_mode="Markdown",
            reply_markup=get_permissions_menu(update.effective_user.id, uid),
            message_thread_id=thread_id
        )
        return ConversationHandler.END
        
    await msg.reply_text("❌ آیدی عددی یا یوزرنیم نامعتبره! مجدداً بفرست یا /cancel بزن:", message_thread_id=thread_id)
    return WAITING_FOR_ADMIN_ID

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return

    target_uid = None
    target_uname = "Unknown"

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        u = update.message.reply_to_message.from_user
        target_uid = str(u.id)
        target_uname = u.username or u.full_name
        if u.username:
            cache = bot_data.setdefault("username_cache", {})
            cache[target_uid] = u.username
            if len(cache) > 1000:
                cache.pop(next(iter(cache)))
    elif context.args:
        raw_arg = " ".join(context.args).replace(f"@{context.bot.username}", "").strip()
        resolved_uid, resolved_uname, _ = await resolve_user_input(raw_arg, context)
        if resolved_uid:
            target_uid = str(resolved_uid)
            target_uname = resolved_uname

    if target_uid:
        create_undo_point()
        bot_data["admins"][target_uid] = {
            "type": "permanent",
            "username": target_uname,
            "permissions": ["admins", "messages", "commands"]
        }
        save_db()
        log_event(f"➕ افزودن ادمین: {target_uid}")
        
        uname_disp = f"@{target_uname}" if target_uname != "Unknown" else "بدون یوزرنیم"
        response_text = f"✅ کاربر {uname_disp} با آیدی عددی {target_uid} ادمین ربات شد!"
        await update.message.reply_text(response_text, message_thread_id=thread_id)
    else:
        await update.message.reply_text("❌ بر روی پیام کاربر ریپلی کن یا آیدی/یوزرنیم رو جلو دستور بنویس.", message_thread_id=thread_id)

async def deladmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return

    target_uid = None

    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_uid = str(update.message.reply_to_message.from_user.id)
    elif context.args:
        raw_arg = " ".join(context.args).replace(f"@{context.bot.username}", "").strip()
        resolved_uid, _, _ = await resolve_user_input(raw_arg, context)
        if resolved_uid:
            target_uid = str(resolved_uid)

    if target_uid and target_uid in bot_data["admins"]:
        if target_uid == str(OWNER_ID):
            await update.message.reply_text("❌ مالک اصلی رو نمی‌تونی پاک کنی کصخل!", message_thread_id=thread_id)
            return
        create_undo_point()    
        del bot_data["admins"][target_uid]
        save_db()
        log_event(f"➖ حذف ادمین: {target_uid}")
        await update.message.reply_text(f"❌ ادمین {target_uid} از لیست ادمین‌ها حذف شد.", message_thread_id=thread_id)
    else:
        await update.message.reply_text("❌ همچین ادمینی پیدا نشد.", message_thread_id=thread_id)

async def set_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            bot_data.get("unauth_msg", "به توپم دست نزن"),
            message_thread_id=thread_id
        )
        return

    bot_data.setdefault("saved_users", {})
    bot_data.setdefault("username_cache", {})

    added = []

    # حالت ریپلای روی پیام کاربر
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user
        uid = str(target_user.id)
        uname = target_user.username or "NoUsername"

        if uname != "NoUsername":
            bot_data["username_cache"][uid] = uname
            if len(bot_data["username_cache"]) > 1000:
                bot_data["username_cache"].pop(next(iter(bot_data["username_cache"])))

        custom_tag = " ".join(context.args) if context.args else None

        # ثبت نقطه Undo قبل از اعمال تغییر
        if 'create_undo_point' in globals():
            create_undo_point()

        bot_data["saved_users"][uid] = {
            "username": uname,
            "custom_tag": custom_tag
        }

        added.append(
            f"{uid} (لقب: {custom_tag or 'پیش‌فرض'})"
        )

    # حالت وارد کردن آیدی یا یوزرنیم
    elif context.args:
        custom_tag = None
        args = context.args.copy()

        # اگر آخرین آرگومان بعد از --tag بود به عنوان لقب بگیر
        if "--tag" in args:
            index = args.index("--tag")
            if index + 1 < len(args):
                custom_tag = " ".join(args[index + 1:])
            args = args[:index]

        for arg in args:
            uid, uname, _ = await resolve_user_input(arg, context)
            if uid:
                # ثبت نقطه Undo قبل از افزودن هر کاربر
                if 'create_undo_point' in globals():
                    create_undo_point()

                bot_data["saved_users"][str(uid)] = {
                    "username": uname or "NoUsername",
                    "custom_tag": custom_tag
                }

                if uname:
                    bot_data["username_cache"][str(uid)] = uname

                added.append(
                    f"{uid} (@{uname or 'بدون یوزرنیم'})"
                )

    if added:
        save_db()
        await update.message.reply_text(
            "✅ کاربران اضافه شدند:\n\n" +
            "\n".join(added),
            message_thread_id=thread_id
        )
    else:
        await update.message.reply_text(
            "❌ ورودی نامعتبره.\n"
            "روی پیام کاربر ریپلای کن یا آیدی/یوزرنیم بده.",
            message_thread_id=thread_id
        )

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return

    users = bot_data.get("saved_users", {})
    if not users:
        await update.message.reply_text("لیست خالیه حاجی.", message_thread_id=thread_id)
        return

    text = "📋 لیست کاربران تنظیم‌شده:\n\n"
    for uid, info in users.items():
        uname_val = info.get('username')
        if not uname_val or uname_val in ["Unknown", "NoUsername"]:
            cached = bot_data.get("username_cache", {}).get(uid)
            if cached:
                uname_val = cached
            else:
                try:
                    c = await context.bot.get_chat(int(uid))
                    if c.username:
                        uname_val = c.username
                        info['username'] = uname_val
                        cache = bot_data.setdefault("username_cache", {})
                        cache[uid] = uname_val
                        if len(cache) > 1000:
                            cache.pop(next(iter(cache)))
                        save_db()
                except Exception: pass

        uname = f"@{uname_val}" if uname_val and uname_val not in ["Unknown", "NoUsername"] else "بدون یوزرنیم"
        ctag = info.get('custom_tag') or 'پیش‌فرض'
        text += f"• {uid} ({uname}) ➔ 🏷 لقب: {ctag}\n"

    await update.message.reply_text(text, message_thread_id=thread_id)

async def listmsg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return

    messages = bot_data.get("messages", [])
    medias = bot_data.get("medias", [])
    text = f"📝 **آمار خشاب:**\n💬 متون: {len(messages)} تا\n🖼 مدیاها: {len(medias)} تا"
    await update.message.reply_text(text, parse_mode="Markdown", message_thread_id=thread_id)

async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return
    
    target_id = None
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_id = str(update.message.reply_to_message.from_user.id)
    elif context.args:
        uid, _, _ = await resolve_user_input(context.args[0], context)
        if uid: target_id = str(uid)

    if target_id and target_id in bot_data["saved_users"]:
        create_undo_point()
        del bot_data["saved_users"][target_id]
        save_db()
        await update.message.reply_text(f"❌ کاربر {target_id} بایکوت شد.", message_thread_id=thread_id)
    else:
        await update.message.reply_text("❌ پیدا نشد همچین چیزی.", message_thread_id=thread_id)

async def delallsave_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return
    create_undo_point()
    bot_data["saved_users"].clear()
    save_db()
    await update.message.reply_text("🧹 همه تارگت‌ها جارو شدند.", message_thread_id=thread_id)

async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):

    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None

    if not is_admin(update.effective_user.id):
        return

    stack = bot_data.get("undo_stack", [])

    if not stack:
        await update.message.reply_text(
            "❌ عملیات قابل برگشتی وجود ندارد.",
            message_thread_id=thread_id
        )
        return


    old_data = stack.pop()

    bot_data.clear()
    bot_data.update(old_data)

    bot_data["undo_stack"] = stack

    save_db()

    await update.message.reply_text(
        "↩️ آخرین عملیات با موفقیت برگشت داده شد.",
        message_thread_id=thread_id
    )    

async def deltext_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return
    create_undo_point()
    bot_data["messages"].clear()
    save_db()
    await update.message.reply_text("🗑 متون خشاب پاک شدند.", message_thread_id=thread_id)

async def delmedia_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return
    create_undo_point()
    bot_data["medias"].clear()
    save_db()
    await update.message.reply_text("🗑 مدیاهای خشاب پاک شدند.", message_thread_id=thread_id)

async def deldata_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return
    create_undo_point()
    bot_data["messages"].clear()
    bot_data["medias"].clear()
    save_db()
    await update.message.reply_text("🗑 کلاً خشاب خالی شد.", message_thread_id=thread_id)

async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return

    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    
    if update.message.reply_to_message and update.message.reply_to_message.from_user:
        target_user = update.message.reply_to_message.from_user
    else:
        target_user = update.effective_user

    if target_user.username:
        cache = bot_data.setdefault("username_cache", {})
        cache[str(target_user.id)] = target_user.username
        if len(cache) > 1000:
            cache.pop(next(iter(cache)))
        save_db()

    creation_year = estimate_creation_year(target_user.id)
    username_str = f"@{target_user.username}" if target_user.username else "ندارد"
    
    info_text = (
        "📊 آمار طرف:\n\n"
        f"👤 اسم: {escape_markdown(target_user.full_name)}\n"
        f"🆔 یوزرنیم: {escape_markdown(username_str)}\n"
        f"🔢 آیدی عددی: `{target_user.id}`\n"
        f"📅 تخمین ساخت اکانت: {creation_year}\n"
    )

    kb = [
        [InlineKeyboardButton("➕ افزودن به تارگت‌ها", callback_data=f"target_add_{target_user.id}:{update.effective_user.id}", style=ButtonStyle.SUCCESS)],
        [InlineKeyboardButton("❌ ولش کن", callback_data=f"menu_main:{update.effective_user.id}", style=ButtonStyle.DANGER)]
    ]
    
    try:
        photos = await context.bot.get_user_profile_photos(target_user.id, limit=1)
        if photos and photos.total_count > 0:
            await update.message.reply_photo(
                photo=photos.photos[0][-1].file_id, 
                caption=info_text, 
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(kb), 
                message_thread_id=thread_id
            )
            return
    except Exception as e:
        logging.error(f"Error photo info: {e}")

    await update.message.reply_text(
        info_text, 
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kb), 
        message_thread_id=thread_id
    )

async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message.is_topic_message else None
    if update.effective_user.id != OWNER_ID: return
    
    await update.message.reply_text("📢 گزارش کارکرد ربات به پیوی شما ارسال شد.", message_thread_id=thread_id)
    
    groups_list_text = "👥 **گروه‌ها:**\n"
    for gid, gtitle in bot_data.get("joined_groups", {}).items():
        groups_list_text += f"• {escape_markdown(gtitle)} (`{gid}`)\n"

    rep = (
        f"📊 **گزارش زنده:**\n\n"
        f"🚀 وضعیت اتک: {'فعال' if bot_data['is_running'] else 'متوقف'}\n"
        f"🎯 تارگت‌ها: {len(bot_data['saved_users'])}\n"
        f"💬 متون: {len(bot_data['messages'])}\n"
        f"🖼 مدیاها: {len(bot_data['medias'])}\n"
        f"👥 ادمین‌ها: {len(bot_data['admins'])}\n\n"
        f"{groups_list_text}"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_ID, text=rep, parse_mode="Markdown")
    except Exception: pass

async def safe_telegram_call(coro):
    while True:
        try:
            return await coro
        except RetryAfter as e:
            logging.warning(f"FloodWait encountered. Sleeping for {e.retry_after} seconds.")
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError as te:
            logging.error(f"Telegram error in safe call: {te}")
            raise
        except Exception as ex:
            logging.error(f"Unexpected error in safe call: {ex}")
            raise

async def start_auto_sending(chat_id: int, thread_id: int, context: ContextTypes.DEFAULT_TYPE):
    seq_index = 0
    try:
        while bot_data.get("is_running", False):
            if bot_data.get("lock_paused", False):
                await asyncio.sleep(2)
                continue

            mode = bot_data.get("attack_mode", "random")
            messages = bot_data.get("messages", [])
            medias = bot_data.get("medias", [])
            default_tag = bot_data.get("tag_text", "شخص پدر مرده")

            if not messages and not medias and mode != "lock":
                await asyncio.sleep(2)
                continue

            tags = []
            for uid, info in bot_data.get("saved_users", {}).items():
                tag = info.get("custom_tag") or default_tag
                tags.append(f"[{escape_markdown(tag)}](tg://user?id={uid})")

            tags_text = " ".join(tags)

            try:
                if mode == "lock":
                    bot_data["locked_users"] = list(bot_data.get("saved_users", {}).keys())
                    for uid in bot_data["locked_users"]:
                        try:
                            await safe_telegram_call(context.bot.restrict_chat_member(
                                chat_id=chat_id,
                                user_id=int(uid),
                                permissions=ChatPermissions(can_send_messages=False)
                            ))
                        except Exception as e:
                            logging.error(f"Error muting {uid}: {e}")

                    save_db()

                    lock_text = bot_data.get("lock_msg", "...")
                    tags_list = [
                        f"[{escape_markdown(uinfo.get('custom_tag') or default_tag)}](tg://user?id={uid})"
                        for uid, uinfo in bot_data.get("saved_users", {}).items()
                    ]

                    if tags_list:
                        lock_text += "\n\n" + " ".join(tags_list)

                    await safe_telegram_call(context.bot.send_message(
                        chat_id=chat_id,
                        text=lock_text,
                        parse_mode="Markdown",
                        message_thread_id=thread_id
                    ))

                elif mode == "bomb":
                    if messages:
                        bomb_text = "\n\n".join([escape_markdown(m) for m in messages])
                        if tags_text:
                            bomb_text += f"\n\n{tags_text}"

                        await safe_telegram_call(context.bot.send_message(
                            chat_id=chat_id,
                            text=bomb_text,
                            parse_mode="Markdown",
                            message_thread_id=thread_id
                        ))

                    for media in medias:
                        m_type = media["type"]
                        f_id = media["file_id"]
                        cap = escape_markdown(media.get("caption", ""))
                        if tags_text:
                            cap = f"{cap}\n\n{tags_text}" if cap else tags_text

                        try:
                            if m_type == "photo":
                                await safe_telegram_call(context.bot.send_photo(chat_id=chat_id, photo=f_id, caption=cap, parse_mode="Markdown", message_thread_id=thread_id))
                            elif m_type == "animation":
                                await safe_telegram_call(context.bot.send_animation(chat_id=chat_id, animation=f_id, caption=cap, parse_mode="Markdown", message_thread_id=thread_id))
                            elif m_type == "voice":
                                await safe_telegram_call(context.bot.send_voice(chat_id=chat_id, voice=f_id, message_thread_id=thread_id))
                                if tags_text:
                                    await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=tags_text, parse_mode="Markdown", message_thread_id=thread_id))
                            elif m_type == "sticker":
                                await safe_telegram_call(context.bot.send_sticker(chat_id=chat_id, sticker=f_id, message_thread_id=thread_id))
                                if tags_text:
                                    await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=tags_text, parse_mode="Markdown", message_thread_id=thread_id))
                        except Exception as me:
                            logging.error(f"Error sending media item {m_type}: {me}")

                elif mode == "sequential":
                    combined = messages + medias
                    if combined:
                        total_messages = len(messages)
                        if seq_index >= len(combined):
                            seq_index = 0

                        item = combined[seq_index]
                        seq_index = (seq_index + 1) % len(combined)

                        if isinstance(item, str):
                            msg_txt = escape_markdown(item)
                            if tags_text:
                                msg_txt += f"\n\n{tags_text}"
                            await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=msg_txt, parse_mode="Markdown", message_thread_id=thread_id))
                        elif isinstance(item, dict):
                            m_type = item["type"]
                            f_id = item["file_id"]
                            cap = escape_markdown(item.get("caption", ""))
                            if tags_text:
                                cap = f"{cap}\n\n{tags_text}" if cap else tags_text

                            try:
                                if m_type == "photo":
                                    await safe_telegram_call(context.bot.send_photo(chat_id=chat_id, photo=f_id, caption=cap, parse_mode="Markdown", message_thread_id=thread_id))
                                elif m_type == "animation":
                                    await safe_telegram_call(context.bot.send_animation(chat_id=chat_id, animation=f_id, caption=cap, parse_mode="Markdown", message_thread_id=thread_id))
                                elif m_type == "voice":
                                    await safe_telegram_call(context.bot.send_voice(chat_id=chat_id, voice=f_id, message_thread_id=thread_id))
                                    if tags_text:
                                        await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=tags_text, parse_mode="Markdown", message_thread_id=thread_id))
                                elif m_type == "sticker":
                                    await safe_telegram_call(context.bot.send_sticker(chat_id=chat_id, sticker=f_id, message_thread_id=thread_id))
                                    if tags_text:
                                        await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=tags_text, parse_mode="Markdown", message_thread_id=thread_id))
                            except Exception as me:
                                logging.error(f"Error sending sequential media {m_type}: {me}")

                elif mode == "random":
                    all_items = messages + medias
                    if all_items:
                        item = random.choice(all_items)
                        if isinstance(item, str):
                            msg_txt = escape_markdown(item)
                            if tags_text:
                                msg_txt += f"\n\n{tags_text}"
                            await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=msg_txt, parse_mode="Markdown", message_thread_id=thread_id))
                        elif isinstance(item, dict):
                            m_type = item["type"]
                            f_id = item["file_id"]
                            cap = escape_markdown(item.get("caption", ""))
                            if tags_text:
                                cap = f"{cap}\n\n{tags_text}" if cap else tags_text

                            try:
                                if m_type == "photo":
                                    await safe_telegram_call(context.bot.send_photo(chat_id=chat_id, photo=f_id, caption=cap, parse_mode="Markdown", message_thread_id=thread_id))
                                elif m_type == "animation":
                                    await safe_telegram_call(context.bot.send_animation(chat_id=chat_id, animation=f_id, caption=cap, parse_mode="Markdown", message_thread_id=thread_id))
                                elif m_type == "voice":
                                    await safe_telegram_call(context.bot.send_voice(chat_id=chat_id, voice=f_id, message_thread_id=thread_id))
                                    if tags_text:
                                        await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=tags_text, parse_mode="Markdown", message_thread_id=thread_id))
                                elif m_type == "sticker":
                                    await safe_telegram_call(context.bot.send_sticker(chat_id=chat_id, sticker=f_id, message_thread_id=thread_id))
                                    if tags_text:
                                        await safe_telegram_call(context.bot.send_message(chat_id=chat_id, text=tags_text, parse_mode="Markdown", message_thread_id=thread_id))
                            except Exception as me:
                                logging.error(f"Error sending random media {m_type}: {me}")

            except Exception as e:
                logging.error(f"Error in auto send iteration: {e}")

            await asyncio.sleep(bot_data.get("interval", 10))
    except asyncio.CancelledError:
        logging.info("Auto sending task cancelled successfully.")
    except Exception as e:
        logging.error(f"Error in auto send loop: {e}", exc_info=True)

async def go_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return
    await update.message.reply_text("⚙️ حالت اتک رو انتخاب کن:", reply_markup=get_attack_mode_menu(update.effective_user.id), message_thread_id=thread_id)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None

    if not is_admin(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    if bot_data.get("attack_mode") == "lock":
        locked_users = bot_data.get("locked_users", [])

        for uid in locked_users:
            try:
                await safe_telegram_call(context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=int(uid),
                    permissions=ChatPermissions(can_send_messages=True)
                ))

                user_info = bot_data.get("saved_users", {}).get(uid, {})
                username = user_info.get("custom_tag") or bot_data.get("username_cache", {}).get(uid, uid)

                await safe_telegram_call(context.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ فرد @{username} قفل شده آزاد شد!",
                    message_thread_id=thread_id
                ))

            except Exception as e:
                logging.error(f"Error unmuting {uid}: {e}")

        create_undo_point()
        bot_data["locked_users"] = []
        bot_data["lock_paused"] = False

    bot_data["is_running"] = False
    save_db()

    global active_attack_task
    if active_attack_task and not active_attack_task.done():
        active_attack_task.cancel()
        try:
            await active_attack_task
        except asyncio.CancelledError:
            pass
        active_attack_task = None

    await update.message.reply_text(
        "🛑 ارسال پیام‌ها متوقف شد.",
        message_thread_id=thread_id
    )

async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("📦 فرمت بکاپ رو مشخص کن:", reply_markup=get_backup_menu(update.effective_user.id), message_thread_id=thread_id)

async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    now = time.time()
    recent_logs = [h for h in bot_data.get("history", []) if now - h["time"] <= 86400]
    text = "📜 **گزارش ۲۴ ساعت اخیر:**\n\n"
    for log in reversed(recent_logs):
        time_str = time.strftime('%H:%M:%S', time.localtime(log['time']))
        text += f"⏱ [{time_str}] {escape_markdown(log['event'])}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_data

    user_id = update.effective_user.id
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None

    if not is_admin(user_id):
        await update.message.reply_text(
            bot_data.get("unauth_msg", "به توپم دست نزن"),
            message_thread_id=thread_id
        )
        return

    if not update.message.reply_to_message or not update.message.reply_to_message.document:
        await update.message.reply_text(
            "❌ لطفاً روی فایل بکاپ دیتابیس ریپلای کنید.",
            message_thread_id=thread_id
        )
        return

    temp_file = "restore_temp.json"
    try:
        document = update.message.reply_to_message.document
        file = await context.bot.get_file(document.file_id)
        await file.download_to_drive(temp_file)

        with open(temp_file, "r", encoding="utf-8") as f:
            new_data = json.load(f)
            
        validated_data = validate_and_merge_data(new_data)

        create_undo_point()
        async with db_lock:
            bot_data = validated_data
        save_db()

        await update.message.reply_text(
            "✅ دیتابیس با موفقیت ریستور و اعتبارسنجی شد.",
            message_thread_id=thread_id
        )

    except Exception as e:
        await update.message.reply_text(
            f"❌ خطا در ریستور:\n{e}",
            message_thread_id=thread_id
        )
    finally:
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except Exception as e:
                logging.error(f"Error removing temp restore file: {e}")

async def history_user_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    if not is_admin(update.effective_user.id) or not context.args: return
    uid = context.args[0]
    logs = bot_data.get("user_logs", {}).get(uid, [])
    
    if not logs:
        await update.message.reply_text(f"📜 لاگی برای `{uid}` نیست.", parse_mode="Markdown", message_thread_id=thread_id)
        return

    text = f"📜 **تاریخچه پیام‌های `{uid}`:**\n\n"
    for l in logs[-15:]: 
        text += f"⏱ [{l['time']}] {escape_markdown(l['text'])}\n"
    await update.message.reply_text(text, parse_mode="Markdown", message_thread_id=thread_id)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_id = update.message.message_thread_id if update.message and update.message.is_topic_message else None
    if not is_admin(update.effective_user.id): return
    
    start_time = time.time()
    try:
        await context.bot.get_me()
        ping = round((time.time() - start_time) * 1000, 2)
    except Exception:
        ping = 0.0

    status_text = (
        f"📊 **وضعیت ربات اتکر:**\n\n"
        f"⚡️ پینگ ربات: {ping}ms\n"
        f"👥 تعداد ادمین‌ها: {len(bot_data['admins'])}\n"
        f"🎯 افراد سیو شده: {len(bot_data['saved_users'])}\n"
        f"💬 پیام‌های متنی: {len(bot_data['messages'])}\n"
        f"🖼 تعداد مدیاها: {len(bot_data['medias'])}\n"
        f"🏷 کلمه تگ فعلی: {escape_markdown(bot_data['tag_text'])}\n"
        f"💬 متن غیرادمین: {escape_markdown(bot_data.get('unauth_msg', 'به توپم دست نزن'))}\n"
        f"⏱ فاصله ارسال: {bot_data['interval']} ثانیه\n"
        f"🚀 حالت فعلی: {bot_data.get('attack_mode', 'نامشخص')}\n"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown", message_thread_id=thread_id)

async def track_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    await save_group_member(update)

    if msg.from_user:
        uid_str = str(msg.from_user.id)
        if msg.from_user.username:
            cache = bot_data.setdefault("username_cache", {})
            if len(cache) > 1000:
                cache.pop(next(iter(cache)))
            cache[uid_str] = msg.from_user.username

    if msg.chat.type in ["group", "supergroup"]:
        groups = bot_data.setdefault("joined_groups", {})
        groups[str(msg.chat.id)] = msg.chat.title
        if len(groups) > 500:
            groups.pop(next(iter(groups)))

    if msg.left_chat_member:
        left_user = msg.left_chat_member
        uid_str = str(left_user.id)

        if uid_str in bot_data["saved_users"]:
            thread_id = msg.message_thread_id if msg.is_topic_message else None
            uname = f"@{left_user.username}" if left_user.username else left_user.full_name

            await update.message.reply_text(
                text=f"📢 شخص {escape_markdown(uname)} با اینکه فحش گذاشته شد لف داد و بی‌غیرتی خودش رو ثابت کرد! 🤣",
                parse_mode="Markdown",
                message_thread_id=thread_id
            )

            if bot_data.get("attack_mode") == "lock":
                create_undo_point()
                bot_data["lock_paused"] = True
                bot_data["lock_wait_user"] = uid_str
                save_db()

                await update.message.reply_text(
                    text=f"🔒 به علت لف دادن {escape_markdown(uname)} سیو شده، ارسال پیام در حالت قفلی به پایان رسید!",
                    parse_mode="Markdown",
                    message_thread_id=thread_id
                )

    if msg.new_chat_members:
        thread_id = msg.message_thread_id if msg.is_topic_message else None

        for new_user in msg.new_chat_members:
            uid_str = str(new_user.id)

            if uid_str == bot_data.get("lock_wait_user"):
                uname = f"@{new_user.username}" if new_user.username else new_user.full_name

                create_undo_point()
                bot_data["lock_paused"] = False
                bot_data.pop("lock_wait_user", None)
                save_db()

                await update.message.reply_text(
                    text=f"✅ شخص {escape_markdown(uname)} سیو شده مجدد وارد گروه شد!\nارسال پیام ادامه می‌یابد.",
                    parse_mode="Markdown",
                    message_thread_id=thread_id
                )
    if msg.from_user:
     uid = str(msg.from_user.id)

    bot_data.setdefault("known_users", {})
    bot_data.setdefault("username_cache", {})

    bot_data["known_users"][uid] = {
        "username": msg.from_user.username or "",
        "first_name": msg.from_user.first_name or "",
        "last_name": msg.from_user.last_name or ""
    }

    if msg.from_user.username:
        bot_data["username_cache"][uid] = msg.from_user.username

    if msg.from_user and str(msg.from_user.id) in bot_data["saved_users"]:
        uid_str = str(msg.from_user.id)
        user_logs = bot_data.setdefault("user_logs", {}).setdefault(uid_str, [])
        user_logs.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "text": msg.text or "[Media/Other]"
        })
        if len(user_logs) > 50:
            user_logs.pop(0)

async def handle_ping(request): 
    return web.Response(text="Bot is Alive!")

async def auto_keep_alive(port):
    try:
        await asyncio.sleep(10)
        url = f"http://localhost:{port}/"
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.get(url) as resp:
                        pass
                except asyncio.CancelledError:
                    raise
                except Exception:
                    pass
                await asyncio.sleep(300)
    except asyncio.CancelledError:
        logging.info("Keep-alive task cancelled.")

async def start_web_server():
    global web_runner, keep_alive_task
    try:
        web_app = web.Application()
        web_app.router.add_get('/', handle_ping)
        web_runner = web.AppRunner(web_app)
        await web_runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(web_runner, '0.0.0.0', port)
        await site.start()
        keep_alive_task = asyncio.create_task(auto_keep_alive(port))
    except Exception as e:
        logging.error(f"Error starting web server: {e}")

async def cleanup_web_server():
    global keep_alive_task, web_runner
    if keep_alive_task and not keep_alive_task.done():
        keep_alive_task.cancel()
        try:
            await keep_alive_task
        except asyncio.CancelledError:
            pass
    if web_runner:
        try:
            await web_runner.cleanup()
        except Exception as e:
            logging.error(f"Error cleaning up web runner: {e}")

async def main():
    await start_web_server()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(
    CommandHandler(
        "setallmember",
        setallmember_cmd
    )
)

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("panel", panel_cmd),
            CallbackQueryHandler(handle_callback)
        ],
        states={
            WAITING_FOR_MSG: [CommandHandler("done", done_messages), MessageHandler(filters.TEXT & ~filters.COMMAND, collect_messages)],
            WAITING_FOR_MEDIA: [CommandHandler("done", done_messages), MessageHandler((filters.PHOTO | filters.VOICE | filters.ANIMATION | filters.Sticker.ALL) & ~filters.COMMAND, collect_media)],
            WAITING_FOR_TAG_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_tag_text)],
            WAITING_FOR_UNAUTH_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_unauth_msg)],
            WAITING_FOR_LOCK_MSG: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_lock_msg)],
            WAITING_FOR_CUSTOM_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_custom_time)],
            WAITING_FOR_ADMIN_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_id)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_cmd),
            CommandHandler("panel", panel_cmd),
            CallbackQueryHandler(handle_callback)
        ],
        allow_reentry=True,
        per_message=False
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("deladmin", deladmin_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("set", set_user_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("listmsg", listmsg_cmd))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("delallsave", delallsave_cmd))
    app.add_handler(CommandHandler("undo", undo_cmd))
    app.add_handler(CommandHandler("deltext", deltext_cmd))
    app.add_handler(CommandHandler("delmedia", delmedia_cmd))
    app.add_handler(CommandHandler("deldata", deldata_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("go", go_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("recent", recent_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("restore", restore_cmd))
    app.add_handler(CommandHandler("history_user", history_user_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    
    app.add_handler(MessageHandler(filters.ALL, track_chats))

    print("ربات آنلاین شد...")

    try:
        async with app:
            await app.initialize()
            await app.start()
            await app.updater.start_polling(drop_pending_updates=True)
            await asyncio.Event().wait()
    finally:
        global active_attack_task
        if active_attack_task and not active_attack_task.done():
            active_attack_task.cancel()
            try:
                await active_attack_task
            except asyncio.CancelledError:
                pass
        await cleanup_web_server()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass