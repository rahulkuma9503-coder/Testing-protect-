import os
import logging
import uuid
import base64
import json
import secrets
import string
import re
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException, Query
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# --- Telegram Imports ---
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler
from telegram.error import TelegramError, BadRequest, Forbidden
from telegram.constants import ParseMode

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
MONGODB_URI = os.environ.get("MONGODB_URI")
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL")
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "").split(",")
SUPPORT_CHANNEL_ID = os.environ.get("SUPPORT_CHANNEL_ID")

if not TELEGRAM_TOKEN:
    raise Exception("TELEGRAM_TOKEN environment variable is required!")

# --- Database Setup (MongoDB) ---
client = None
db = None

# Database collections (will be initialized after connection)
links_collection = None
webapp_sessions = None
users_collection = None
channel_verification = None
broadcasts_collection = None

# Try to connect to MongoDB
if MONGODB_URI:
    try:
        logger.info("Connecting to MongoDB...")
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=10000,  # 10 seconds
            connectTimeoutMS=15000,  # 15 seconds
            socketTimeoutMS=30000,  # 30 seconds
            retryWrites=True,
            w='majority'
        )
        
        # Test connection
        client.admin.command('ping')
        logger.info("✅ MongoDB connected successfully")
        
        # Setup database
        db_name = "protected_bot_db"
        db = client[db_name]
        
        # Initialize collections
        links_collection = db["protected_links"]
        webapp_sessions = db["webapp_sessions"]
        users_collection = db["users"]
        channel_verification = db["channel_verification"]
        broadcasts_collection = db["broadcasts"]
        
        # Create indexes
        links_collection.create_index("created_at", expireAfterSeconds=2592000)  # 30 days
        links_collection.create_index("_id", unique=True)
        webapp_sessions.create_index("created_at", expireAfterSeconds=1800)  # 30 minutes
        webapp_sessions.create_index("token", unique=True)
        users_collection.create_index("user_id", unique=True)
        users_collection.create_index("last_active")
        
        logger.info("✅ MongoDB collections and indexes initialized")
        
    except Exception as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        logger.warning("Running in memory-only mode (data will be lost on restart)")
        
        # Create in-memory collections as fallback
        class MemoryCollection:
            def __init__(self):
                self.data = {}
            
            def find_one(self, query):
                if "_id" in query:
                    return self.data.get(query["_id"])
                return None
            
            def find(self, query=None, **kwargs):
                return list(self.data.values())
            
            def insert_one(self, document):
                if "_id" in document:
                    self.data[document["_id"]] = document
                elif "token" in document:
                    self.data[document["token"]] = document
                elif "user_id" in document:
                    self.data[document["user_id"]] = document
                return type('obj', (object,), {'inserted_id': 'dummy'})
            
            def update_one(self, query, update, **kwargs):
                doc = self.find_one(query)
                if doc and "$set" in update:
                    doc.update(update["$set"])
                return type('obj', (object,), {'matched_count': 1 if doc else 0})
            
            def delete_one(self, query):
                key = None
                if "_id" in query:
                    key = query.get("_id")
                elif "token" in query:
                    key = query.get("token")
                elif "user_id" in query:
                    key = query.get("user_id")
                
                if key in self.data:
                    del self.data[key]
                    return type('obj', (object,), {'deleted_count': 1})
                return type('obj', (object,), {'deleted_count': 0})
            
            def count_documents(self, query=None):
                return len(self.data)
            
            def create_index(self, *args, **kwargs):
                pass
        
        # Create memory collections
        links_collection = MemoryCollection()
        webapp_sessions = MemoryCollection()
        users_collection = MemoryCollection()
        channel_verification = MemoryCollection()
        broadcasts_collection = MemoryCollection()
