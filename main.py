import asyncio
import logging
import os
import sys
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

# --- ПЕРЕМЕННЫЕ (RAILWAY) ---
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

class States(StatesGroup):
    pkg_name = State()
    pkg_att = State()
    pkg_price = State()
    give_user_id = State()
    give_amount = State()
    add_token = State()

# --- ИНИЦИАЛИЗАЦИЯ POSTGRESQL ---
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            attempts INTEGER DEFAULT 0,
            received_free_bonus BOOLEAN DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS packages (
            id SERIAL PRIMARY KEY,
            name TEXT,
            attempts INTEGER,
            price_usd REAL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            user_id BIGINT,
            token_used TEXT
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            usage_count INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE
        );
    """)
    # Если в Variables есть KIE_AI_KEY, добавим его как первый токен, если таблица пуста
    first_key = os.getenv("KIE_AI_KEY")
    if first_key:
        await conn.execute("INSERT INTO tokens (token) VALUES ($1) ON CONFLICT DO NOTHING", first_key)
    await conn.close()
    logging.info("✅ БД и Таблица токенов настроены")

# --- ГЛАВНОЕ МЕНЮ (КЛАВИАТУРА) ---
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎁 Получить бонус"), KeyboardButton(text="💳 Купить попытки")],
        [KeyboardButton(text="👤 Профиль")]
    ], resize_keyboard=True)

# --- УТИЛИТА: ПОЛУЧИТЬ РАБОЧИЙ ТОКЕН ---
async def get_active_token():
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT token FROM tokens WHERE is_active = TRUE ORDER BY usage_count ASC LIMIT 1")
    await conn.close()
    return row['token'] if row else None

# --- WEBHOOK ОБРАБОТЧИК ---
async def handle_kie_callback(request):
    try:
        data = await request.json()
        task_id = data.get("taskId") or data.get("data", {}).get("taskId")
        state = str(data.get("state") or data.get("status") or data.get("data", {}).get("state")).lower()
        
        video_url = None
        res_json_str = data.get("data", {}).get("resultJson")
        if res_json_str:
            res_data = json.loads(res_json_str)
            urls = res_data.get("resultUrls", [])
            if urls: video_url = urls[0]

        if task_id and video_url:
            conn = await asyncpg.connect(DATABASE_URL)
            row = await conn.fetchrow("SELECT user_id, token_used FROM tasks WHERE task_id = $1", task_id)
            if row and state in ["success", "succeeded", "complete"]:
                uid, token = row['user_id'], row['token_used']
                await bot.send_video(uid, video_url, caption="✅ Готово!")
                await conn.execute("UPDATE users SET attempts = attempts - 1 WHERE user_id = $1", uid)
                await conn.execute("UPDATE tokens SET usage_count = usage_count + 1 WHERE token = $1", token)
                await conn.execute("DELETE FROM tasks WHERE task_id = $1", task_id)
            await conn.close()
        return web.Response(text="ok")
    except Exception as e:
        logging.error(f"Callback error: {e}")
        return web.Response(text="error")

# --- СОЗДАНИЕ ЗАДАЧИ ---
async def create_kie_task(video_url: str, user_id: int):
    token = await get_active_token()
    if not token: return "no_token"
    
    api_url = "https://api.kie.ai/api/v1/jobs/createTask"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"model": "sora-watermark-remover", "input": {"video_url": video_url}, "callBackUrl": WEBHOOK_URL}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload, headers=headers) as resp:
            res = await resp.json()
            if resp.status == 200 and res.get("code") == 200:
                tid = res["data"]["taskId"]
                conn = await asyncpg.connect(DATABASE_URL)
                await conn.execute("INSERT INTO tasks (task_id, user_id, token_used) VALUES ($1, $2, $3)", tid, user_id, token)
                await conn.close()
                return "ok"
            return "error"

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", m.from_user.id)
    await conn.close()
    await m.answer("Добро пожаловать! Присылай ссылку на видео для удаления водяного знака.", reply_markup=main_kb())

@dp.message(F.text == "👤 Профиль")
async def profile(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    user = await conn.fetchrow("SELECT attempts FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    await m.answer(f"🆔 Твой ID: `{m.from_user.id}`\n⚡ Доступно попыток: {user['attempts'] if user else 0}", parse_mode="Markdown")

@dp.message(F.text == "🎁 Получить бонус")
async def bonus_info(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔗 Подписаться", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить и забрать", callback_data="check_bonus")]
    ])
    await m.answer(f"Для получения бонуса нужно подписаться на наш канал!", reply_markup=kb)

@dp.callback_query(F.data == "check_bonus")
async def check_bonus(c: types.CallbackQuery):
    try:
        status = await bot.get_chat_member(CHANNEL_ID, c.from_user.id)
        if status.status in ["member", "administrator", "creator"]:
            conn = await asyncpg.connect(DATABASE_URL)
            row = await conn.fetchrow("SELECT received_free_bonus FROM users WHERE user_id = $1", c.from_user.id)
            if row['received_free_bonus']:
                await c.answer("Вы уже получили свой бонус!", show_alert=True)
            else:
                await conn.execute("UPDATE users SET attempts = attempts + 1, received_free_bonus = TRUE WHERE user_id = $1", c.from_user.id)
                await c.message.answer("✅ Вам начислена +1 попытка!")
            await conn.close()
        else:
            await c.answer("Вы еще не подписались!", show_alert=True)
    except:
        await c.answer("Ошибка проверки подписки.", show_alert=True)

@dp.message(F.text == "💳 Купить попытки")
async def shop_btn(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    pkgs = await conn.fetch("SELECT id, name, price_usd FROM packages")
    await conn.close()
    if not pkgs: return await m.answer("Магазин пуст.")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"{p['name']} - ${p['price_usd']}", callback_data=f"buy_{p['id']}")] for p in pkgs])
    await m.answer("Выберите количество попыток:", reply_markup=kb)

@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    user = await conn.fetchrow("SELECT attempts FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    if not user or user['attempts'] <= 0: return await m.answer("❌ У вас 0 попыток.")
    
    res = await create_kie_task(m.text, m.from_user.id)
    if res == "ok": await m.answer("⏳ Видео обрабатывается...")
    elif res == "no_token": await m.answer("❌ Нет доступных API ключей.")
    else: await m.answer("❌ Ошибка API.")

# --- АДМИНКА ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_panel(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Токены", callback_data="adm_tokens")],
        [InlineKeyboardButton(text="➕ Добавить токен", callback_data="add_tok")],
        [InlineKeyboardButton(text="👤 Выдать попытки", callback_data="adm_g")]
    ])
    await m.answer("⚙️ Админ-панель", reply_markup=kb)

@dp.callback_query(F.data == "adm_tokens")
async def list_tokens(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT token, usage_count, is_active FROM tokens")
    await conn.close()
    text = "📜 Список токенов:\n\n"
    for r in rows:
        status = "✅" if r['is_active'] else "❌"
        text += f"Key: `{r['token'][:10]}...`\nСтатус: {status} | Использовано: {r['usage_count']}\n\n"
    await c.message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "add_tok")
async def add_tok_start(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(States.add_token)
    await c.message.answer("Пришли новый API токен Kie AI:")

@dp.message(States.add_token)
async def add_tok_finish(m: types.Message, state: FSMContext):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO tokens (token) VALUES ($1) ON CONFLICT DO NOTHING", m.text)
    await conn.close()
    await m.answer("✅ Токен добавлен!")
    await state.clear()

# (Стандартные админ-функции выдачи и покупки остаются)
@dp.callback_query(F.data.startswith("buy_"))
async def buy_proc(c: types.CallbackQuery, crypto: AioCryptoPay):
    pid = int(c.data.split("_")[1])
    conn = await asyncpg.connect(DATABASE_URL)
    p = await conn.fetchrow("SELECT price_usd, attempts FROM packages WHERE id = $1", pid)
    await conn.close()
    inv = await crypto.create_invoice(asset='USDT', amount=p['price_usd'])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить", url=inv.bot_invoice_url)],
        [InlineKeyboardButton(text="Проверить", callback_data=f"check_{inv.invoice_id}_{p['attempts']}")]
    ])
    await c.message.answer(f"Счет на {p['price_usd']} USDT", reply_markup=kb)

@dp.callback_query(F.data.startswith("check_"))
async def check_p(c: types.CallbackQuery, crypto: AioCryptoPay):
    _, iid, att = c.data.split("_")
    res = await crypto.get_invoices(invoice_ids=int(iid))
    if res and res[0].status == 'paid':
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute("UPDATE users SET attempts = attempts + $1 WHERE user_id = $2", int(att), c.from_user.id)
        await conn.close()
        await c.message.answer("✅ Попытки начислены!")
    else: await c.answer("Не оплачено", show_alert=True)

@dp.callback_query(F.data == "adm_g")
async def adm_g1(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(States.give_user_id); await c.message.answer("ID юзера:")
@dp.message(States.give_user_id)
async def adm_g2(m: types.Message, state: FSMContext):
    await state.update_data(uid=m.text); await state.set_state(States.give_amount); await m.answer("Сколько?")
@dp.message(States.give_amount)
async def adm_g3(m: types.Message, state: FSMContext):
    d = await state.get_data()
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE users SET attempts = attempts + $1 WHERE user_id = $2", int(m.text), int(d['uid']))
    await conn.close()
    await m.answer("✅ Готово!"); await state.clear()

async def main():
    await init_db()
    crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)
    app = web.Application()
    app.router.add_post('/', handle_kie_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    await dp.start_polling(bot, crypto=crypto)

if __name__ == "__main__":
    asyncio.run(main())