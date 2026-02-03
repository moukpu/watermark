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

# --- ПЕРЕМЕННЫЕ ---
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
    add_token_val = State() # Сам ключ
    add_token_name = State() # Название ключа

# --- БД ---
async def init_db():
    conn = await asyncpg.connect(DATABASE_URL)
    # Обновляем структуру таблиц
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
            name TEXT,
            usage_count INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE
        );
    """)
    # ФИКС: Если колонка token_used не успела создаться в tasks
    try:
        await conn.execute("ALTER TABLE tasks ADD COLUMN token_used TEXT")
    except: pass
    await conn.close()
    logging.info("✅ База данных синхронизирована")

# --- КЛАВИАТУРА ---
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎁 Получить бонус"), KeyboardButton(text="💳 Купить попытки")],
        [KeyboardButton(text="👤 Профиль")]
    ], resize_keyboard=True)

# --- ЛОГИКА ТОКЕНОВ ---
async def get_active_token():
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT token FROM tokens WHERE is_active = TRUE ORDER BY usage_count ASC LIMIT 1")
    await conn.close()
    return row['token'] if row else None

# --- CALLBACK ОБРАБОТЧИК ---
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
    except: return web.Response(text="error")

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", m.from_user.id)
    await conn.close()
    await m.answer("Бот готов к работе!", reply_markup=main_kb())

@dp.message(F.text == "👤 Профиль")
async def profile(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    await m.answer(f"👤 Профиль\n🆔 ID: `{m.from_user.id}`\n⚡ Попытки: {u['attempts'] if u else 0}", parse_mode="Markdown")

@dp.message(F.text == "🎁 Получить бонус")
async def bonus_info(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="✅ Проверить", callback_data="check_bonus")]
    ])
    await m.answer("Подпишись на канал, чтобы получить бонусную попытку!", reply_markup=kb)

@dp.callback_query(F.data == "check_bonus")
async def check_bonus(c: types.CallbackQuery):
    status = await bot.get_chat_member(CHANNEL_ID, c.from_user.id)
    if status.status in ["member", "administrator", "creator"]:
        conn = await asyncpg.connect(DATABASE_URL)
        row = await conn.fetchrow("SELECT received_free_bonus FROM users WHERE user_id = $1", c.from_user.id)
        if row and row['received_free_bonus']:
            await c.answer("Уже брали!", show_alert=True)
        else:
            await conn.execute("UPDATE users SET attempts = attempts + 1, received_free_bonus = TRUE WHERE user_id = $1", c.from_user.id)
            await c.message.answer("✅ Бонус зачислен!")
        await conn.close()
    else:
        await c.answer("Вы не подписаны!", show_alert=True)

@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts FROM users WHERE user_id = $1", m.from_user.id)
    await conn.close()
    if not u or u['attempts'] <= 0: return await m.answer("Нет попыток.")
    
    token = await get_active_token()
    if not token: return await m.answer("Нет активных токенов.")

    msg = await m.answer("⏳ Обработка...")
    api_url = "https://api.kie.ai/api/v1/jobs/createTask"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"model": "sora-watermark-remover", "input": {"video_url": m.text}, "callBackUrl": WEBHOOK_URL}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload, headers=headers) as resp:
            res = await resp.json()
            if resp.status == 200 and res.get("code") == 200:
                conn = await asyncpg.connect(DATABASE_URL)
                await conn.execute("INSERT INTO tasks (task_id, user_id, token_used) VALUES ($1, $2, $3)", res["data"]["taskId"], m.from_user.id, token)
                await conn.close()
            else: await msg.edit_text("Ошибка сервиса.")

# --- АДМИНКА ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_main(m: types.Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Управление токенами", callback_data="adm_tok_list")],
        [InlineKeyboardButton(text="➕ Добавить токен", callback_data="adm_tok_add")],
        [InlineKeyboardButton(text="👤 Выдать попытки", callback_data="adm_g")]
    ])
    await m.answer("🛠 Админ-панель", reply_markup=kb)

@dp.callback_query(F.data == "adm_tok_list")
async def adm_tok_list(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("SELECT name, usage_count, is_active, token FROM tokens")
    await conn.close()
    if not rows: return await c.answer("Токенов нет", show_alert=True)
    
    for r in rows:
        status = "✅ Активен" if r['is_active'] else "❌ Выключен"
        btn_text = "Выключить" if r['is_active'] else "Включить"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=btn_text, callback_data=f"toggle_tok_{r['token']}")]])
        await c.message.answer(f"🏷 Название: {r['name']}\n📊 Использовано: {r['usage_count']}\nСтатус: {status}", reply_markup=kb)

@dp.callback_query(F.data.startswith("toggle_tok_"))
async def toggle_tok(c: types.CallbackQuery):
    tok_val = c.data.replace("toggle_tok_", "")
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("UPDATE tokens SET is_active = NOT is_active WHERE token = $1", tok_val)
    await conn.close()
    await c.answer("Статус изменен!")
    await adm_tok_list(c)

@dp.callback_query(F.data == "adm_tok_add")
async def tok_add_1(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(States.add_token_val)
    await c.message.answer("Введите сам API ключ:")

@dp.message(States.add_token_val)
async def tok_add_2(m: types.Message, state: FSMContext):
    await state.update_data(val=m.text)
    await state.set_state(States.add_token_name)
    await m.answer("Теперь введите название для этого токена (например, 'Основной'):")

@dp.message(States.add_token_name)
async def tok_add_3(m: types.Message, state: FSMContext):
    data = await state.get_data()
    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("INSERT INTO tokens (token, name) VALUES ($1, $2) ON CONFLICT (token) DO UPDATE SET name = $2", data['val'], m.text)
    await conn.close()
    await m.answer(f"✅ Токен '{m.text}' добавлен!")
    await state.clear()

# (Выдача попыток и магазин - стандартно)
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
    await m.answer("Выдано!"); await state.clear()

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