else:
    logger.warning("MONGODB_URI not set. Running in memory-only mode.")
    
    # Create in-memory collections
    class MemoryCollection:
        def __init__(self):
            self.data = {}
        
        def find_one(self, query):
            if "_id" in query:
                return self.data.get(query["_id"])
            return None
        
        def find(self, query=None, **kwargs):
            return list(self.data.values())
        
        def insert_one(self, document):
            if "_id" in document:
                self.data[document["_id"]] = document
            elif "token" in document:
                self.data[document["token"]] = document
            elif "user_id" in document:
                self.data[document["user_id"]] = document
            return type('obj', (object,), {'inserted_id': 'dummy'})
        
        def update_one(self, query, update, **kwargs):
            doc = self.find_one(query)
            if doc and "$set" in update:
                doc.update(update["$set"])
            return type('obj', (object,), {'matched_count': 1 if doc else 0})
        
        def delete_one(self, query):
            key = None
            if "_id" in query:
                key = query.get("_id")
            elif "token" in query:
                key = query.get("token")
            elif "user_id" in query:
                key = query.get("user_id")
            
            if key in self.data:
                del self.data[key]
                return type('obj', (object,), {'deleted_count': 1})
            return type('obj', (object,), {'deleted_count': 0})
        
        def count_documents(self, query=None):
            return len(self.data)
        
        def create_index(self, *args, **kwargs):
            pass
    
    # Create memory collections
    links_collection = MemoryCollection()
    webapp_sessions = MemoryCollection()
    users_collection = MemoryCollection()
    channel_verification = MemoryCollection()
    broadcasts_collection = MemoryCollection()

