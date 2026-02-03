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

# --- КОНФИГ ---
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
    give_amount = State()

# --- БАЗА ДАННЫХ ---
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
    await conn.close()
    logging.info("✅ Database initialized")

# --- ЛОГИКА ТОКЕНОВ ---
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
        if new:
            await conn.execute("UPDATE tokens SET is_active = TRUE WHERE token = $1", new['token'])
    await conn.close()

# --- КЛАВИАТУРЫ ---
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎁 Получить бонус"), KeyboardButton(text="💳 Купить попытки")],
        [KeyboardButton(text="👤 Профиль")]
    ], resize_keyboard=True)

# --- ХЕНДЛЕРЫ ЮЗЕРА ---
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", m.from_user.id)
    await conn.close()
    await m.answer("Welcome! Send me a video link to remove watermark.", reply_markup=main_kb())

@dp.message(F.text == "👤 Профиль")
async def profile(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    await m.answer(f"👤 Profile\nID: `{m.from_user.id}`\nAttempts: {u['attempts'] if u else 0}", parse_mode="Markdown")

# --- АДМИНКА (МЕНЮ) ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Токены (Выбор/Авто)", callback_data="adm_tok_list")],
        [InlineKeyboardButton(text="👥 Пользователи (База)", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="➕ Добавить токен", callback_data="adm_tok_add")]
    ])
    await m.answer("🛠 Admin Panel", reply_markup=kb)

# --- УПРАВЛЕНИЕ ТОКЕНАМИ ---
@dp.callback_query(F.data == "adm_tok_list")
async def adm_tok_list(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    tokens = await conn.fetch("SELECT token, name, is_active, usage_count, is_auto_switch FROM tokens")
    await conn.close()
    if not tokens: return await c.answer("No tokens found")
    
    auto_s = "✅ ON" if tokens[0]['is_auto_switch'] else "❌ OFF"
    text = f"⚙️ **Tokens Management**\nAuto-switch: {auto_s}\n\n"
    buttons = []
    for i, t in enumerate(tokens, 1):
        mark = "✅" if t['is_active'] else ""
        text += f"{i}. {t['name']} | Used: {t['usage_count']} {mark}\n"
        buttons.append(InlineKeyboardButton(text=f"{mark if mark else i}", callback_data=f"set_active_{t['token']}"))
    
    kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    kb_rows.append([InlineKeyboardButton(text=f"Auto-switch: {auto_s}", callback_data="toggle_auto")])
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("set_active_"))
async def set_active(c: types.CallbackQuery):
    tok = c.data.replace("set_active_", "")
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE tokens SET is_active = FALSE")
    await conn.execute("UPDATE tokens SET is_active = TRUE WHERE token = $1", tok)
    await conn.close()
    await adm_tok_list(c)

@dp.callback_query(F.data == "toggle_auto")
async def toggle_auto(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE tokens SET is_auto_switch = NOT is_auto_switch")
    await conn.close()
    await adm_tok_list(c)

# --- УПРАВЛЕНИЕ ЮЗЕРАМИ ---
@dp.callback_query(F.data.startswith("adm_users_"))
async def adm_users(c: types.CallbackQuery):
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
    await c.message.edit_text(f"Users: {total}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("u_info_"))
async def u_info(c: types.CallbackQuery):
    _, _, uid, page = c.data.split("_")
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", int(uid))
    await conn.close()
    text = f"User: {uid}\nAttempts: {u['attempts']}\nDonated: ${u['total_donated']}\nDownloads: {u['total_downloaded']}"
    kb = [[InlineKeyboardButton(text="Ban/Unban", callback_data=f"u_ban_{uid}_{page}")],
          [InlineKeyboardButton(text="Back", callback_data=f"adm_users_{page}")]]
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- ОБРАБОТКА ССЫЛОК ---
@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts, is_banned FROM users WHERE user_id = $1", m.from_user.id)
    if u and u['is_banned']: return await m.answer("Banned.")
    if not u or u['attempts'] <= 0: return await m.answer("No attempts.")
    
    token_row = await get_current_token()
    if not token_row: return await m.answer("Technical error: No active tokens.")

    msg = await m.answer("⏳ Processing...")
    headers = {"Authorization": f"Bearer {token_row['token']}", "Content-Type": "application/json"}
    payload = {"model": "sora-watermark-remover", "input": {"video_url": m.text}, "callBackUrl": WEBHOOK_URL}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.kie.ai/api/v1/jobs/createTask", json=payload, headers=headers) as resp:
                res = await resp.json()
                if resp.status == 200 and res.get("code") == 200:
                    await conn.execute("INSERT INTO tasks VALUES ($1, $2, $3)", res["data"]["taskId"], m.from_user.id, token_row['token'])
                    await conn.close()
                else:
                    await switch_token_on_error()
                    await msg.edit_text("Error. Admin notified, trying to switch server.")
                    await bot.send_message(ADMIN_ID, f"Token {token_row['name']} failed. Auto-switched.")
        except:
            await msg.edit_text("Network error.")
    await conn.close()

# --- ВЕБХУК КИЕ ---
async def handle_kie_callback(request):
    # Логика принятия видео от Kie...
    return web.Response(text="ok")

# --- ЗАПУСК ---
async def main():
    await init_db()
    crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)
    
    # Web server для коллбеков
    app = web.Application()
    app.router.add_post('/', handle_kie_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    
    # Start Polling
    await dp.start_polling(bot, crypto=crypto)

if __name__ == "__main__":
    asyncio.run(main())
