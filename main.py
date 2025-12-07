[file name]: main.py
[file content begin]
import os
import logging
import uuid
import base64
import json
import secrets
import string
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from pymongo import MongoClient
from fastapi import FastAPI, Request, Response, HTTPException, Query, Depends
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import HTTPBearer

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
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", secrets.token_hex(32))

if not TELEGRAM_TOKEN or not MONGODB_URI:
    raise Exception("TELEGRAM_TOKEN and MONGODB_URI environment variables are required!")

# --- Database Setup (MongoDB) ---
client = MongoClient(MONGODB_URI)
db_name = "protected_bot_db"
db = client[db_name]

# Collections
links_collection = db["protected_links"]
webapp_sessions = db["webapp_sessions"]
users_collection = db["users"]
channel_verification = db["channel_verification"]
broadcasts_collection = db["broadcasts"]
captcha_attempts = db["captcha_attempts"]
analytics = db["analytics"]

# Create indexes
links_collection.create_index("created_at", expireAfterSeconds=2592000)  # 30 days
links_collection.create_index("encoded", unique=True)
webapp_sessions.create_index("created_at", expireAfterSeconds=1800)  # 30 minutes
webapp_sessions.create_index("token", unique=True)
users_collection.create_index("user_id", unique=True)
users_collection.create_index("last_active")
broadcasts_collection.create_index("broadcast_id", unique=True)

# --- Helper Functions ---
def generate_encoded_string(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(length))

def generate_captcha_code() -> str:
    """Generate unique CAPTCHA code"""
    return ''.join(secrets.choice(string.digits) for _ in range(5))

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
    if args[0].startswith("verify_"):
        encoded_id = args[0][7:]
        await handle_protected_link(update, context, user, encoded_id)
    else:
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
    
    # Create Web App URL with your hacker theme
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
        "⚠️ <i>You have 30 minutes to complete verification</i>\n"
        "🔐 <i>Secure Web App verification required</i>",
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
        await broadcast_replied(update, context)
    elif context.args:
        await broadcast_text(update, context)
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

# Register handlers
telegram_bot_app.add_handler(CommandHandler("start", start))
telegram_bot_app.add_handler(CommandHandler("protect", protect_command))
telegram_bot_app.add_handler(CommandHandler("broadcast", broadcast_command))
telegram_bot_app.add_handler(CommandHandler("stats", stats_command))
telegram_bot_app.add_handler(CommandHandler("users", users_command))
telegram_bot_app.add_handler(CommandHandler("help", help_command))
telegram_bot_app.add_handler(CallbackQueryHandler(callback_handler))

# --- FastAPI Web Server Setup ---
app = FastAPI(title="Telegram Protected Link Bot")

# Initialize templates with your HTML
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
async def on_startup():
    """Initialize bot"""
    logger.info("Application startup...")
    
    # Initialize database
    try:
        client.admin.command('ismaster')
        logger.info("MongoDB connected successfully")
    except Exception as e:
        logger.error(f"MongoDB connection failed: {e}")
    
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
    client.close()
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
async def join_page(request: Request, token: str):
    """Serve your awesome hacker-themed HTML"""
    session = webapp_sessions.find_one({"token": token})
    
    if not session:
        return templates.TemplateResponse("error.html", {
            "request": request,
            "error": "Invalid or expired session"
        })
    
    # Check if expired
    if session["expires_at"] < datetime.utcnow():
        webapp_sessions.delete_one({"token": token})
        return templates.TemplateResponse("error.html", {
            "request": request,
            "error": "Session expired"
        })
    
    return templates.TemplateResponse("join.html", {
        "request": request,
        "token": token
    })

@app.get("/api/verify/{token}")
async def verify_session(token: str):
    """API for Web App to verify session"""
    session = webapp_sessions.find_one({"token": token})
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if session["expires_at"] < datetime.utcnow():
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
        "expires_at": session["expires_at"].isoformat()
    }

@app.post("/api/verify/{token}/complete")
async def complete_verification(token: str):
    """Complete verification and get group link"""
    session = webapp_sessions.find_one({"token": token})
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    if session["expires_at"] < datetime.utcnow():
        webapp_sessions.delete_one({"token": token})
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
    
    # Record analytics
    analytics.insert_one({
        "user_id": session["user_id"],
        "link_id": session["link_id"],
        "action": "verification_complete",
        "timestamp": datetime.utcnow(),
        "ip": request.client.host if request else None
    })
    
    return {
        "success": True,
        "group_link": session["group_link"],
        "message": "Verification complete! Redirecting to group...",
        "verified_at": datetime.utcnow().isoformat()
    }

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "bot_username": (await telegram_bot_app.bot.get_me()).username,
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
        "webapp": True,
        "features": ["channel_verification", "webapp_interface", "analytics", "admin_tools"]
    }

if __name__ == "__main__":
    import uvicorn
    import re  # Import re for link validation
    
    port = int(os.environ.get("PORT", 8443))
    uvicorn.run(app, host="0.0.0.0", port=port)
[file content end]