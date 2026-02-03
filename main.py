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

class States(StatesGroup):
    add_token_val = State()
    add_token_name = State()
    give_amount = State()

# --- БД ---
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

# --- ЛОГИКА ТОКЕНОВ ---
async def get_current_token():
    conn = await asyncpg.connect(DATABASE_URL)
    # Сначала ищем тот, где стоит галочка
    row = await conn.fetchrow("SELECT token, name FROM tokens WHERE is_active = TRUE LIMIT 1")
    if not row:
        # Если ни один не выбран вручную, берем самый свободный
        row = await conn.fetchrow("SELECT token, name FROM tokens ORDER BY usage_count ASC LIMIT 1")
    await conn.close()
    return row

async def switch_token_on_error():
    conn = await asyncpg.connect(DATABASE_URL)
    auto = await conn.fetchval("SELECT is_auto_switch FROM tokens LIMIT 1")
    if auto:
        current = await conn.fetchval("SELECT token FROM tokens WHERE is_active = TRUE")
        await conn.execute("UPDATE tokens SET is_active = FALSE")
        # Выбираем следующий токен с минимальным износом
        new = await conn.fetchrow("SELECT token FROM tokens WHERE token != $1 ORDER BY usage_count ASC LIMIT 1", current)
        if new:
            await conn.execute("UPDATE tokens SET is_active = TRUE WHERE token = $1", new['token'])
    await conn.close()

# --- АДМИНКА: ТОКЕНЫ ---
@dp.callback_query(F.data == "adm_tok_list")
async def adm_tok_list(c: types.CallbackQuery):
    conn = await asyncpg.connect(DATABASE_URL)
    tokens = await conn.fetch("SELECT token, name, is_active, usage_count, is_auto_switch FROM tokens")
    await conn.close()
    
    if not tokens: return await c.answer("Токенов нет")
    
    auto_mode = "✅ ВКЛ" if tokens[0]['is_auto_switch'] else "❌ ВЫКЛ"
    text = f"⚙️ **Управление токенами**\nАвтосмена при ошибке: {auto_mode}\n\n"
    buttons = []
    
    for i, t in enumerate(tokens, 1):
        mark = "✅" if t['is_active'] else ""
        text += f"{i}. {t['name']} | {t['usage_count']} скачек {mark}\n"
        buttons.append(InlineKeyboardButton(text=f"{mark if mark else i}", callback_data=f"set_active_{t['token']}"))
    
    # Группируем кнопки по 5 в ряд
    kb_rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
    kb_rows.append([InlineKeyboardButton(text=f"Автосмена: {auto_mode}", callback_data="toggle_auto")])
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    
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

# --- АДМИНКА: ЮЗЕРЫ (ПАГИНАЦИЯ) ---
@dp.callback_query(F.data.startswith("adm_users_"))
async def adm_users(c: types.CallbackQuery):
    page = int(c.data.split("_")[2])
    offset = page * 5
    conn = await asyncpg.connect(DATABASE_URL)
    users = await conn.fetch("SELECT user_id FROM users LIMIT 5 OFFSET $1", offset)
    total = await conn.fetchval("SELECT COUNT(*) FROM users")
    await conn.close()
    
    kb = []
    for u in users:
        kb.append([InlineKeyboardButton(text=f"👤 ID: {u['user_id']}", callback_data=f"user_info_{u['user_id']}_{page}")])
    
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_users_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{(total//5)+1}", callback_data="ignore"))
    if offset + 5 < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_users_{page+1}"))
    
    kb.append(nav)
    kb.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_back")])
    await c.message.edit_text(f"👥 Всего пользователей: {total}", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("user_info_"))
async def user_info(c: types.CallbackQuery):
    _, _, uid, page = c.data.split("_")
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", int(uid))
    await conn.close()
    
    text = (f"👤 **Инфо: {uid}**\n\n"
            f"⚡ Попытки: {u['attempts']}\n"
            f"💰 Донат: ${u['total_donated']}\n"
            f"📥 Скачано: {u['total_downloaded']}\n"
            f"🚫 Бан: {'Да' if u['is_banned'] else 'Нет'}")
    
    kb = [
        [InlineKeyboardButton(text="➕ Выдать попытки", callback_data=f"u_give_{uid}")],
        [InlineKeyboardButton(text="🚫 Бан/Разбан", callback_data=f"u_ban_{uid}_{page}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"adm_users_{page}")]
    ]
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

# --- ОБРАБОТКА ВИДЕО (С АВТОСМЕНОЙ) ---
@dp.message(F.text.regexp(r'https?://'))
async def handle_url(m: types.Message):
    conn = await asyncpg.connect(DATABASE_URL)
    u = await conn.fetchrow("SELECT attempts, is_banned FROM users WHERE user_id = $1", m.from_user.id)
    if u and u['is_banned']: return await m.answer("Вы забанены.")
    if not u or u['attempts'] <= 0: return await m.answer("Нет попыток.")
    
    token_data = await get_current_token()
    if not token_data: return await m.answer("Нет активных ключей.")

    msg = await m.answer("⏳ Обработка...")
    headers = {"Authorization": f"Bearer {token_data['token']}", "Content-Type": "application/json"}
    payload = {"model": "sora-watermark-remover", "input": {"video_url": m.text}, "callBackUrl": WEBHOOK_URL}
    
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post("https://api.kie.ai/api/v1/jobs/createTask", json=payload, headers=headers) as resp:
                res = await resp.json()
                if resp.status == 200 and res.get("code") == 200:
                    await conn.execute("INSERT INTO tasks VALUES ($1, $2, $3)", res["data"]["taskId"], m.from_user.id, token_data['token'])
                else:
                    await msg.edit_text("⚠️ Техническая ошибка. Пробуем другой сервер...")
                    await switch_token_on_error()
                    # Здесь можно добавить повторную попытку или просто уведомить админа
                    await bot.send_message(ADMIN_ID, f"❌ Ошибка на токене {token_data['name']}. Автосмена сработала.")
        except:
            await msg.edit_text("Техническая ошибка.")
    await conn.close()

# --- КЛАВИАТУРА ЮЗЕРА ---
def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🎁 Получить бонус"), KeyboardButton(text="💳 Купить попытки")],
        [KeyboardButton(text="👤 Профиль")]
    ], resize_keyboard=True)

# (Остальные стандартные хендлеры и запуск остаются такими же...)
# Не забудь прописать в main() вызов init_db()
