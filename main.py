import asyncio
import logging
import os
import sys
import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiocryptopay import AioCryptoPay, Networks

# --- ЗАГРУЗКА ПЕРЕМЕННЫХ ИЗ ОКРУЖЕНИЯ (RAILWAY) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
CRYPTO_TOKEN = os.getenv("CRYPTO_TOKEN")
KIE_AI_KEY = os.getenv("KIE_AI_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_URL = os.getenv("CHANNEL_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

DB_NAME = "bot_database.db"

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

class States(StatesGroup):
    pkg_name = State()
    pkg_att = State()
    pkg_price = State()
    give_user_id = State()
    give_amount = State()

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, attempts INTEGER DEFAULT 0, received_free_bonus BOOLEAN DEFAULT 0)")
        await db.execute("CREATE TABLE IF NOT EXISTS packages (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, attempts INTEGER, price_usd REAL)")
        await db.execute("CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, user_id INTEGER)")
        await db.commit()

# --- WEBHOOK СЕРВЕР ДЛЯ KIE AI ---
async def handle_kie_callback(request):
    try:
        data = await request.json()
        logging.info(f"Callback data: {data}")
        
        task_id = data.get("taskId") or data.get("data", {}).get("taskId")
        # В Kie AI результат может быть в data['url'] или data['data']['url']
        video_url = data.get("url") or data.get("data", {}).get("url")
        state = data.get("state") or data.get("status")

        if state == "succeeded" and video_url:
            async with aiosqlite.connect(DB_NAME) as db:
                cursor = await db.execute("SELECT user_id FROM tasks WHERE task_id = ?", (task_id,))
                row = await cursor.fetchone()
                if row:
                    uid = row[0]
                    await bot.send_video(uid, video_url, caption="✅ Видео готово! Попытка списана.")
                    await db.execute("UPDATE users SET attempts = attempts - 1 WHERE user_id = ?", (uid,))
                    await db.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
                    await db.commit()
        return web.Response(text="ok")
    except Exception as e:
        logging.error(f"Callback error: {e}")
        return web.Response(text="error", status=500)

# --- ЛОГИКА API ---
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
                async with aiosqlite.connect(DB_NAME) as db:
                    await db.execute("INSERT INTO tasks (task_id, user_id) VALUES (?, ?)", (tid, user_id))
                    await db.commit()
                return True
            return False

# --- ХЕНДЛЕРЫ БОТА ---
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (m.from_user.id,))
        await db.commit()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Бонус", callback_data="bonus")],
        [InlineKeyboardButton(text="💳 Пополнить", callback_data="shop")]
    ])
    await m.answer(f"Присылай ссылку на видео (TikTok/Reels/Shorts).\nПодпишись на {CHANNEL_URL}", reply_markup=kb)

@dp.callback_query(F.data == "bonus")
async def get_bonus(c: types.CallbackQuery):
    try:
        user_channel_status = await bot.get_chat_member(CHANNEL_ID, c.from_user.id)
        if user_channel_status.status in ["member", "administrator", "creator"]:
            async with aiosqlite.connect(DB_NAME) as db:
                cur = await db.execute("SELECT received_free_bonus FROM users WHERE user_id = ?", (c.from_user.id,))
                row = await cur.fetchone()
                if row and row[0]:
                    return await c.answer("Уже получали!", show_alert=True)
                await db.execute("UPDATE users SET attempts = attempts + 1, received_free_bonus = 1 WHERE user_id = ?", (c.from_user.id,))
                await db.commit()
                await c.message.answer("✅ +1 попытка зачислена!")
        else:
            await c.answer("Подпишись на канал!", show_alert=True)
    except:
        await c.answer("Бот должен быть админом в канале!", show_alert=True)

# --- АДМИНКА (ВЫДАЧА) ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_menu(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Создать пакет", callback_data="adm_pkg")],
        [InlineKeyboardButton(text="➕ Выдать попытки", callback_data="adm_give")]
    ])
    await m.answer("Админ-меню:", reply_markup=kb)

@dp.callback_query(F.data == "adm_give", F.from_user.id == ADMIN_ID)
async def adm_1(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(States.give_user_id); await c.message.answer("Введите Telegram ID:")

@dp.message(States.give_user_id)
async def adm_2(m: types.Message, state: FSMContext):
    await state.update_data(uid=m.text); await state.set_state(States.give_amount); await m.answer("Сколько попыток?")

@dp.message(States.give_amount)
async def adm_3(m: types.Message, state: FSMContext):
    d = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET attempts = attempts + ? WHERE user_id = ?", (int(m.text), int(d['uid'])))
        await db.commit()
    await m.answer("Готово!"); await state.clear()

# --- МАГАЗИН И ОПЛАТА ---
@dp.callback_query(F.data == "shop")
async def shop(c: types.CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name, price_usd FROM packages") as cur:
            pkgs = await cur.fetchall()
    kb = [[InlineKeyboardButton(text=f"{p[1]} - ${p[2]}", callback_data=f"buy_{p[0]}")] for p in pkgs]
    await c.message.answer("Тарифы:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("buy_"))
async def buy(c: types.CallbackQuery, crypto: AioCryptoPay):
    pid = c.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT price_usd, attempts FROM packages WHERE id = ?", (pid,)) as cur:
            p = await cur.fetchone()
    inv = await crypto.create_invoice(asset='USDT', amount=p[0])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Оплатить", url=inv.bot_invoice_url)],
        [InlineKeyboardButton(text="Проверить", callback_data=f"check_{inv.invoice_id}_{p[1]}")]
    ])
    await c.message.answer(f"Оплата {p[0]} USDT", reply_markup=kb)

@dp.callback_query(F.data.startswith("check_"))
async def check_p(c: types.CallbackQuery, crypto: AioCryptoPay):
    _, iid, att = c.data.split("_")
    res = await crypto.get_invoices(invoice_ids=int(iid))
    if res and res[0].status == 'paid':
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("UPDATE users SET attempts = attempts + ? WHERE user_id = ?", (int(att), c.from_user.id))
            await db.commit()
        await c.message.answer("✅ Успешно!")
    else:
        await c.answer("Не оплачено", show_alert=True)

# --- ОБРАБОТКА ССЫЛОК ---
@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT attempts FROM users WHERE user_id = ?", (m.from_user.id,)) as cur:
            u = await cur.fetchone()
    if not u or u[0] <= 0: return await m.answer("Нет попыток!")
    
    wait_msg = await m.answer("⏳ Отправил в Kie AI. Ожидайте...")
    if await create_kie_task(m.text, m.from_user.id):
        pass # Ждем вебхук
    else:
        await wait_msg.edit_text("❌ Ошибка API.")

# --- ЗАПУСК ---
async def main():
    await init_db()
    crypto = AioCryptoPay(token=CRYPTO_TOKEN, network=Networks.MAIN_NET)
    
    app = web.Application()
    app.router.add_post('/kie-callback', handle_kie_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', PORT).start()
    
    print(f"Server started on port {PORT}")
    await dp.start_polling(bot, crypto=crypto)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())