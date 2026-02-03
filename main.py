import asyncio
import logging
import os
import sys
import aiohttp
import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiocryptopay import AioCryptoPay, Networks

# --- ПЕРЕМЕННЫЕ ИЗ RAILWAY ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN")
KIE_AI_KEY = os.getenv("KIE_AI_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_URL = os.getenv("CHANNEL_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
DATABASE_URL = os.getenv("DATABASE_URL") # Берем URL твоей базы PostgreSQL
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

# --- РАБОТА С POSTGRESQL ---
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    # Создаем таблицы в твоей базе на Railway
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
            user_id BIGINT
        );
    """)
    await conn.close()
    logging.info("✅ PostgreSQL таблицы проверены/созданы")

# --- WEBHOOK СЕРВЕР ---
async def handle_kie_callback(request):
    try:
        data = await request.json()
        logging.info(f"📥 CALLBACK: {data}")
        
        task_id = data.get("taskId") or data.get("data", {}).get("taskId")
        video_url = data.get("url") or data.get("data", {}).get("url") or data.get("data", {}).get("video_url")
        state = str(data.get("state") or data.get("status") or data.get("data", {}).get("state")).lower()

        if task_id:
            conn = await asyncpg.connect(DATABASE_URL)
            row = await conn.fetchrow("SELECT user_id FROM tasks WHERE task_id = $1", task_id)
            
            if row:
                uid = row['user_id']
                if state in ["succeeded", "success", "200", "complete"] and video_url:
                    await bot.send_video(uid, video_url, caption="✅ Видео готово!")
                    await conn.execute("UPDATE users SET attempts = attempts - 1 WHERE user_id = $1", uid)
                    await conn.execute("DELETE FROM tasks WHERE task_id = $1", task_id)
                else:
                    logging.warning(f"Task {task_id} not ready. State: {state}")
            await conn.close()
        return web.Response(text="ok")
    except Exception as e:
        logging.error(f"Callback error: {e}")
        return web.Response(text="error", status=500)

# --- ЗАПРОС К KIE AI ---
async def create_kie_task(video_url: str, user_id: int):
    api_url = "https://api.kie.ai/api/v1/jobs/createTask"
    headers = {"Authorization": f"Bearer {KIE_AI_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "sora-watermark-remover",
        "input": {"video_url": video_url},
        "callBackUrl": WEBHOOK_URL
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload, headers=headers) as resp:
            res = await resp.json()
            if resp.status == 200 and res.get("code") == 200:
                tid = res["data"]["taskId"]
                conn = await asyncpg.connect(DATABASE_URL)
                await conn.execute("INSERT INTO tasks (task_id, user_id) VALUES ($1, $2)", tid, user_id)
                await conn.close()
                return True
            return False

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", m.from_user.id)
    await conn.close()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Бонус", callback_data="bonus")],
        [InlineKeyboardButton(text="💳 Магазин", callback_data="shop")]
    ])
    await m.answer(f"Присылай ссылку на видео.\nТвой ID: {m.from_user.id}", reply_markup=kb)

@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    user = await conn.fetchrow("SELECT attempts FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    
    if not user or user['attempts'] <= 0:
        return await m.answer("❌ Нет попыток!")
    
    msg = await m.answer("⏳ Обрабатываю...")
    if not await create_kie_task(m.text, m.from_user.id):
        await msg.edit_text("❌ Ошибка API.")

@dp.callback_query(F.data == "bonus")
async def get_bonus(c: types.CallbackQuery):
    try:
        status = await bot.get_chat_member(CHANNEL_ID, c.from_user.id)
        if status.status in ["member", "administrator", "creator"]:
            conn = await asyncpg.connect(DATABASE_URL)
            row = await conn.fetchrow("SELECT received_free_bonus FROM users WHERE user_id = $1", c.from_user.id)
            if row and row['received_free_bonus']:
                await c.answer("Уже брали!", show_alert=True)
            else:
                await conn.execute("UPDATE users SET attempts = attempts + 1, received_free_bonus = TRUE WHERE user_id = $1", c.from_user.id)
                await c.message.answer("✅ +1 попытка!")
            await conn.close()
        else:
            await c.answer("Подпишись на канал!", show_alert=True)
    except:
        await c.answer("Ошибка подписки", show_alert=True)

@dp.callback_query(F.data == "shop")
async def shop(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    pkgs = await conn.fetch("SELECT id, name, price_usd FROM packages")
    await conn.close()
    kb = [[InlineKeyboardButton(text=f"{p['name']} - ${p['price_usd']}", callback_data=f"buy_{p['id']}")] for p in pkgs]
    await c.message.answer("Тарифы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("buy_"))
async def buy(c: types.CallbackQuery, crypto: AioCryptoPay):
    pid = int(c.data.split("_")[1])
    conn = await asyncpg.connect(DATABASE_URL)
    p = await conn.fetchrow("SELECT price_usd, attempts FROM packages WHERE id = $1", pid)
    await conn.close()
    inv = await crypto.create_invoice(asset='USDT', amount=p['price_usd'])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить", url=inv.bot_invoice_url)],
        [InlineKeyboardButton(text="Проверить", callback_data=f"check_{inv.invoice_id}_{p['attempts']}")]
    ])
    await c.message.answer(f"Счет: {p['price_usd']} USDT", reply_markup=kb)

@dp.callback_query(F.data.startswith("check_"))
async def check_p(c: types.CallbackQuery, crypto: AioCryptoPay):
    _, iid, att = c.data.split("_")
    res = await crypto.get_invoices(invoice_ids=int(iid))
    if res and res[0].status == 'paid':
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute("UPDATE users SET attempts = attempts + $1 WHERE user_id = $2", int(att), c.from_user.id)
        await conn.close()
        await c.message.answer("✅ Оплачено!")
    else:
        await c.answer("Не найдено", show_alert=True)

# --- АДМИНКА ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Пакет", callback_data="adm_p"), InlineKeyboardButton(text="👤 Выдать", callback_data="adm_g")]
    ])
    await m.answer("Админ:", reply_markup=kb)

@dp.callback_query(F.data == "adm_p", F.from_user.id == ADMIN_ID)
async def adm_p1(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(States.pkg_name); await c.message.answer("Имя:")
@dp.message(States.pkg_name)
async def adm_p2(m: types.Message, state: FSMContext):
    await state.update_data(n=m.text); await state.set_state(States.pkg_att); await m.answer("Попытки:")
@dp.message(States.pkg_att)
async def adm_p3(m: types.Message, state: FSMContext):
    await state.update_data(a=m.text); await state.set_state(States.pkg_price); await m.answer("Цена:")
@dp.message(States.pkg_price)
async def adm_p4(m: types.Message, state: FSMContext):
    d = await state.get_data()
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO packages (name, attempts, price_usd) VALUES ($1, $2, $3)", d['n'], int(d['a']), float(m.text))
    await conn.close()
    await m.answer("Создано!"); await state.clear()

@dp.callback_query(F.data == "adm_g", F.from_user.id == ADMIN_ID)
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
    await m.answer("Выдано!"); await state.clear()

async def main():
    await init_db()
    crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)
    app = web.Application()
    app.router.add_post('/kie-callback', handle_kie_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    await dp.start_polling(bot, crypto=crypto)

if __name__ == "__main__":
    asyncio.run(main())