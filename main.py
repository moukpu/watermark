import asyncio
import logging
import os
import aiohttp
import asyncpg
import json
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiocryptopay import AioCryptoPay, Networks

# --- CONFIG ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_URL = os.getenv("CHANNEL_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
PORT = int(os.getenv("PORT", 8080))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
logging.basicConfig(level=logging.INFO)

class States(StatesGroup):
    add_token_val = State()
    add_token_name = State()
    pkg_name = State()
    pkg_att = State()
    pkg_price = State()
    give_amount = State()

# --- DATABASE INIT & MIGRATION ---
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            attempts INTEGER DEFAULT 0,
            total_donated REAL DEFAULT 0,
            total_downloaded INTEGER DEFAULT 0,
            is_banned BOOLEAN DEFAULT FALSE,
            received_free_bonus BOOLEAN DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            name TEXT,
            usage_count INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT FALSE,
            is_auto_switch BOOLEAN DEFAULT TRUE
        );
        CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, user_id BIGINT, token_used TEXT);
        CREATE TABLE IF NOT EXISTS packages (id SERIAL PRIMARY KEY, name TEXT, attempts INTEGER, price_usd REAL);
    """)
    # Migration (Adding columns if they don't exist)
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_donated REAL DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_downloaded INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS is_auto_switch BOOLEAN DEFAULT TRUE")
    except: pass
    await conn.close()
    logging.info("✅ Database & Migrations ready")

# --- TOKEN LOGIC ---
async def get_current_token():
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT token, name FROM tokens WHERE is_active = TRUE LIMIT 1")
    if not row:
        row = await conn.fetchrow("SELECT token, name FROM tokens ORDER BY usage_count ASC LIMIT 1")
    await conn.close()
    return row

async def switch_token_on_error():
    conn = await asyncpg.connect(DATABASE_URL)
    auto = await conn.fetchval("SELECT is_auto_switch FROM tokens LIMIT 1")
    if auto:
        current = await conn.fetchval("SELECT token FROM tokens WHERE is_active = TRUE")
        await conn.execute("UPDATE tokens SET is_active = FALSE")
        new = await conn.fetchrow("SELECT token FROM tokens WHERE token != $1 ORDER BY usage_count ASC LIMIT 1", current)
        if new: await conn.execute("UPDATE tokens SET is_active = TRUE WHERE token = $1", new['token'])
    await conn.close()

# --- USER INTERFACE (ENGLISH) ---
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎁 Get Bonus"), KeyboardButton(text="💳 Buy Attempts")],
        [KeyboardButton(text="👤 Profile")]
    ], resize_keyboard=True)

@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", m.from_user.id)
    await conn.close()
    welcome = (
        "**Welcome to AI Video Master! 🎬**\n\n"
        "I can help you remove watermarks from your videos instantly.\n\n"
        "**How to use:**\n"
        "1️⃣ Send me a video link.\n"
        "2️⃣ Wait for processing.\n"
        "3️⃣ Get your clean video!\n\n"
        "Use the menu below to start."
    )
    await m.answer(welcome, reply_markup=main_kb(), parse_mode="Markdown")

@dp.message(F.text == "👤 Profile")
async def profile(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts, total_downloaded FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    att = u['attempts'] if u else 0
    dl = u['total_downloaded'] if u else 0
    await m.answer(f"👤 **Your Profile**\n\n🆔 ID: `{m.from_user.id}`\n⚡ Attempts: **{att}**\n📥 Processed: **{dl}**", parse_mode="Markdown")

@dp.message(F.text == "🎁 Get Bonus")
async def bonus_info(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Subscribe", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Check", callback_data="check_bonus")]
    ])
    await m.answer("Subscribe to our channel for +1 free attempt!", reply_markup=kb)

@dp.callback_query(F.data == "check_bonus")
async def cb_bonus(c: types.CallbackQuery):
    try:
        st = await bot.get_chat_member(CHANNEL_ID, c.from_user.id)
        if st.status in ["member", "administrator", "creator"]:
            conn = await asyncpg.connect(DATABASE_URL)
            res = await conn.fetchrow("SELECT received_free_bonus FROM users WHERE user_id = $1", c.from_user.id)
            if res and res['received_free_bonus']:
                await c.answer("❌ Already received!", show_alert=True)
            else:
                await conn.execute("UPDATE users SET attempts = attempts + 1, received_free_bonus = TRUE WHERE user_id = $1", c.from_user.id)
                await c.message.answer("✅ +1 attempt added!")
            await conn.close()
        else: await c.answer("❌ Not subscribed!", show_alert=True)
    except: await c.answer("Check error. Is bot admin in channel?", show_alert=True)

@dp.message(F.text == "💳 Buy Attempts")
async def shop(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    pkgs = await conn.fetch("SELECT id, name, price_usd, attempts FROM packages ORDER BY price_usd ASC")
    await conn.close()
    if not pkgs: return await m.answer("Shop is empty.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{p['name']} — {p['price_usd']}$ — {p['attempts']} attempts", callback_data=f"buy_{p['id']}")] for p in pkgs])
    await m.answer("Choose your package:", reply_markup=kb)

# --- ADMIN PANEL (RUSSIAN) ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_main(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Токены", callback_data="adm_tok_list")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="➕ Добавить Токен", callback_data="adm_tok_add")],
        [InlineKeyboardButton(text="📦 Новый Пакет", callback_data="adm_pkg_add")]
    ])
    await m.answer("🛠 Админ-панель", reply_markup=kb)

@dp.callback_query(F.data == "adm_tok_list")
async def adm_tok_list(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    tokens = await conn.fetch("SELECT token, name, is_active, usage_count, is_auto_switch FROM tokens")
    await conn.close()
    auto_s = "✅ ВКЛ" if tokens and tokens[0]['is_auto_switch'] else "❌ ВЫКЛ"
    text = f"⚙️ **Токены** (Автосмена: {auto_s})\n\n"
    buttons = []
    for i, t in enumerate(tokens, 1):
        mark = "✅" if t['is_active'] else ""
        text += f"{i}. {t['name']} | Скачек: {t['usage_count']} {mark}\n"
        buttons.append(InlineKeyboardButton(text=f"{mark if mark else i}", callback_data=f"set_active_{t['token']}"))
    
    rows = [buttons[i:i+5] for i in range(0, len(buttons), 5)]
    rows.append([InlineKeyboardButton(text=f"Автосмена: {auto_s}", callback_data="toggle_auto")])
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_users_"))
async def adm_users_list(c: types.CallbackQuery):
    page = int(c.data.split("_")[2])
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT user_id FROM users LIMIT 5 OFFSET $1", page * 5)
    total = await conn.fetchval("SELECT COUNT(*) FROM users")
    await conn.close()
    kb = [[InlineKeyboardButton(text=f"👤 {u['user_id']}", callback_data=f"u_info_{u['user_id']}_{page}")] for u in users]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_users_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}", callback_data="none"))
    if (page + 1) * 5 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_users_{page+1}"))
    kb.append(nav)
    await c.message.edit_text(f"Юзеры: {total}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("u_info_"))
async def u_info(c: types.CallbackQuery):
    _, _, uid, page = c.data.split("_")
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", int(uid))
    await conn.close()
    text = f"👤 ID: {uid}\n⚡ Попытки: {u['attempts']}\n💰 Донат: ${u['total_donated']}\n📥 Скачано: {u['total_downloaded']}\n🚫 Бан: {u['is_banned']}"
    kb = [[InlineKeyboardButton(text="🚫 Бан/Разбан", callback_data=f"u_ban_{uid}_{page}")],
          [InlineKeyboardButton(text="➕ Дать попытки", callback_data=f"u_give_{uid}")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_users_{page}")]]
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- TOKEN ACTIONS ---
@dp.callback_query(F.data.startswith("set_active_"))
async def set_active_token(c: types.CallbackQuery):
    tok = c.data.replace("set_active_", "")
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE tokens SET is_active = FALSE")
    await conn.execute("UPDATE tokens SET is_active = TRUE WHERE token = $1", tok)
    await conn.close(); await adm_tok_list(c)

@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto_switch(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE tokens SET is_auto_switch = NOT is_auto_switch")
    await conn.close(); await adm_tok_list(c)

# --- VIDEO HANDLER ---
@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts, is_banned FROM users WHERE user_id = $1", m.from_user.id)
    if u and u['is_banned']: return await m.answer("You are banned.")
    if not u or u['attempts'] <= 0: return await m.answer("No attempts left.")
    
    token_row = await get_current_token()
    if not token_row: return await m.answer("Technical error. No active servers.")
    msg = await m.answer("⏳ Processing...")
    
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {token_row['token']}", "Content-Type": "application/json"}
        payload = {"model": "sora-watermark-remover", "input": {"video_url": m.text}, "callBackUrl": WEBHOOK_URL}
        try:
            async with session.post("https://api.kie.ai/api/v1/jobs/createTask", json=payload, headers=headers) as resp:
                res = await resp.json()
                if resp.status == 200 and res.get("code") == 200:
                    await conn.execute("INSERT INTO tasks VALUES ($1, $2, $3)", res["data"]["taskId"], m.from_user.id, token_row['token'])
                else:
                    await switch_token_on_error()
                    await msg.edit_text("⚠️ Server error. Auto-switching server, try again!")
        except: await msg.edit_text("Connection error.")
    await conn.close()

# --- WEBHOOK & PAYMENTS (KIE CALLBACK) ---
async def handle_kie_callback(request):
    try:
        data = await request.json()
        task_id = data.get("taskId") or data.get("data", {}).get("taskId")
        res_json = data.get("data", {}).get("resultJson")
        if task_id and res_json:
            v_url = json.loads(res_json).get("resultUrls", [None])[0]
            if v_url:
                conn = await asyncpg.connect(DATABASE_URL)
                task = await conn.fetchrow("SELECT user_id, token_used FROM tasks WHERE task_id = $1", task_id)
                if task:
                    await bot.send_video(task['user_id'], v_url, caption="✅ Result ready!")
                    await conn.execute("UPDATE users SET attempts = attempts - 1, total_downloaded = total_downloaded + 1 WHERE user_id = $1", task['user_id'])
                    await conn.execute("UPDATE tokens SET usage_count = usage_count + 1 WHERE token = $1", task['token_used'])
                    await conn.execute("DELETE FROM tasks WHERE task_id = $1", task_id)
                await conn.close()
    except: pass
    return web.Response(text="ok")

# --- STARTUP ---
async def main():
    await init_db()
    crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)
    app = web.Application()
    app.router.add_post('/', handle_kie_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    logging.info(f"🚀 Started on port {PORT}")
    await dp.start_polling(bot, crypto=crypto)

if __name__ == "__main__":
    asyncio.run(main())
