import asyncio
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8690440731:AAGMovCMMyp7i6B_ZWShGnge9OdiXy0Gx14")
CRYPTO_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "542708:AAuyndOoviKFZVFFIGuj0nezMmJsqVNIU93")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8540810366").split(",") if x.strip()}
DB_PATH = os.getenv("DB_PATH", "bot.db")
CRYPTO_API = "https://pay.crypt.bot/api"

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("autoreg_bot")


@dataclass
class NumberRow:
    id: int
    user_id: int
    phone: str
    acc_type: str
    status: str


class Storage:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        c = self.conn.cursor()
        c.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                balance REAL DEFAULT 0,
                banned INTEGER DEFAULT 0,
                created_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                acc_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                queue_entered_at INTEGER,
                taken_by INTEGER,
                code_type TEXT,
                code_value TEXT,
                code_reported INTEGER DEFAULT 0,
                no_code_reported INTEGER DEFAULT 0,
                reward REAL DEFAULT 0,
                created_at INTEGER,
                updated_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                status TEXT,
                check_url TEXT,
                created_at INTEGER,
                updated_at INTEGER
            );
            """
        )
        self.conn.commit()
        if self.get_setting("price_reg") is None:
            self.set_setting("price_reg", "1.5")
        if self.get_setting("price_noreg") is None:
            self.set_setting("price_noreg", "1.0")

    def ensure_user(self, user_id: int, username: str, full_name: str):
        self.conn.execute(
            """
            INSERT INTO users(user_id, username, full_name, created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name
            """,
            (user_id, username, full_name, int(time.time())),
        )
        self.conn.commit()

    def is_banned(self, user_id: int) -> bool:
        r = self.conn.execute("SELECT banned FROM users WHERE user_id=?", (user_id,)).fetchone()
        return bool(r and r["banned"])

    def set_ban(self, user_id: int, value: bool):
        self.conn.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if value else 0, user_id))
        self.conn.commit()

    def get_setting(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_setting(self, key: str, value: str):
        self.conn.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def add_number(self, user_id: int, phone: str, acc_type: str):
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO numbers(user_id,phone,acc_type,status,queue_entered_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (user_id, phone, acc_type, "pending", now, now, now),
        )
        self.conn.commit()

    def pending_position(self, number_id: int) -> Optional[int]:
        rows = self.conn.execute(
            "SELECT id FROM numbers WHERE status='pending' ORDER BY queue_entered_at,id"
        ).fetchall()
        for idx, row in enumerate(rows, 1):
            if row["id"] == number_id:
                return idx
        return None

    def user_pending(self, user_id: int):
        return self.conn.execute(
            "SELECT * FROM numbers WHERE user_id=? AND status='pending' ORDER BY queue_entered_at,id",
            (user_id,),
        ).fetchall()

    def user_archive(self, user_id: int, limit: int = 10, offset: int = 0):
        return self.conn.execute(
            """
            SELECT * FROM numbers
            WHERE user_id=? AND status IN ('success','fail','rejected')
            ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        ).fetchall()

    def user_archive_count(self, user_id: int) -> int:
        r = self.conn.execute(
            "SELECT COUNT(*) AS c FROM numbers WHERE user_id=? AND status IN ('success','fail','rejected')",
            (user_id,),
        ).fetchone()
        return int(r["c"])

    def get_balance(self, user_id: int) -> float:
        r = self.conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return float(r["balance"] if r else 0)

    def add_balance(self, user_id: int, amount: float):
        self.conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        self.conn.commit()

    def sub_balance(self, user_id: int, amount: float):
        self.conn.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user_id))
        self.conn.commit()

    def get_pending_for_admin(self, limit: int = 5, offset: int = 0):
        return self.conn.execute(
            """
            SELECT n.*, u.username FROM numbers n
            LEFT JOIN users u ON u.user_id=n.user_id
            WHERE n.status='pending'
            ORDER BY n.queue_entered_at,n.id
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()

    def pending_count(self) -> int:
        r = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status='pending'").fetchone()
        return int(r["c"])

    def get_number(self, number_id: int):
        return self.conn.execute("SELECT * FROM numbers WHERE id=?", (number_id,)).fetchone()

    def set_number_status(self, number_id: int, status: str, admin_id: Optional[int] = None):
        now = int(time.time())
        if admin_id:
            self.conn.execute(
                "UPDATE numbers SET status=?, taken_by=?, updated_at=? WHERE id=?",
                (status, admin_id, now, number_id),
            )
        else:
            self.conn.execute("UPDATE numbers SET status=?, updated_at=? WHERE id=?", (status, now, number_id))
        self.conn.commit()

    def set_code_request(self, number_id: int, code_type: str):
        self.conn.execute(
            "UPDATE numbers SET code_type=?, status='waiting_code', updated_at=? WHERE id=?",
            (code_type, int(time.time()), number_id),
        )
        self.conn.commit()

    def save_code(self, number_id: int, code: str):
        self.conn.execute(
            "UPDATE numbers SET code_value=?, code_reported=1, status='awaiting_admin', updated_at=? WHERE id=?",
            (code, int(time.time()), number_id),
        )
        self.conn.commit()

    def report_no_code(self, number_id: int):
        self.conn.execute(
            "UPDATE numbers SET no_code_reported=1, status='awaiting_admin', updated_at=? WHERE id=?",
            (int(time.time()), number_id),
        )
        self.conn.commit()

    def finalize_number(self, number_id: int, success: bool, reward: float):
        status = "success" if success else "fail"
        self.conn.execute(
            "UPDATE numbers SET status=?, reward=?, updated_at=? WHERE id=?",
            (status, reward if success else 0, int(time.time()), number_id),
        )
        self.conn.commit()

    def reject_number(self, number_id: int):
        self.conn.execute(
            "UPDATE numbers SET status='rejected', updated_at=? WHERE id=?", (int(time.time()), number_id)
        )
        self.conn.commit()

    def user_stats_day(self, user_id: int):
        since = int((datetime.utcnow() - timedelta(days=1)).timestamp())
        ok = self.conn.execute(
            "SELECT COUNT(*) c FROM numbers WHERE user_id=? AND status='success' AND updated_at>=?",
            (user_id, since),
        ).fetchone()["c"]
        bad = self.conn.execute(
            "SELECT COUNT(*) c FROM numbers WHERE user_id=? AND status IN ('fail','rejected') AND updated_at>=?",
            (user_id, since),
        ).fetchone()["c"]
        return int(ok), int(bad)

    def all_users(self):
        return self.conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()

    def user_numbers(self, user_id: int):
        return self.conn.execute("SELECT * FROM numbers WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()

    def create_payout(self, user_id: int, amount: float):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO payouts(user_id,amount,status,created_at,updated_at) VALUES(?,?,?,?,?)",
            (user_id, amount, "processing", now, now),
        )
        self.conn.commit()

    def finish_payout(self, user_id: int, status: str, url: str = ""):
        self.conn.execute(
            """
            UPDATE payouts SET status=?, check_url=?, updated_at=?
            WHERE user_id=? AND status='processing'
            """,
            (status, url, int(time.time()), user_id),
        )
        self.conn.commit()


db = Storage(DB_PATH)
withdraw_lock = asyncio.Lock()


def user_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📲 Сдать номер", "📋 Мои номера"],
            ["💰 Баланс", "📊 Статистика"],
        ],
        resize_keyboard=True,
    )


def admin_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["🛠 Номера", "👥 Пользователи"], ["💵 Цена", "📈 Админ-статистика"]],
        resize_keyboard=True,
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def crypto_request(method: str, payload: dict) -> dict:
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(f"{CRYPTO_API}/{method}", json=payload, timeout=10) as resp:
            data = await resp.json()
            return data


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.full_name)
    if db.is_banned(user.id):
        await update.message.reply_text("⛔ Вы заблокированы администратором.")
        return

    text = (
        "✨ <b>Добро пожаловать в сервис сдачи номеров</b>\n\n"
        "• Добавляйте номера в очередь\n"
        "• Отслеживайте статус в реальном времени\n"
        "• Получайте выплаты автоматически"
    )
    kb = admin_keyboard() if is_admin(user.id) else user_keyboard()
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.full_name)
    if db.is_banned(user.id):
        await update.message.reply_text("⛔ Вы заблокированы.")
        return

    text = update.message.text
    if text == "📲 Сдать номер":
        await show_submit_menu(update)
    elif text == "📋 Мои номера":
        await show_my_numbers(update)
    elif text == "💰 Баланс":
        await show_balance(update)
    elif text == "📊 Статистика":
        await show_stats(update)
    elif text == "🛠 Номера" and is_admin(user.id):
        await show_admin_numbers(update, 0)
    elif text == "👥 Пользователи" and is_admin(user.id):
        await show_admin_users(update, 0)
    elif text == "💵 Цена" and is_admin(user.id):
        await show_price_menu(update)
    elif text == "📈 Админ-статистика" and is_admin(user.id):
        await show_admin_stats(update)


async def show_submit_menu(update: Update):
    reg = float(db.get_setting("price_reg"))
    noreg = float(db.get_setting("price_noreg"))
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"✅ Рег (${reg:.2f})", callback_data="submit:reg")],
            [InlineKeyboardButton(f"🆕 Не рег (${noreg:.2f})", callback_data="submit:noreg")],
        ]
    )
    await update.message.reply_text("Выберите тип аккаунта:", reply_markup=kb)


async def cb_submit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    acc_type = q.data.split(":")[1]
    context.user_data["waiting_phone_type"] = acc_type
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await q.message.reply_text(
        "Отправьте номер телефона (должен начинаться с +7).",
        reply_markup=kb,
    )


async def contact_or_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if "waiting_phone_type" not in context.user_data:
        return
    acc_type = context.user_data["waiting_phone_type"]

    phone = ""
    if update.message.contact:
        phone = "+" + update.message.contact.phone_number.lstrip("+")
    elif update.message.text:
        phone = update.message.text.strip()

    if not phone.startswith("+7") or not phone[1:].isdigit() or len(phone) < 12:
        await update.message.reply_text("❌ Номер должен быть в формате +7XXXXXXXXXX")
        return

    db.add_number(user.id, phone, acc_type)
    num = db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    pos = db.pending_position(num)
    del context.user_data["waiting_phone_type"]

    await update.message.reply_text(
        f"✅ Номер <b>{phone}</b> добавлен в очередь.\nТекущая позиция: <b>#{pos}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=user_keyboard() if not is_admin(user.id) else admin_keyboard(),
    )

    for admin_id in ADMIN_IDS:
        await context.bot.send_message(
            admin_id,
            f"📥 Новый номер в очереди: {phone} ({'рег' if acc_type=='reg' else 'не рег'})\n"
            f"Пользователь: @{user.username or user.id}",
        )


async def show_my_numbers(update: Update):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("⏳ Ожидающие", callback_data="my:pending")],
            [InlineKeyboardButton("🗂 Архивные", callback_data="my:archive:0")],
        ]
    )
    await update.message.reply_text("Раздел «Мои номера»", reply_markup=kb)


async def cb_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    uid = q.from_user.id

    if parts[1] == "pending":
        rows = db.user_pending(uid)
        if not rows:
            await q.message.edit_text("⏳ У вас нет ожидающих номеров.")
            return
        lines = ["<b>Ваши ожидающие номера:</b>"]
        buttons = []
        for r in rows:
            pos = db.pending_position(r["id"])
            lines.append(f"• {r['phone']} — очередь #{pos}")
            buttons.append([InlineKeyboardButton(f"Удалить {r['phone']}", callback_data=f"mydel:{r['id']}")])
        await q.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    if parts[1] == "archive":
        page = int(parts[2])
        per_page = 10
        total = db.user_archive_count(uid)
        rows = db.user_archive(uid, per_page, page * per_page)
        if not rows:
            await q.message.edit_text("🗂 Архив пуст.")
            return
        lines = [f"<b>Архив (стр. {page+1})</b>"]
        for i, r in enumerate(rows, 1 + page * per_page):
            icon = "🟢✅" if r["status"] == "success" else "🔴❌"
            lines.append(f"{i}. {r['phone']} {icon}")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"my:archive:{page-1}"))
        if (page + 1) * per_page < total:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"my:archive:{page+1}"))
        markup = InlineKeyboardMarkup([nav]) if nav else None
        await q.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=markup)


async def cb_my_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    num_id = int(q.data.split(":")[1])
    row = db.get_number(num_id)
    if not row or row["user_id"] != q.from_user.id or row["status"] != "pending":
        await q.answer("Нельзя удалить", show_alert=True)
        return
    db.reject_number(num_id)
    await q.message.reply_text("🗑 Номер удален из очереди.")


async def show_balance(update: Update):
    uid = update.effective_user.id
    bal = db.get_balance(uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("💸 Вывод", callback_data="balance:withdraw")]])
    await update.message.reply_text(f"Ваш баланс: <b>${bal:.2f}</b>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    amount = db.get_balance(uid)
    if amount <= 0:
        await q.message.reply_text("Недостаточно средств для вывода.")
        return

    if withdraw_lock.locked():
        await q.message.reply_text("Сейчас уже есть активная заявка на вывод. Попробуйте позже.")
        return

    async with withdraw_lock:
        db.create_payout(uid, amount)
        try:
            balance_data = await crypto_request("getBalance", {})
            if not balance_data.get("ok"):
                raise RuntimeError("getBalance failed")
            usdt = 0.0
            for item in balance_data["result"]:
                if item.get("currency_code") == "USDT":
                    usdt = float(item.get("available", 0))
                    break
            if usdt < amount:
                db.finish_payout(uid, "rejected")
                await q.message.reply_text(
                    "На балансе сервиса не достаточно денег для вывода баланса, администратор скоро пополнит баланс."
                )
                return

            payload = {
                "asset": "USDT",
                "amount": f"{amount:.2f}",
                "pin_to_user_id": uid,
                "spend_id": str(uuid.uuid4()),
            }
            start_t = time.time()
            result = await crypto_request("createCheck", payload)
            if time.time() - start_t > 10:
                db.finish_payout(uid, "timeout")
                await q.message.reply_text("Заявка отменена: превышено время ожидания (10 сек).")
                return

            if not result.get("ok"):
                db.finish_payout(uid, "failed")
                await q.message.reply_text("Ошибка создания выплаты. Попробуйте позже.")
                return

            check_url = result["result"]["bot_check_url"]
            db.sub_balance(uid, amount)
            db.finish_payout(uid, "done", check_url)
            await q.message.reply_text(
                f"✅ Выплата успешно создана на сумму ${amount:.2f}\n{check_url}",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.exception("withdraw error: %s", e)
            db.finish_payout(uid, "failed")
            await q.message.reply_text("Не удалось выполнить вывод. Попробуйте позже.")


async def show_stats(update: Update):
    uid = update.effective_user.id
    ok, bad = db.user_stats_day(uid)
    total = ok + bad
    percent = (ok / total * 100) if total else 0
    await update.message.reply_text(
        f"📊 Статистика за 24ч\nУспешно: {ok}\nНе успешно: {bad}\nПроцент успеха: {percent:.1f}%"
    )


async def show_admin_numbers(update: Update, page: int):
    total = db.pending_count()
    rows = db.get_pending_for_admin(5, page * 5)
    if not rows:
        await update.message.reply_text("Очередь пуста.")
        return
    lines = [f"🛠 Очередь номеров: {total}"]
    kb = []
    for idx, r in enumerate(rows, 1 + page * 5):
        lines.append(f"{idx}. {r['phone']} ({'рег' if r['acc_type']=='reg' else 'не рег'}) @{r['username'] or r['user_id']}")
        kb.append([InlineKeyboardButton(f"Открыть #{r['id']}", callback_data=f"admnum:{r['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admqueue:{page-1}"))
    if (page + 1) * 5 < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admqueue:{page+1}"))
    if nav:
        kb.append(nav)
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def cb_admin_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    page = int(q.data.split(":")[1])
    total = db.pending_count()
    rows = db.get_pending_for_admin(5, page * 5)
    if not rows:
        await q.message.edit_text("Очередь пуста.")
        return
    lines = [f"🛠 Очередь номеров: {total}"]
    kb = []
    for idx, r in enumerate(rows, 1 + page * 5):
        lines.append(f"{idx}. {r['phone']} ({'рег' if r['acc_type']=='reg' else 'не рег'}) @{r['username'] or r['user_id']}")
        kb.append([InlineKeyboardButton(f"Открыть #{r['id']}", callback_data=f"admnum:{r['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admqueue:{page-1}"))
    if (page + 1) * 5 < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admqueue:{page+1}"))
    if nav:
        kb.append(nav)
    await q.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def cb_admin_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r:
        await q.message.reply_text("Номер не найден")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Отклонить", callback_data=f"admreject:{num_id}"), InlineKeyboardButton("✅ Взять", callback_data=f"admtake:{num_id}")]]
    )
    await q.message.reply_text(
        f"Номер: {r['phone']}\nТип: {'рег' if r['acc_type']=='reg' else 'не рег'}\nСтатус: {r['status']}",
        reply_markup=kb,
    )


async def cb_admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r:
        return
    db.reject_number(num_id)
    await context.bot.send_message(r["user_id"], f"❌ Ваш номер {r['phone']} был отклонен администратором.")
    await q.message.reply_text("Номер отклонен.")


async def cb_admin_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["status"] != "pending":
        await q.message.reply_text("Номер уже в работе/закрыт.")
        return
    db.set_number_status(num_id, "in_work", q.from_user.id)
    await context.bot.send_message(r["user_id"], f"🔔 Ваш номер {r['phone']} взяли в работу.")
    code_btn_text = "Запросить код с аккаунта" if r["acc_type"] == "reg" else "Запросить код с звонка"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(code_btn_text, callback_data=f"askcode:{num_id}")]])
    await q.message.reply_text("Номер взят. Следующий шаг:", reply_markup=kb)


async def cb_admin_ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r:
        return
    code_type = "код с аккаунта" if r["acc_type"] == "reg" else "код с звонка"
    db.set_code_request(num_id, code_type)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("❗ Не пришел код", callback_data=f"nocode:{num_id}")]])
    await context.bot.send_message(
        r["user_id"],
        f"📩 Администратор запросил {code_type} для номера {r['phone']}.\n"
        "Ответьте на это сообщение кодом.",
        reply_markup=kb,
    )
    context.user_data[f"admin_wait_code_{num_id}"] = True
    await q.message.reply_text("Запрос кода отправлен пользователю.")


async def on_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text or len(text) > 64:
        return
    uid = update.effective_user.id
    rows = db.conn.execute(
        "SELECT * FROM numbers WHERE user_id=? AND status='waiting_code' ORDER BY updated_at DESC LIMIT 1", (uid,)
    ).fetchall()
    if not rows:
        return
    r = rows[0]
    db.save_code(r["id"], text)
    await update.message.reply_text("✅ Код принят. Ожидайте подтверждения от администратора.")
    for admin_id in ADMIN_IDS:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Встал", callback_data=f"finalok:{r['id']}"), InlineKeyboardButton("❌ Не встал", callback_data=f"finalbad:{r['id']}")]]
        )
        await context.bot.send_message(
            admin_id,
            f"Поступил код по номеру {r['phone']}: <code>{text}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )


async def cb_no_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["user_id"] != q.from_user.id:
        return
    db.report_no_code(num_id)
    await q.message.reply_text("Информация отправлена администратору.")
    for admin_id in ADMIN_IDS:
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ Встал", callback_data=f"finalok:{num_id}"), InlineKeyboardButton("❌ Не встал", callback_data=f"finalbad:{num_id}")]]
        )
        await context.bot.send_message(admin_id, f"Пользователь сообщил: код не пришел по номеру {r['phone']}.", reply_markup=kb)


async def cb_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    is_ok = q.data.startswith("finalok")
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r:
        return
    price = float(db.get_setting("price_reg" if r["acc_type"] == "reg" else "price_noreg"))
    db.finalize_number(num_id, is_ok, price)
    if is_ok:
        db.add_balance(r["user_id"], price)
        await context.bot.send_message(r["user_id"], f"✅ Номер {r['phone']} успешно подтвержден. +${price:.2f} зачислено на баланс.")
        await q.message.reply_text("Успешно закрыто. Баланс пользователю начислен.")
    else:
        await context.bot.send_message(r["user_id"], f"❌ Номер {r['phone']} не был зарегистрирован. Вознаграждение не начислено.")
        await q.message.reply_text("Отмечено как неуспешно.")


async def show_admin_users(update: Update, page: int):
    users = db.all_users()
    if not users:
        await update.message.reply_text("Пользователей нет.")
        return
    per = 8
    part = users[page * per : page * per + per]
    lines = [f"👥 Пользователи (стр. {page+1})"]
    kb = []
    for u in part:
        nums = db.user_numbers(u["user_id"])
        ok = len([x for x in nums if x["status"] == "success"])
        bad = len([x for x in nums if x["status"] in ("fail", "rejected")])
        total = ok + bad
        percent = (ok / total * 100) if total else 0
        lines.append(f"@{u['username'] or u['user_id']} | баланс ${u['balance']:.2f} | успех {percent:.1f}%")
        kb.append([InlineKeyboardButton(f"Профиль {u['user_id']}", callback_data=f"admuser:{u['user_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admusers:{page-1}"))
    if (page + 1) * per < len(users):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admusers:{page+1}"))
    if nav:
        kb.append(nav)
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def cb_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    page = int(q.data.split(":")[1])
    users = db.all_users()
    per = 8
    part = users[page * per : page * per + per]
    if not part:
        await q.message.edit_text("Пусто")
        return
    lines = [f"👥 Пользователи (стр. {page+1})"]
    kb = []
    for u in part:
        nums = db.user_numbers(u["user_id"])
        ok = len([x for x in nums if x["status"] == "success"])
        bad = len([x for x in nums if x["status"] in ("fail", "rejected")])
        total = ok + bad
        percent = (ok / total * 100) if total else 0
        lines.append(f"@{u['username'] or u['user_id']} | баланс ${u['balance']:.2f} | успех {percent:.1f}%")
        kb.append([InlineKeyboardButton(f"Профиль {u['user_id']}", callback_data=f"admuser:{u['user_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admusers:{page-1}"))
    if (page + 1) * per < len(users):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admusers:{page+1}"))
    if nav:
        kb.append(nav)
    await q.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def cb_admin_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    uid = int(q.data.split(":")[1])
    u = db.conn.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    if not u:
        return
    nums = db.user_numbers(uid)
    num_list = "\n".join([f"• {x['phone']} [{x['status']}]" for x in nums[:15]]) or "—"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚫 Бан", callback_data=f"ban:{uid}"), InlineKeyboardButton("♻️ Разбан", callback_data=f"unban:{uid}")],
        ]
    )
    await q.message.reply_text(
        f"Пользователь: @{u['username'] or uid}\nБаланс: ${u['balance']:.2f}\nСданные номера:\n{num_list}",
        reply_markup=kb,
    )


async def cb_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    action, uid = q.data.split(":")
    uid_i = int(uid)
    db.set_ban(uid_i, action == "ban")
    await q.message.reply_text("Готово.")


async def show_price_menu(update: Update):
    reg = db.get_setting("price_reg")
    noreg = db.get_setting("price_noreg")
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Изменить REG", callback_data="price:reg")],
            [InlineKeyboardButton("Изменить NOREG", callback_data="price:noreg")],
        ]
    )
    await update.message.reply_text(f"Текущие цены:\nREG: ${reg}\nNOREG: ${noreg}", reply_markup=kb)


async def cb_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    mode = q.data.split(":")[1]
    context.user_data["price_edit"] = mode
    await q.message.reply_text(f"Введите новую цену для {mode.upper()} (пример: 1.75)")


async def admin_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if "price_edit" not in context.user_data:
        return
    try:
        val = float(update.message.text.replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число.")
        return
    if val <= 0:
        await update.message.reply_text("Цена должна быть больше 0")
        return
    key = "price_reg" if context.user_data["price_edit"] == "reg" else "price_noreg"
    db.set_setting(key, f"{val:.2f}")
    del context.user_data["price_edit"]
    await update.message.reply_text("Цена обновлена ✅")


async def show_admin_stats(update: Update):
    total = db.conn.execute("SELECT COUNT(*) c FROM numbers").fetchone()["c"]
    pending = db.pending_count()
    success = db.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status='success'").fetchone()["c"]
    failed = db.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status IN ('fail','rejected')").fetchone()["c"]
    await update.message.reply_text(
        f"📈 Общая статистика\nВсего номеров: {total}\nВ очереди: {pending}\nУспешно: {success}\nНеуспешно: {failed}"
    )


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb_submit_type, pattern=r"^submit:"))
    app.add_handler(CallbackQueryHandler(cb_my, pattern=r"^my:"))
    app.add_handler(CallbackQueryHandler(cb_my_delete, pattern=r"^mydel:"))
    app.add_handler(CallbackQueryHandler(cb_withdraw, pattern=r"^balance:withdraw$"))
    app.add_handler(CallbackQueryHandler(cb_admin_queue, pattern=r"^admqueue:"))
    app.add_handler(CallbackQueryHandler(cb_admin_number, pattern=r"^admnum:"))
    app.add_handler(CallbackQueryHandler(cb_admin_reject, pattern=r"^admreject:"))
    app.add_handler(CallbackQueryHandler(cb_admin_take, pattern=r"^admtake:"))
    app.add_handler(CallbackQueryHandler(cb_admin_ask_code, pattern=r"^askcode:"))
    app.add_handler(CallbackQueryHandler(cb_no_code, pattern=r"^nocode:"))
    app.add_handler(CallbackQueryHandler(cb_finalize, pattern=r"^final(ok|bad):"))
    app.add_handler(CallbackQueryHandler(cb_admin_users, pattern=r"^admusers:"))
    app.add_handler(CallbackQueryHandler(cb_admin_user_profile, pattern=r"^admuser:"))
    app.add_handler(CallbackQueryHandler(cb_ban, pattern=r"^(ban|unban):"))
    app.add_handler(CallbackQueryHandler(cb_price, pattern=r"^price:"))

    app.add_handler(MessageHandler(filters.CONTACT | (filters.TEXT & filters.Regex(r"^\+7\d{10,}$")), contact_or_phone), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_price_input), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_user_text), group=2)
    menu_filter = filters.TEXT & filters.Regex(r"^(📲 Сдать номер|📋 Мои номера|💰 Баланс|📊 Статистика|🛠 Номера|👥 Пользователи|💵 Цена|📈 Админ-статистика)$")
    app.add_handler(MessageHandler(menu_filter, menu_router), group=3)
    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Bot started")
    application.run_polling()
