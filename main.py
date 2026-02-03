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

# --- DATABASE INIT ---
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
    # Migrations
    try:
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_donated REAL DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS total_downloaded INTEGER DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_banned BOOLEAN DEFAULT FALSE")
        await conn.execute("ALTER TABLE tokens ADD COLUMN IF NOT EXISTS is_auto_switch BOOLEAN DEFAULT TRUE")
    except: pass
    await conn.close()
    logging.info("✅ DB Ready")

# --- UTILS ---
async def get_current_token():
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT token FROM tokens WHERE is_active = TRUE LIMIT 1")
    if not row:
        row = await conn.fetchrow("SELECT token FROM tokens ORDER BY usage_count ASC LIMIT 1")
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

def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎁 Get Bonus"), KeyboardButton(text="💳 Buy Attempts")],
        [KeyboardButton(text="👤 Profile")]
    ], resize_keyboard=True)

# --- USER HANDLERS ---
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", m.from_user.id)
    await conn.close()
    await m.answer("Welcome! Send a video link to remove watermark.", reply_markup=main_kb())

@dp.message(F.text == "👤 Profile")
async def profile(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts, total_downloaded FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    await m.answer(f"👤 **Profile**\n\nID: `{m.from_user.id}`\nAttempts: **{u['attempts']}**\nDone: **{u['total_downloaded']}**", parse_mode="Markdown")

@dp.message(F.text == "🎁 Get Bonus")
async def bonus(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Subscribe", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Check", callback_data="check_bonus")]
    ])
    await m.answer("Subscribe to get +1 attempt!", reply_markup=kb)

@dp.callback_query(F.data == "check_bonus")
async def cb_check_bonus(c: types.CallbackQuery):
    try:
        st = await bot.get_chat_member(CHANNEL_ID, c.from_user.id)
        if st.status in ["member", "administrator", "creator"]:
            conn = await asyncpg.connect(DATABASE_URL)
            u = await conn.fetchrow("SELECT received_free_bonus FROM users WHERE user_id = $1", c.from_user.id)
            if u and u['received_free_bonus']:
                await c.answer("Already taken!", show_alert=True)
            else:
                await conn.execute("UPDATE users SET attempts = attempts + 1, received_free_bonus = TRUE WHERE user_id = $1", c.from_user.id)
                await c.message.answer("✅ Success! +1 added.")
            await conn.close()
        else: await c.answer("Subscribe first!", show_alert=True)
    except: await c.answer("Error. Is bot admin in channel?", show_alert=True)

@dp.message(F.text == "💳 Buy Attempts")
async def shop(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    pkgs = await conn.fetch("SELECT id, name, price_usd, attempts FROM packages ORDER BY price_usd ASC")
    await conn.close()
    if not pkgs: return await m.answer("Shop is empty.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{p['name']} — {p['price_usd']}$ — {p['attempts']} att.", callback_data=f"buy_{p['id']}")] for p in pkgs])
    await m.answer("Choose a package:", reply_markup=kb)

# --- ADMIN HANDLERS (RUSSIAN) ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def adm_panel(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Токены", callback_data="adm_tok_list")],
        [InlineKeyboardButton(text="👥 Юзеры", callback_data="adm_users_0")],
        [InlineKeyboardButton(text="➕ Добавить Токен", callback_data="adm_tok_add")],
        [InlineKeyboardButton(text="📦 Новый Пакет", callback_data="adm_pkg_add")]
    ])
    await m.answer("🛠 Админка", reply_markup=kb)