# --- Helper Functions ---
def generate_encoded_string(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def escape_html(text: str) -> str:
    """Escape text for HTML parse mode"""
    if not text:
        return ""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

async def ensure_user_in_db(user_id: int, username: str = None, first_name: str = None) -> None:
    """Ensure user exists in database"""
    try:
        existing_user = users_collection.find_one({"user_id": user_id})
        
        if existing_user:
            update_data = {
                "last_active": datetime.utcnow(),
                "message_count": existing_user.get("message_count", 0) + 1
            }
            
            if username is not None and username != existing_user.get("username"):
                update_data["username"] = username
            
            if first_name is not None and first_name != existing_user.get("first_name"):
                update_data["first_name"] = first_name
            
            users_collection.update_one(
                {"user_id": user_id},
                {"$set": update_data}
            )
        else:
            user_data = {
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "joined_at": datetime.utcnow(),
                "last_active": datetime.utcnow(),
                "message_count": 1,
                "total_links": 0,
                "total_verifications": 0
            }
            users_collection.insert_one(user_data)
            
    except Exception as e:
        logger.error(f"Error ensuring user in DB: {e}")

async def is_user_in_channel(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of support channel"""
    if not SUPPORT_CHANNEL_ID:
        return True
    
    try:
        channel_id = int(SUPPORT_CHANNEL_ID)
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except BadRequest as e:
        if "user not found" in str(e).lower():
            return False
        elif "chat not found" in str(e).lower():
            return False
        else:
            logger.error(f"Error checking channel membership: {e}")
            return False
    except Forbidden:
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking channel membership: {e}")
        return False

async def send_channel_verification(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str = None) -> None:
    """Send message asking user to join channel"""
    if not SUPPORT_CHANNEL_ID:
        return
    
    try:
        chat = await context.bot.get_chat(int(SUPPORT_CHANNEL_ID))
        invite_link = await context.bot.create_chat_invite_link(
            chat_id=int(SUPPORT_CHANNEL_ID),
            member_limit=1,
            creates_join_request=False
        )
        
        callback_data = f"check_{action}" if action else "check_membership"
        
        keyboard = [
            [InlineKeyboardButton("✅ Join Support Channel", url=invite_link.invite_link)],
            [InlineKeyboardButton("🔁 I've Joined - Check Now", callback_data=callback_data)]
        ]
        
        escaped_title = escape_html(chat.title)
        
        await update.message.reply_text(
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📢 <b>Channel Verification Required</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"To use this bot, you must join our support channel first:\n"
            f"👉 <b>{escaped_title}</b>\n\n"
            f"<b>Instructions:</b>\n"
            f"1️⃣ Click 'Join Support Channel' button above\n"
            f"2️⃣ Join the channel\n"
            f"3️⃣ Come back and click 'I've Joined - Check Now'\n\n"
            f"⚠️ <i>You must join to proceed</i>",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Error creating invite link: {e}")
        await update.message.reply_text(
            "Please contact the admin to get the channel invite link."
        )

def validate_telegram_link(link: str) -> bool:
    """Validate all types of Telegram group links"""
    patterns = [
        r'^https://(t\.me|telegram\.me)/joinchat/[a-zA-Z0-9_-]+$',
        r'^https://(t\.me|telegram\.me)/\+[a-zA-Z0-9_-]+$',
        r'^https://(t\.me|telegram\.me)/[a-zA-Z0-9_]{5,}$',
        r'^https://(t\.me|telegram\.me)/i/[a-zA-Z0-9_-]+$',
        r'^https://(t\.me|telegram\.me)/c/\d+$',
    ]
    
    for pattern in patterns:
        if re.match(pattern, link):
            return True
    
    return False

# --- Telegram Bot Logic ---
telegram_bot_app = Application.builder().token(TELEGRAM_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command"""
    user = update.effective_user
    args = context.args
    
    await ensure_user_in_db(user.id, user.username, user.first_name)
    
    # Check if user is admin
    is_admin = str(user.id) in ADMIN_USER_ID
    
    if not args:
        # Regular start command
        if not is_admin:
            is_member = await is_user_in_channel(user.id, context)
            if not is_member:
                await send_channel_verification(update, context, "start")
                return
        
        # Create beautiful welcome message
        user_name = escape_html(user.first_name) if user.first_name else "User"
        
        welcome_msg = (
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎊 <b>Welcome {user_name}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            
            "🤖 <b>I am a Channel Link Protection Bot</b>\n"
            "<i>ɪ ᴄᴀɴ ʜᴇʟᴘ ʏᴏᴜ ᴘʀᴏᴛᴇᴄᴛ ʏᴏᴜʀ ᴄʜᴀɴɴᴇʟ ʟɪɴᴋꜱ.</i>\n\n"
            
            "🛠 <b>Commands:</b>\n"
            "• /start - Start the bot\n"
            "• /protect - Generate protected link\n"
            "• /help - Show this message\n\n"
            
            "🌟 <b>Features:</b>\n"
            "• 🔒 Advanced Link Protection\n"
            "• 🚀 Instant Link Generation\n"
            "• 📊 Link Analytics\n"
            "• 👥 User Management\n\n"
            
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "💡 <b>How to use:</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "1. Use <code>/protect &lt;your_link&gt;</code>\n"
            "2. Share the protected link\n"
            "3. Users verify via Web App\n"
            "4. Get access to your channel\n\n"
            
            "⚠️ <i>Note: Users must join support channel first</i>"
        )
        
        # Add admin commands if user is admin
        if is_admin:
            welcome_msg += (
                "\n\n━━━━━━━━━━━━━━━━━━━━━━\n"
                "👑 <b>Admin Commands:</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "• /broadcast - Broadcast messages\n"
                "• /stats - View bot statistics\n"
                "• /users - List all users\n"
                "• /health - Check bot health\n"
                "• /help - Show help message"
            )
        
        await update.message.reply_text(welcome_msg, parse_mode="HTML")
        return
    
    # Handle protected link access
    encoded_id = args[0]
    await handle_protected_link(update, context, user, encoded_id)

async def handle_protected_link(update: Update, context: ContextTypes.DEFAULT_TYPE, user, encoded_id: str):
    """Handle protected link access"""
    link_data = links_collection.find_one({"_id": encoded_id})
    
    if not link_data:
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "❌ <b>Invalid Link</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "The link is invalid or has expired.",
            parse_mode="HTML"
        )
        return
    
    # Check if user is admin
    is_admin = str(user.id) in ADMIN_USER_ID
    
    # Check channel membership for non-admin users
    if not is_admin:
        is_member = await is_user_in_channel(user.id, context)
        if not is_member:
            await send_channel_verification(update, context, f"link_{encoded_id}")
            return
    
    # Create Web App session
    webapp_token = str(uuid.uuid4())
    session_data = {
        "token": webapp_token,
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "link_id": encoded_id,
        "group_link": link_data["group_link"],
        "verified": False,
        "created_at": datetime.utcnow(),
        "expires_at": datetime.utcnow() + timedelta(minutes=30),
        "attempts": 0
    }
    
    webapp_sessions.insert_one(session_data)
    
    # Create Web App URL
    web_app_url = f"{RENDER_EXTERNAL_URL}/join?token={webapp_token}"
    
    # Create keyboard with Web App button
    keyboard = [[InlineKeyboardButton(
        "🔐 OPEN VERIFICATION PANEL", 
        web_app=WebAppInfo(url=web_app_url)
    )]]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 <b>PROTECTED LINK ACCESS</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "<b>Click the button below to open the verification panel:</b>\n\n"
        "⚠️ <i>You have 30 minutes to complete verification</i>",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate protected link"""
    user = update.effective_user
    
    # Check if user is admin
    is_admin = str(user.id) in ADMIN_USER_ID
    
    # Check channel membership for non-admin users
    if not is_admin:
        is_member = await is_user_in_channel(user.id, context)
        if not is_member:
            await send_channel_verification(update, context, "protect")
            return
    
    await ensure_user_in_db(user.id, user.username, user.first_name)
    
    if update.effective_chat.type != "private":
        await update.message.reply_text(
            "Please use this command in private chat.",
            parse_mode="HTML"
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 <b>Command Usage</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>Usage:</b> <code>/protect &lt;group_link&gt;</code>\n\n"
            "<b>Supported Link Types:</b>\n"
            "• Public Group: <code>https://t.me/groupname</code>\n"
            "• Approval Link: <code>https://t.me/joinchat/xxxxx</code>\n"
            "• Private Link: <code>https://t.me/+invitecode</code>\n"
            "• Channel Link: <code>https://t.me/c/xxxxx</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/protect https://t.me/joinchat/ABCD1234</code>",
            parse_mode="HTML"
        )
        return
    
    group_link = context.args[0]
    
    if not validate_telegram_link(group_link):
        await update.message.reply_text(
            "❌ <b>Invalid Telegram Link</b>\n\n"
            "Please provide a valid Telegram group/channel link.",
            parse_mode="HTML"
        )
        return
    
    # Generate unique ID
    unique_id = str(uuid.uuid4())
    encoded_id = base64.urlsafe_b64encode(unique_id.encode()).decode().rstrip("=")
    
    link_data = {
        "_id": encoded_id,
        "group_link": group_link,
        "created_by": user.id,
        "created_by_username": user.username,
        "created_at": datetime.utcnow(),
        "access_count": 0,
        "unique_users": [],
        "last_accessed": None
    }
    
    links_collection.insert_one(link_data)
    
    # Update user stats
    users_collection.update_one(
        {"user_id": user.id},
        {"$inc": {"total_links": 1}}
    )
    
    bot_username = (await context.bot.get_me()).username
    protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
    
    # Create share buttons
    share_url = f"https://t.me/share/url?url={protected_link}&text=Join%20via%20protected%20link"
    keyboard = [[
        InlineKeyboardButton("📤 Share Link", url=share_url),
        InlineKeyboardButton("📋 Copy Link", callback_data=f"copy_{encoded_id}")
    ]]
    
    await update.message.reply_text(
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ <b>LINK PROTECTED SUCCESSFULLY!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Protected Link:</b>\n"
        f"<code>{protected_link}</code>\n\n"
        f"<b>Original Link:</b>\n"
        f"<code>{group_link}</code>\n\n"
        f"<b>Link ID:</b> <code>{encoded_id[:8]}...</code>\n"
        f"<b>Expires:</b> 30 days\n\n"
        f"<b>Security Features:</b>\n"
        f"• 🔒 Web App verification required\n"
        f"• 📊 Usage tracking enabled\n"
        f"• ⏰ 30-minute session timeout\n"
        f"• 👥 User authentication",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle callback queries"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data.startswith("check_"):
        action = data[6:]
        user = query.from_user
        
        is_member = await is_user_in_channel(user.id, context)
        
        if is_member:
            if action.startswith("link_"):
                encoded_id = action[5:]
                link_data = links_collection.find_one({"_id": encoded_id})
                if link_data:
                    # Create Web App session
                    webapp_token = str(uuid.uuid4())
                    session_data = {
                        "token": webapp_token,
                        "user_id": user.id,
                        "link_id": encoded_id,
                        "group_link": link_data["group_link"],
                        "verified": False,
                        "created_at": datetime.utcnow(),
                        "expires_at": datetime.utcnow() + timedelta(minutes=30)
                    }
                    webapp_sessions.insert_one(session_data)
                    
                    web_app_url = f"{RENDER_EXTERNAL_URL}/join?token={webapp_token}"
                    keyboard = [[InlineKeyboardButton(
                        "🔐 OPEN VERIFICATION PANEL", 
                        web_app=WebAppInfo(url=web_app_url)
                    )]]
                    
                    await query.edit_message_text(
                        "✅ <b>Channel Verified!</b>\n\n"
                        "Click the button below to open the verification panel:",
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode="HTML"
                    )
            else:
                await query.edit_message_text(
                    "✅ <b>Channel Verified!</b>\n\n"
                    "You can now use the bot.",
                    parse_mode="HTML"
                )
        else:
            await query.edit_message_text(
                "❌ <b>Not a Member Yet</b>\n\n"
                "Please join the channel first.",
                parse_mode="HTML"
            )
    
    elif data.startswith("copy_"):
        encoded_id = data[5:]
        bot_username = context.bot.username
        protected_link = f"https://t.me/{bot_username}?start={encoded_id}"
        
        await query.edit_message_text(
            f"✅ <b>Link Copied!</b>\n\n"
            f"Protected Link:\n<code>{protected_link}</code>",
            parse_mode="HTML"
        )

# --- Admin Commands ---
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin broadcast command"""
    user = update.effective_user
    
    if str(user.id) not in ADMIN_USER_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    if update.message.reply_to_message:
        # Handle media broadcast
        pass
    elif context.args:
        # Handle text broadcast
        all_users = list(users_collection.find({}, {"user_id": 1}))
        
        for user_data in all_users:
            try:
                await context.bot.send_message(
                    chat_id=user_data["user_id"],
                    text=" ".join(context.args),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Failed to send to user {user_data['user_id']}: {e}")
        
        await update.message.reply_text(f"Broadcast sent to {len(all_users)} users.")
    else:
        await update.message.reply_text(
            "Usage:\n"
            "/broadcast message - Text broadcast\n"
            "Reply to a message with /broadcast - Media broadcast"
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show bot statistics"""
    user = update.effective_user
    
    if str(user.id) not in ADMIN_USER_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    total_users = users_collection.count_documents({})
    total_links = links_collection.count_documents({})
    total_sessions = webapp_sessions.count_documents({})
    
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_users = users_collection.count_documents({"last_active": {"$gte": today}})
    
    stats_text = (
        "📊 <b>BOT STATISTICS</b>\n\n"
        f"👥 <b>Users:</b> {total_users}\n"
        f"📈 <b>Active Today:</b> {today_users}\n"
        f"🔗 <b>Protected Links:</b> {total_links}\n"
        f"🔐 <b>Active Sessions:</b> {total_sessions}\n"
    )
    
    await update.message.reply_text(stats_text, parse_mode="HTML")

async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List users"""
    user = update.effective_user
    
    if str(user.id) not in ADMIN_USER_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    users_list = list(users_collection.find().sort("last_active", -1).limit(10))
    
    if not users_list:
        await update.message.reply_text("No users found.")
        return
    
    users_text = "👥 <b>RECENT USERS</b>\n\n"
    
    for i, u in enumerate(users_list):
        username = f"@{u.get('username')}" if u.get('username') else "No username"
        last_active = u.get('last_active', datetime.utcnow())
        days_ago = (datetime.utcnow() - last_active).days
        
        users_text += (
            f"<b>{i+1}.</b> {escape_html(u.get('first_name', 'User'))}\n"
            f"   👤 {escape_html(username)}\n"
            f"   🆔 ID: <code>{u.get('user_id')}</code>\n"
            f"   ⏰ Active: {days_ago} days ago\n\n"
        )
    
    await update.message.reply_text(users_text, parse_mode="HTML")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show help"""
    user = update.effective_user
    is_admin = str(user.id) in ADMIN_USER_ID
    
    help_text = (
        "🤖 <b>PROTECTED LINK BOT HELP</b>\n\n"
        "<b>Commands:</b>\n"
        "• /protect - Create protected link\n"
        "• /start - Start the bot\n"
        "• /help - Show this message\n\n"
        
        "<b>How it works:</b>\n"
        "1. Use /protect with your group link\n"
        "2. Share the generated protected link\n"
        "3. Users verify via Web App\n"
        "4. Users get access to your group\n\n"
        
        "<b>Features:</b>\n"
        "✅ Channel verification\n"
        "✅ Secure Web App interface\n"
        "✅ Usage statistics\n"
        "✅ Link expiration\n"
    )
    
    if is_admin:
        help_text += (
            "\n<b>Admin Commands:</b>\n"
            "• /broadcast - Send message to all users\n"
            "• /stats - View statistics\n"
            "• /users - List users\n"
        )
    
    await update.message.reply_text(help_text, parse_mode="HTML")

async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Health check command"""
    user = update.effective_user
    
    if str(user.id) not in ADMIN_USER_ID:
        await update.message.reply_text("❌ Admin only command.")
        return
    
    try:
        if client:
            client.admin.command('ping')
            mongo_status = "✅ Connected"
        else:
            mongo_status = "❌ Using memory storage"
    except Exception as e:
        mongo_status = f"❌ Error: {e}"
    
    status_text = (
        "🏥 <b>BOT HEALTH STATUS</b>\n\n"
        f"<b>Database:</b> {mongo_status}\n"
        f"<b>Total Users:</b> {users_collection.count_documents({})}\n"
        f"<b>Total Links:</b> {links_collection.count_documents({})}\n"
        f"<b>Server Time:</b> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}\n"
    )
    
    await update.message.reply_text(status_text, parse_mode="HTML")

# Register handlers
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("users", users_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(CommandHandler("health", health_command))
telegram_bot_app.add_handler(CallbackQueryHandler(callback_handler))

# --- FastAPI Web Server Setup ---
app = FastAPI(title="Telegram Protected Link Bot")

@app.on_event("startup")
async def on_startup():
    """Initialize bot"""
    logger.info("Application startup...")
    
    # Test MongoDB connection if available
    if client:
        try:
            client.admin.command('ping')
            logger.info("✅ MongoDB connected successfully on startup")
        except Exception as e:
            logger.error(f"❌ MongoDB connection failed on startup: {e}")
    else:
        logger.warning("⚠️ Running with in-memory storage (data will be lost on restart)")
    
    # Initialize and start PTB
    await telegram_bot_app.initialize()
    await telegram_bot_app.start()
    
    # Set webhook
    webhook_url = f"{RENDER_EXTERNAL_URL}/{TELEGRAM_TOKEN}"
    await telegram_bot_app.bot.set_webhook(
        webhook_url,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True
    )
    logger.info(f"Webhook set to {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    """Shutdown bot"""
    logger.info("Application shutdown...")
    await telegram_bot_app.stop()
    await telegram_bot_app.shutdown()
    if client:
        try:
            client.close()
            logger.info("MongoDB connection closed")
        except:
            pass
    logger.info("Application shutdown complete")

@app.post("/{token}")
async def telegram_webhook(request: Request, token: str):
    """Handle Telegram webhook"""
    if token != TELEGRAM_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")
    
    update_data = await request.json()
    update = Update.de_json(update_data, telegram_bot_app.bot)
    await telegram_bot_app.process_update(update)
    
    return Response(status_code=200)

# --- Web App Endpoints ---

@app.get("/join")
async def join_page(token: str):
    """Direct join page - immediately redirects to Telegram group"""
    try:
        # Check session
        session = webapp_sessions.find_one({"token": token})
        
        if not session:
            # Simple error page
            html_content = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Error</title>
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body {
                        background: #000;
                        color: #f00;
                        font-family: monospace;
                        display: flex;
                        justify-content: center;
                        align-items: center;
                        height: 100vh;
                        margin: 0;
                        text-align: center;
                    }
                    .container {
                        padding: 30px;
                        border: 2px solid #f00;
                        border-radius: 10px;
                        background: rgba(255, 0, 0, 0.1);
                    }
                </style>
            </head>
            <body>
                <div class="container">
                    <h1>❌ ERROR</h1>
                    <p>Invalid or expired session.</p>
                    <p>Please restart from Telegram.</p>
                </div>
            </body>
            </html>
            """
            return HTMLResponse(content=html_content, status_code=404)
        
        # Check if expired
        if session.get("expires_at"):
            if isinstance(session["expires_at"], datetime):
                expires_at = session["expires_at"]
            else:
                expires_at = datetime.fromisoformat(session["expires_at"].replace('Z', '+00:00'))
            
            if expires_at < datetime.utcnow():
                webapp_sessions.delete_one({"token": token})
                # Expired session
                html_content = """
                <!DOCTYPE html>
                <html>
                <head>
                    <title>Expired</title>
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <style>
                        body {
                            background: #000;
                            color: #ff9900;
                            font-family: monospace;
                            display: flex;
                            justify-content: center;
                            align-items: center;
                            height: 100vh;
                            margin: 0;
                            text-align: center;
                        }
                        .container {
                            padding: 30px;
                            border: 2px solid #ff9900;
                            border-radius: 10px;
                            background: rgba(255, 153, 0, 0.1);
                        }
                    </style>
                </head>
                <body>
                    <div class="container">
                        <h1>⏰ EXPIRED</h1>
                        <p>Session has expired (30-minute limit).</p>
                        <p>Please restart from Telegram.</p>
                    </div>
                </body>
                </html>
                """
                return HTMLResponse(content=html_content, status_code=410)
        
        # Mark as verified
        webapp_sessions.update_one(
            {"token": token},
            {"$set": {"verified": True, "verified_at": datetime.utcnow()}}
        )
        
        # Update link statistics
        links_collection.update_one(
            {"_id": session["link_id"]},
            {
                "$inc": {"access_count": 1},
                "$set": {"last_accessed": datetime.utcnow()},
                "$addToSet": {"unique_users": session["user_id"]}
            }
        )
        
        # Update user stats
        users_collection.update_one(
            {"user_id": session["user_id"]},
            {"$inc": {"total_verifications": 1}}
        )
        
        # Create HTML that redirects immediately
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Redirecting to Telegram...</title>
            <meta http-equiv="refresh" content="0;url={session['group_link']}">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{
                    background: #000;
                    color: #0f0;
                    font-family: monospace;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    text-align: center;
                }}
                .container {{
                    padding: 30px;
                }}
                h1 {{
                    color: #0f0;
                    text-shadow: 0 0 10px #0f0;
                }}
                p {{
                    margin: 20px 0;
                }}
                a {{
                    color: #0f0;
                    text-decoration: underline;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>TEAM SECRET</h1>
                <p>Redirecting to Telegram group...</p>
                <p>If not redirected, <a href="{session['group_link']}">click here</a></p>
            </div>
            <script>
                // Immediate redirect
                window.location.href = "{session['group_link']}";
                
                // Try to close WebApp if in Telegram
                setTimeout(function() {{
                    if (window.Telegram && window.Telegram.WebApp) {{
                        window.Telegram.WebApp.close();
                    }}
                }}, 1000);
            </script>
        </body>
        </html>
        """
        
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        logger.error(f"Error in /join endpoint: {e}")
        # Simple error page
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Error</title>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{
                    background: #000;
                    color: #f00;
                    font-family: monospace;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    text-align: center;
                }}
                .container {{
                    padding: 30px;
                    border: 2px solid #f00;
                    border-radius: 10px;
                    background: rgba(255, 0, 0, 0.1);
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>⚠️ ERROR</h1>
                <p>An unexpected error occurred.</p>
                <p>Please try again.</p>
            </div>
        </body>
        </html>
        """
        return HTMLResponse(content=html_content, status_code=500)

@app.get("/api/verify/{token}")
async def verify_session(token: str):
    """API for Web App to verify session"""
    session = webapp_sessions.find_one({"token": token})
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Check if expired
    if session.get("expires_at"):
        if isinstance(session["expires_at"], datetime):
            expires_at = session["expires_at"]
        else:
            expires_at = datetime.fromisoformat(session["expires_at"].replace('Z', '+00:00'))
        
        if expires_at < datetime.utcnow():
            webapp_sessions.delete_one({"token": token})
            raise HTTPException(status_code=410, detail="Session expired")
    
    # Check if user has reached max attempts
    if session.get("attempts", 0) >= 3:
        raise HTTPException(status_code=429, detail="Too many attempts")
    
    return {
        "valid": True,
        "user_id": session["user_id"],
        "username": session.get("username"),
        "first_name": session.get("first_name"),
        "expires_at": session.get("expires_at", datetime.utcnow() + timedelta(minutes=30)).isoformat()
    }

@app.post("/api/verify/{token}/complete")
async def complete_verification(token: str):
    """Complete verification and get group link"""
    session = webapp_sessions.find_one({"token": token})
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Check if expired
    if session.get("expires_at"):
        if isinstance(session["expires_at"], datetime):
            expires_at = session["expires_at"]
        else:
            expires_at = datetime.fromisoformat(session["expires_at"].replace('Z', '+00:00'))
        
        if expires_at < datetime.utcnow():
            webapp_sessions.delete_one({"token": token}")
            raise HTTPException(status_code=410, detail="Session expired")
    
    # Mark as verified
    webapp_sessions.update_one(
        {"token": token},
        {"$set": {"verified": True, "verified_at": datetime.utcnow()}}
    )
    
    # Update link statistics
    link_update = {
        "$inc": {"access_count": 1},
        "$set": {"last_accessed": datetime.utcnow()},
        "$addToSet": {"unique_users": session["user_id"]}
    }
    
    links_collection.update_one({"_id": session["link_id"]}, link_update)
    
    # Update user stats
    users_collection.update_one(
        {"user_id": session["user_id"]},
        {"$inc": {"total_verifications": 1}}
    )
    
    return {
        "success": True,
        "group_link": session["group_link"],
        "message": "Verification complete! Redirecting to group...",
        "verified_at": datetime.utcnow().isoformat()
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        # Test MongoDB connection if available
        if client:
            mongo_status = "connected" if client.admin.command('ping') else "disconnected"
        else:
            mongo_status = "memory_storage"
    except:
        mongo_status = "disconnected"
    
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "mongodb": mongo_status,
        "total_users": users_collection.count_documents({}),
        "total_links": links_collection.count_documents({})
    }

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "Telegram Protected Link Bot",
        "version": "2.0.0",
        "status": "running",
        "mongodb": "connected" if client else "memory_storage",
        "webapp": True,
        "features": ["channel_verification", "webapp_interface", "analytics", "admin_tools"]
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8443))
    uvicorn.run(app, host="0.0.0.0", port=port)