@dp.callback_query(F.data == "adm_tok_list")
async def adm_tok_list(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    tokens = await conn.fetch("SELECT token, name, is_active, usage_count, is_auto_switch FROM tokens")
    await conn.close()
    auto_s = "✅ ВКЛ" if tokens and tokens[0]['is_auto_switch'] else "❌ ВЫКЛ"
    text = f"⚙️ Токены (Автосмена: {auto_s})\n\n"
    btns = []
    for i, t in enumerate(tokens, 1):
        mark = "✅" if t['is_active'] else ""
        text += f"{i}. {t['name']} ({t['usage_count']}) {mark}\n"
        btns.append(InlineKeyboardButton(text=f"{mark if mark else i}", callback_data=f"set_act_{t['token']}"))
    rows = [btns[i:i+5] for i in range(0, len(btns), 5)]
    rows.append([InlineKeyboardButton(text=f"Автосмена: {auto_s}", callback_data="toggle_auto")])
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

@dp.callback_query(F.data.startswith("set_act_"))
async def cb_set_act(c: types.CallbackQuery):
    t = c.data.replace("set_act_", "")
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE tokens SET is_active = FALSE; UPDATE tokens SET is_active = TRUE WHERE token = $1", t)
    await conn.close(); await adm_tok_list(c)

@dp.callback_query(F.data == "toggle_auto")
async def cb_toggle_auto(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE tokens SET is_auto_switch = NOT is_auto_switch")
    await conn.close(); await adm_tok_list(c)

@dp.callback_query(F.data.startswith("adm_users_"))
async def adm_users(c: types.CallbackQuery):
    page = int(c.data.split("_")[2]); conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT user_id FROM users LIMIT 5 OFFSET $1", page*5)
    total = await conn.fetchval("SELECT COUNT(*) FROM users"); await conn.close()
    kb = [[InlineKeyboardButton(text=f"👤 {u['user_id']}", callback_data=f"u_inf_{u['user_id']}_{page}")] for u in users]
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_users_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}", callback_data="none"))
    if (page+1)*5 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_users_{page+1}"))
    kb.append(nav); await c.message.edit_text(f"Юзеров: {total}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("u_inf_"))
async def u_inf(c: types.CallbackQuery):
    _, _, uid, page = c.data.split("_"); conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", int(uid)); await conn.close()
    text = f"👤 ID: {uid}\n⚡ Попытки: {u['attempts']}\n💰 Донат: ${u['total_donated']}\n📥 Скачано: {u['total_downloaded']}\n🚫 Бан: {u['is_banned']}"
    kb = [[InlineKeyboardButton(text="🚫 Бан/Разбан", callback_data=f"u_ban_{uid}_{page}")],
          [InlineKeyboardButton(text="➕ Дать попытки", callback_data=f"u_giv_{uid}")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_users_{page}")]]
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("u_ban_"))
async def cb_ban(c: types.CallbackQuery):
    _, _, uid, page = c.data.split("_"); conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE users SET is_banned = NOT is_banned WHERE user_id = $1", int(uid))
    await conn.close(); await u_inf(c)

@dp.callback_query(F.data.startswith("u_giv_"))
async def cb_giv_start(c: types.CallbackQuery, state: FSMContext):
    uid = c.data.split("_")[2]; await state.update_data(tg_uid=uid)
    await state.set_state(States.give_amount); await c.message.answer(f"Сколько попыток выдать {uid}?"); await c.answer()

@dp.message(States.give_amount)
async def cb_giv_end(m: types.Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("Введите число.")
    d = await state.get_data(); uid = int(d['tg_uid']); amt = int(m.text)
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE users SET attempts = attempts + $1 WHERE user_id = $2", amt, uid)
    await conn.close(); await m.answer(f"✅ Выдано {amt} юзеру {uid}"); await state.clear()

# --- ADD TOKEN / PACKAGE ---
@dp.callback_query(F.data == "adm_tok_add")
async def add_tok_1(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(States.add_token_val); await c.message.answer("Пришли API KEY:"); await c.answer()
@dp.message(States.add_token_val)
async def add_tok_2(m: types.Message, state: FSMContext):
    await state.update_data(v=m.text); await state.set_state(States.add_token_name); await m.answer("Название:")
@dp.message(States.add_token_name)
async def add_tok_3(m: types.Message, state: FSMContext):
    v = (await state.get_data())['v']; conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO tokens (token, name) VALUES ($1, $2) ON CONFLICT (token) DO UPDATE SET name = $2", v, m.text)
    await conn.close(); await m.answer("✅ Добавлен"); await state.clear()

@dp.callback_query(F.data == "adm_pkg_add")
async def add_pkg_1(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(States.pkg_name); await c.message.answer("Имя пакета:"); await c.answer()
@dp.message(States.pkg_name)
async def add_pkg_2(m: types.Message, state: FSMContext):
    await state.update_data(n=m.text); await state.set_state(States.pkg_att); await m.answer("Попыток:")
@dp.message(States.pkg_att)
async def add_pkg_3(m: types.Message, state: FSMContext):
    await state.update_data(a=m.text); await state.set_state(States.pkg_price); await m.answer("Цена ($):")
@dp.message(States.pkg_price)
async def add_pkg_4(m: types.Message, state: FSMContext):
    d = await state.get_data(); conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO packages (name, attempts, price_usd) VALUES ($1, $2, $3)", d['n'], int(d['a']), float(m.text))
    await conn.close(); await m.answer("✅ Пакет создан"); await state.clear()

# --- PROCESSING ---
@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts, is_banned FROM users WHERE user_id = $1", m.from_user.id)
    if u and u['is_banned']: return await m.answer("Banned.")
    if not u or u['attempts'] <= 0: return await m.answer("No attempts.")
    
    t_row = await get_current_token(); 
    if not t_row: return await m.answer("Technical error: No servers.")
    msg = await m.answer("⏳ Processing..."); h = {"Authorization": f"Bearer {t_row['token']}", "Content-Type": "application/json"}
    p = {"model": "sora-watermark-remover", "input": {"video_url": m.text}, "callBackUrl": WEBHOOK_URL}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.kie.ai/api/v1/jobs/createTask", json=p, headers=h) as resp:
                res = await resp.json()
                if resp.status == 200 and res.get("code") == 200:
                    await conn.execute("INSERT INTO tasks VALUES ($1, $2, $3)", res["data"]["taskId"], m.from_user.id, t_row['token'])
                else:
                    await switch_token_on_error(); await msg.edit_text("⚠️ Server error. Switching, try again.")
        except: await msg.edit_text("Network error.")
    await conn.close()

# --- WEBHOOK ---
async def handle_kie(request):
    try:
        data = await request.json(); tid = data.get("taskId") or data.get("data", {}).get("taskId")
        rj = data.get("data", {}).get("resultJson")
        if tid and rj:
            v_url = json.loads(rj).get("resultUrls", [None])[0]
            if v_url:
                conn = await asyncpg.connect(DATABASE_URL)
                task = await conn.fetchrow("SELECT user_id, token_used FROM tasks WHERE task_id = $1", tid)
                if task:
                    await bot.send_video(task['user_id'], v_url, caption="✅ Success!")
                    await conn.execute("UPDATE users SET attempts = attempts - 1, total_downloaded = total_downloaded + 1 WHERE user_id = $1", task['user_id'])
                    await conn.execute("UPDATE tokens SET usage_count = usage_count + 1 WHERE token = $1", task['token_used'])
                    await conn.execute("DELETE FROM tasks WHERE task_id = $1", tid)
                await conn.close()
    except: pass
    return web.Response(text="ok")

# --- PAYMENTS ---
@dp.callback_query(F.data.startswith("buy_"))
async def buy_att(c: types.CallbackQuery, crypto: AioCryptoPay):
    pid = int(c.data.split("_")[1]); conn = await asyncpg.connect(DATABASE_URL)
    p = await conn.fetchrow("SELECT price_usd, attempts FROM packages WHERE id = $1", pid); await conn.close()
    inv = await crypto.create_invoice(asset='USDT', amount=p['price_usd'])
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Pay", url=inv.bot_invoice_url)], [InlineKeyboardButton(text="✅ Check", callback_data=f"chk_{inv.invoice_id}_{p['attempts']}")]])
    await c.message.answer(f"Refill {p['attempts']} attempts for {p['price_usd']}$", reply_markup=kb)

@dp.callback_query(F.data.startswith("chk_"))
async def chk_pay(c: types.CallbackQuery, crypto: AioCryptoPay):
    _, iid, att = c.data.split("_")
    try:
        # Получаем инвойс. Библиотека возвращает список, если передано несколько ID, 
        # или один объект, если ID один. Проверяем статус корректно.
        res = await crypto.get_invoices(invoice_ids=int(iid))
        
        # Если пришел список — берем первый элемент, если объект — работаем напрямую
        invoice = res[0] if isinstance(res, list) else res
        
        if invoice and invoice.status == 'paid':
            conn = await asyncpg.connect(DATABASE_URL)
            await conn.execute(
                "UPDATE users SET attempts = attempts + $1, total_donated = total_donated + $2 WHERE user_id = $3", 
                int(att), float(invoice.amount), c.from_user.id
            )
            await conn.close()
            await c.message.answer("✅ Payment confirmed! Your attempts have been added.")
        else:
            # Если статус не 'paid'
            await c.message.answer("❌ Payment not found. Please make sure you have completed the transaction.")
            
    except Exception as e:
        logging.error(f"Payment check error: {e}")
        await c.message.answer("⚠️ Sorry, there was an error checking your payment. Please try again in a moment.")

async def main():
    await init_db(); crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)
    app = web.Application(); app.router.add_post('/', handle_kie)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    await dp.start_polling(bot, crypto=crypto)

if __name__ == "__main__":
    asyncio.run(main())
