import asyncio
import logging
import math
import os
import re
import sqlite3
import ssl
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8690440731:AAGSZvkriStW96os8exWs4SUR6f4Q5Pvf0w")
CRYPTO_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "542708:AAuyndOoviKFZVFFIGuj0nezMmJsqVNIU93")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "8540810366,8104932320,8260773398").split(",") if x.strip()}
SENIOR_ADMIN_IDS = set(ADMIN_IDS)
DB_PATH = os.getenv("DB_PATH", "bot.db")
CRYPTO_API = "https://pay.crypt.bot/api"
SUB_CHANNEL = "@VINTAGEINF"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
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
                prepaid_junior_balance REAL DEFAULT 0,
                banned INTEGER DEFAULT 0,
                subscribed INTEGER DEFAULT 0,
                referrer_id INTEGER,
                referral_earned REAL DEFAULT 0,
                vip_until INTEGER DEFAULT 0,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS numbers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                phone TEXT NOT NULL,
                acc_type TEXT NOT NULL,
                service TEXT NOT NULL DEFAULT 'telegram',
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
            CREATE TABLE IF NOT EXISTS treasury_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER,
                amount REAL,
                status TEXT,
                pay_url TEXT,
                created_by INTEGER DEFAULT 0,
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS vip_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                invoice_id INTEGER,
                amount REAL,
                status TEXT,
                pay_url TEXT,
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS blocked_numbers (
                phone TEXT PRIMARY KEY,
                blocked_by INTEGER,
                created_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS admin_roles (
                user_id INTEGER PRIMARY KEY,
                is_junior INTEGER NOT NULL DEFAULT 1,
                reg_price REAL NOT NULL DEFAULT 1.40,
                noreg_price REAL NOT NULL DEFAULT 1.40,
                max_price REAL NOT NULL DEFAULT 1.40,
                imo_price REAL NOT NULL DEFAULT 1.40,
                treasury_balance REAL NOT NULL DEFAULT 0,
                profit_total REAL NOT NULL DEFAULT 0,
                created_at INTEGER,
                updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS admin_treasury_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                invoice_id INTEGER,
                amount REAL,
                status TEXT,
                pay_url TEXT,
                created_at INTEGER,
                updated_at INTEGER
            );
            """
        )
        self.conn.commit()
        self._migrate_users_table()
        self._migrate_numbers_table()
        self._migrate_admin_roles_table()
        defaults = {
            "price_reg": "1.5",
            "price_noreg": "1.0",
            "price_max": "1.2",
            "price_imo": "1.2",
            "vip_price_reg": "2.2",
            "vip_price_noreg": "1.7",
            "vip_sub_price": "20",
            "work_enabled": "1",
            "work_tg_reg": "1",
            "work_tg_noreg": "1",
            "work_max": "1",
            "work_imo": "1",
            "treasury_balance": "0",
            "ref_percent": "10",
        }
        for k, v in defaults.items():
            if self.get_setting(k) is None:
                self.set_setting(k, v)

    def _migrate_users_table(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)").fetchall()}
        if "subscribed" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN subscribed INTEGER DEFAULT 0")
        if "referrer_id" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN referrer_id INTEGER")
        if "referral_earned" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN referral_earned REAL DEFAULT 0")
        if "vip_until" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN vip_until INTEGER DEFAULT 0")
        if "prepaid_junior_balance" not in cols:
            self.conn.execute("ALTER TABLE users ADD COLUMN prepaid_junior_balance REAL DEFAULT 0")
        self.conn.commit()



    def _migrate_numbers_table(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(numbers)").fetchall()}
        if "service" not in cols:
            self.conn.execute("ALTER TABLE numbers ADD COLUMN service TEXT NOT NULL DEFAULT 'telegram'")
            self.conn.execute("UPDATE numbers SET service='telegram' WHERE service IS NULL OR service='' ")
        tcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(treasury_invoices)").fetchall()}
        if "created_by" not in tcols:
            self.conn.execute("ALTER TABLE treasury_invoices ADD COLUMN created_by INTEGER DEFAULT 0")
        self.conn.commit()

    def _migrate_admin_roles_table(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(admin_roles)").fetchall()}
        if "max_price" not in cols:
            self.conn.execute("ALTER TABLE admin_roles ADD COLUMN max_price REAL NOT NULL DEFAULT 1.40")
        if "imo_price" not in cols:
            self.conn.execute("ALTER TABLE admin_roles ADD COLUMN imo_price REAL NOT NULL DEFAULT 1.40")
        access_cols = {
            "access_numbers": 1,
            "access_users": 0,
            "access_price": 0,
            "access_stats": 0,
            "access_work": 1,
            "access_mailing": 1,
            "access_treasury": 1,
            "access_payouts": 0,
            "access_block": 0,
            "access_admins": 0,
            "access_vip": 0,
        }
        for col, default in access_cols.items():
            if col not in cols:
                self.conn.execute(f"ALTER TABLE admin_roles ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}")
        self.conn.commit()

    def ensure_user(self, user_id: int, username: str, full_name: str) -> bool:
        existed = self.conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone() is not None
        self.conn.execute(
            """
            INSERT INTO users(user_id,username,full_name,created_at)
            VALUES(?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,full_name=excluded.full_name
            """,
            (user_id, username, full_name, int(time.time())),
        )
        self.conn.commit()
        return not existed

    def user_row(self, user_id: int):
        return self.conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    def user_by_username(self, username: str):
        u = username.lstrip("@").strip()
        return self.conn.execute("SELECT * FROM users WHERE lower(username)=lower(?)", (u,)).fetchone()

    def all_user_ids(self):
        return [r["user_id"] for r in self.conn.execute("SELECT user_id FROM users").fetchall()]

    def is_banned(self, user_id: int) -> bool:
        r = self.user_row(user_id)
        return bool(r and r["banned"])

    def set_ban(self, user_id: int, value: bool):
        self.conn.execute("UPDATE users SET banned=? WHERE user_id=?", (1 if value else 0, user_id))
        self.conn.commit()

    def is_subscribed(self, user_id: int) -> bool:
        r = self.user_row(user_id)
        if not r:
            return False
        return bool(r["subscribed"]) if "subscribed" in r.keys() else False

    def set_subscribed(self, user_id: int, value: bool):
        self._migrate_users_table()
        self.conn.execute("UPDATE users SET subscribed=? WHERE user_id=?", (1 if value else 0, user_id))
        self.conn.commit()

    def is_vip(self, user_id: int) -> bool:
        r = self.user_row(user_id)
        return bool(r and int(r["vip_until"] or 0) > int(time.time()))

    def vip_until(self, user_id: int) -> int:
        r = self.user_row(user_id)
        return int(r["vip_until"] or 0) if r else 0

    def set_vip_until(self, user_id: int, ts: int):
        self.conn.execute("UPDATE users SET vip_until=? WHERE user_id=?", (ts, user_id))
        self.conn.commit()

    def add_vip_month(self, user_id: int):
        now = int(time.time())
        cur = self.vip_until(user_id)
        base = cur if cur > now else now
        self.set_vip_until(user_id, base + 30 * 24 * 3600)

    def set_referrer_if_empty(self, user_id: int, referrer_id: int):
        if user_id == referrer_id:
            return
        self.conn.execute("UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL", (referrer_id, user_id))
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

    def work_enabled(self) -> bool:
        return self.get_setting("work_enabled") == "1"

    def set_work_enabled(self, enabled: bool):
        self.set_setting("work_enabled", "1" if enabled else "0")

    def get_treasury_balance(self) -> float:
        return float(self.get_setting("treasury_balance") or 0)

    def set_treasury_balance(self, value: float):
        self.set_setting("treasury_balance", f"{value:.2f}")

    def add_treasury_balance(self, amount: float):
        self.set_treasury_balance(self.get_treasury_balance() + amount)

    def junior_admin_row(self, user_id: int):
        return self.conn.execute("SELECT * FROM admin_roles WHERE user_id=? AND is_junior=1", (user_id,)).fetchone()

    def is_junior_admin(self, user_id: int) -> bool:
        return self.junior_admin_row(user_id) is not None

    def add_junior_admin(self, user_id: int, reg_price: float, noreg_price: float, max_price: float = 1.4, imo_price: float = 1.4):
        now = int(time.time())
        self.conn.execute(
            """
            INSERT INTO admin_roles(user_id,is_junior,reg_price,noreg_price,max_price,imo_price,treasury_balance,profit_total,created_at,updated_at,access_numbers,access_users,access_price,access_stats,access_work,access_mailing,access_treasury,access_payouts,access_block,access_admins,access_vip)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                is_junior=1,
                reg_price=excluded.reg_price,
                noreg_price=excluded.noreg_price,
                max_price=excluded.max_price,
                imo_price=excluded.imo_price,
                access_numbers=COALESCE(access_numbers,1),
                access_work=COALESCE(access_work,1),
                access_mailing=COALESCE(access_mailing,1),
                access_treasury=COALESCE(access_treasury,1),
                updated_at=excluded.updated_at
            """,
            (user_id, 1, reg_price, noreg_price, max_price, imo_price, 0.0, 0.0, now, now, 1, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0),
        )
        self.conn.commit()

    def remove_junior_admin(self, user_id: int):
        self.conn.execute("DELETE FROM admin_roles WHERE user_id=?", (user_id,))
        self.conn.commit()

    def set_junior_prices(self, user_id: int, reg_price: float, noreg_price: float, max_price: float, imo_price: float):
        self.conn.execute(
            "UPDATE admin_roles SET reg_price=?,noreg_price=?,max_price=?,imo_price=?,updated_at=? WHERE user_id=?",
            (reg_price, noreg_price, max_price, imo_price, int(time.time()), user_id),
        )
        self.conn.commit()

    def all_junior_admins(self):
        return self.conn.execute(
            "SELECT a.*,u.username,u.full_name FROM admin_roles a LEFT JOIN users u ON u.user_id=a.user_id WHERE a.is_junior=1 ORDER BY a.updated_at DESC"
        ).fetchall()

    def junior_admin_price(self, admin_id: int, acc_type: str) -> float:
        row = self.junior_admin_row(admin_id)
        if not row:
            return 0.0
        if acc_type == "reg":
            return float(row["reg_price"])
        if acc_type == "noreg":
            return float(row["noreg_price"])
        if acc_type == "max":
            return float(row["max_price"])
        if acc_type == "imo":
            return float(row["imo_price"])
        return 0.0

    def junior_treasury_balance(self, admin_id: int) -> float:
        row = self.junior_admin_row(admin_id)
        return float(row["treasury_balance"] if row else 0.0)

    def add_junior_treasury_balance(self, admin_id: int, amount: float):
        self.conn.execute(
            "UPDATE admin_roles SET treasury_balance=treasury_balance+?,updated_at=? WHERE user_id=?",
            (amount, int(time.time()), admin_id),
        )
        self.conn.commit()

    def sub_junior_treasury_balance(self, admin_id: int, amount: float):
        self.conn.execute(
            "UPDATE admin_roles SET treasury_balance=treasury_balance-?,updated_at=? WHERE user_id=?",
            (amount, int(time.time()), admin_id),
        )
        self.conn.commit()

    def add_junior_profit(self, admin_id: int, amount: float):
        self.conn.execute(
            "UPDATE admin_roles SET profit_total=profit_total+?,updated_at=? WHERE user_id=?",
            (amount, int(time.time()), admin_id),
        )
        self.conn.commit()

    def junior_access(self, admin_id: int, key: str) -> bool:
        row = self.junior_admin_row(admin_id)
        if not row:
            return False
        col = f"access_{key}"
        if col not in row.keys():
            return False
        return bool(int(row[col] or 0))

    def set_junior_access(self, admin_id: int, key: str, enabled: bool):
        col = f"access_{key}"
        allowed = {"access_numbers", "access_users", "access_price", "access_stats", "access_work", "access_mailing", "access_treasury", "access_payouts", "access_block", "access_admins", "access_vip"}
        if col not in allowed:
            return
        self.conn.execute(f"UPDATE admin_roles SET {col}=?,updated_at=? WHERE user_id=?", (1 if enabled else 0, int(time.time()), admin_id))
        self.conn.commit()

    def add_number(self, user_id: int, phone: str, acc_type: str, service: str = "telegram"):
        service = service if service in {"telegram", "max", "imo"} else "telegram"
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO numbers(user_id,phone,acc_type,service,status,queue_entered_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (user_id, phone, acc_type, service, "pending", now, now, now),
        )
        self.conn.commit()

    def _pending_ordered(self, service: Optional[str] = None):
        now = int(time.time())
        if service in {"max", "imo"}:
            return self.conn.execute(
                """
                SELECT n.id FROM numbers n
                JOIN users u ON u.user_id=n.user_id
                WHERE n.status='pending' AND n.service=?
                ORDER BY n.queue_entered_at, n.id
                """,
                (service,),
            ).fetchall()
        return self.conn.execute(
            """
            SELECT n.id FROM numbers n
            JOIN users u ON u.user_id=n.user_id
            WHERE n.status='pending' AND n.service='telegram'
            ORDER BY CASE WHEN u.vip_until>? THEN 0 ELSE 1 END, n.queue_entered_at, n.id
            """,
            (now,),
        ).fetchall()

    def pending_position(self, number_id: int, service: Optional[str] = None) -> Optional[int]:
        for idx, row in enumerate(self._pending_ordered(service), 1):
            if row["id"] == number_id:
                return idx
        return None

    def user_pending(self, user_id: int):
        return self.conn.execute("SELECT * FROM numbers WHERE user_id=? AND status='pending' ORDER BY queue_entered_at,id", (user_id,)).fetchall()

    def user_archive(self, user_id: int, limit: int = 10, offset: int = 0):
        return self.conn.execute(
            "SELECT * FROM numbers WHERE user_id=? AND status IN ('success','fail','rejected') ORDER BY id DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()

    def user_archive_count(self, user_id: int) -> int:
        r = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE user_id=? AND status IN ('success','fail','rejected')", (user_id,)).fetchone()
        return int(r["c"])

    def get_balance(self, user_id: int) -> float:
        r = self.conn.execute("SELECT balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return float(r["balance"] if r else 0)

    def add_balance(self, user_id: int, amount: float):
        self.conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
        self.conn.commit()

    def add_prepaid_junior_balance(self, user_id: int, amount: float):
        self.conn.execute("UPDATE users SET prepaid_junior_balance=prepaid_junior_balance+? WHERE user_id=?", (amount, user_id))
        self.conn.commit()

    def prepaid_junior_balance(self, user_id: int) -> float:
        r = self.conn.execute("SELECT prepaid_junior_balance FROM users WHERE user_id=?", (user_id,)).fetchone()
        return float(r["prepaid_junior_balance"] if r else 0.0)

    def consume_prepaid_junior_balance(self, user_id: int, amount: float) -> float:
        now = float(max(0.0, amount))
        avail = self.prepaid_junior_balance(user_id)
        used = min(avail, now)
        if used > 0:
            self.conn.execute("UPDATE users SET prepaid_junior_balance=MAX(0,prepaid_junior_balance-?) WHERE user_id=?", (used, user_id))
            self.conn.commit()
        return used

    def sub_balance(self, user_id: int, amount: float):
        self.conn.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user_id))
        self.conn.commit()

    def get_pending_for_admin(self, limit: int = 5, offset: int = 0, service: str = "telegram"):
        service = service if service in {"telegram", "max", "imo"} else "telegram"
        now = int(time.time())
        return self.conn.execute(
            """
            SELECT n.*,u.username,u.vip_until FROM numbers n
            LEFT JOIN users u ON u.user_id=n.user_id
            WHERE n.status='pending' AND n.service=?
            ORDER BY CASE WHEN u.vip_until>? THEN 0 ELSE 1 END, n.queue_entered_at,n.id
            LIMIT ? OFFSET ?
            """,
            (service, now, limit, offset),
        ).fetchall()

    def pending_count(self, service: Optional[str] = None) -> int:
        if service in {"telegram", "max", "imo"}:
            return int(self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status='pending' AND service=?", (service,)).fetchone()["c"])
        return int(self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status='pending'").fetchone()["c"])

    def clear_pending_queue(self) -> int:
        now = int(time.time())
        cur = self.conn.execute("UPDATE numbers SET status='rejected',updated_at=? WHERE status='pending'", (now,))
        self.conn.commit()
        return int(cur.rowcount or 0)

    def get_number(self, number_id: int):
        return self.conn.execute("SELECT * FROM numbers WHERE id=?", (number_id,)).fetchone()

    def set_number_status(self, number_id: int, status: str, admin_id: Optional[int] = None):
        now = int(time.time())
        if admin_id is not None:
            self.conn.execute("UPDATE numbers SET status=?,taken_by=?,updated_at=? WHERE id=?", (status, admin_id, now, number_id))
        else:
            self.conn.execute("UPDATE numbers SET status=?,updated_at=? WHERE id=?", (status, now, number_id))
        self.conn.commit()

    def try_take_number(self, number_id: int, admin_id: int) -> bool:
        now = int(time.time())
        cur = self.conn.execute(
            "UPDATE numbers SET status='in_work',taken_by=?,updated_at=? WHERE id=? AND status='pending'",
            (admin_id, now, number_id),
        )
        self.conn.commit()
        return bool(cur.rowcount)

    def set_code_request(self, number_id: int, code_type: str):
        self.conn.execute("UPDATE numbers SET code_type=?,status='waiting_code',updated_at=? WHERE id=?", (code_type, int(time.time()), number_id))
        self.conn.commit()

    def save_code(self, number_id: int, code: str):
        self.conn.execute("UPDATE numbers SET code_value=?,code_reported=1,status='awaiting_admin',updated_at=? WHERE id=?", (code, int(time.time()), number_id))
        self.conn.commit()

    def report_no_code(self, number_id: int):
        self.conn.execute("UPDATE numbers SET no_code_reported=1,status='awaiting_admin',updated_at=? WHERE id=?", (int(time.time()), number_id))
        self.conn.commit()

    def finalize_number(self, number_id: int, success: bool, reward: float):
        self.conn.execute(
            "UPDATE numbers SET status=?,reward=?,updated_at=? WHERE id=?",
            ("success" if success else "fail", reward if success else 0, int(time.time()), number_id),
        )
        self.conn.commit()

    def reject_number(self, number_id: int):
        self.conn.execute("UPDATE numbers SET status='rejected',updated_at=? WHERE id=?", (int(time.time()), number_id))
        self.conn.commit()

    def user_stats_day(self, user_id: int):
        since = int((datetime.utcnow() - timedelta(days=1)).timestamp())
        ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE user_id=? AND status='success' AND updated_at>=?", (user_id, since)).fetchone()["c"]
        bad = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE user_id=? AND status IN ('fail','rejected') AND updated_at>=?", (user_id, since)).fetchone()["c"]
        return int(ok), int(bad)

    def all_users(self):
        return self.conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()

    def user_numbers(self, user_id: int):
        return self.conn.execute("SELECT * FROM numbers WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()

    def user_last_success_time(self, user_id: int) -> int:
        r = self.conn.execute("SELECT MAX(updated_at) m FROM numbers WHERE user_id=? AND status='success'", (user_id,)).fetchone()
        return int(r["m"] or 0)

    def create_payout(self, user_id: int, amount: float):
        now = int(time.time())
        self.conn.execute("INSERT INTO payouts(user_id,amount,status,created_at,updated_at) VALUES(?,?,?,?,?)", (user_id, amount, "processing", now, now))
        self.conn.commit()

    def finish_payout(self, user_id: int, status: str, url: str = ""):
        self.conn.execute("UPDATE payouts SET status=?,check_url=?,updated_at=? WHERE user_id=? AND status='processing'", (status, url, int(time.time()), user_id))
        self.conn.commit()

    def daily_done_payouts(self):
        tz = timezone(timedelta(hours=3))
        now = datetime.now(tz)
        day_start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz)
        ts = int(day_start.astimezone(timezone.utc).timestamp())
        return self.conn.execute(
            "SELECT p.*,u.username FROM payouts p LEFT JOIN users u ON u.user_id=p.user_id WHERE p.status='done' AND p.updated_at>=? ORDER BY p.updated_at DESC",
            (ts,),
        ).fetchall()

    def create_treasury_invoice(self, invoice_id: int, amount: float, pay_url: str, created_by: int = 0):
        now = int(time.time())
        self.conn.execute("INSERT INTO treasury_invoices(invoice_id,amount,status,pay_url,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (invoice_id, amount, "active", pay_url, int(created_by or 0), now, now))
        self.conn.commit()

    def get_active_treasury_invoices(self, created_by: Optional[int] = None):
        if created_by is None:
            return self.conn.execute("SELECT * FROM treasury_invoices WHERE status='active' ORDER BY id DESC LIMIT 20").fetchall()
        return self.conn.execute("SELECT * FROM treasury_invoices WHERE status='active' AND created_by=? ORDER BY id DESC LIMIT 20", (int(created_by),)).fetchall()

    def close_treasury_invoice(self, invoice_id: int, status: str):
        self.conn.execute("UPDATE treasury_invoices SET status=?,updated_at=? WHERE invoice_id=?", (status, int(time.time()), invoice_id))
        self.conn.commit()

    def create_vip_invoice(self, user_id: int, invoice_id: int, amount: float, pay_url: str):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO vip_invoices(user_id,invoice_id,amount,status,pay_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, invoice_id, amount, "active", pay_url, now, now),
        )
        self.conn.commit()

    def active_vip_invoice(self, user_id: int):
        return self.conn.execute("SELECT * FROM vip_invoices WHERE user_id=? AND status='active' ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()

    def close_vip_invoice(self, invoice_id: int, status: str):
        self.conn.execute("UPDATE vip_invoices SET status=?,updated_at=? WHERE invoice_id=?", (status, int(time.time()), invoice_id))
        self.conn.commit()

    def vip_users(self):
        now = int(time.time())
        return self.conn.execute("SELECT * FROM users WHERE vip_until>? ORDER BY vip_until DESC", (now,)).fetchall()

    def is_phone_blocked(self, phone: str) -> bool:
        return self.conn.execute("SELECT 1 FROM blocked_numbers WHERE phone=?", (phone,)).fetchone() is not None

    def block_phone(self, phone: str, admin_id: int):
        self.conn.execute(
            "INSERT INTO blocked_numbers(phone,blocked_by,created_at) VALUES(?,?,?) ON CONFLICT(phone) DO UPDATE SET blocked_by=excluded.blocked_by,created_at=excluded.created_at",
            (phone, admin_id, int(time.time())),
        )
        self.conn.commit()

    def unblock_phone(self, phone: str):
        self.conn.execute("DELETE FROM blocked_numbers WHERE phone=?", (phone,))
        self.conn.commit()

    def blocked_phones(self):
        return self.conn.execute("SELECT * FROM blocked_numbers ORDER BY created_at DESC").fetchall()

    def recent_taken_numbers(self, limit: int = 10):
        return self.conn.execute(
            "SELECT * FROM numbers WHERE taken_by IS NOT NULL ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def phone_status_flags(self, phone: str):
        successful = self.conn.execute("SELECT 1 FROM numbers WHERE phone=? AND status='success' LIMIT 1", (phone,)).fetchone() is not None
        in_progress = self.conn.execute(
            "SELECT 1 FROM numbers WHERE phone=? AND status IN ('pending','in_work','waiting_code','awaiting_admin','awaiting_user_exit','awaiting_exit_confirm') LIMIT 1",
            (phone,),
        ).fetchone() is not None
        return successful, in_progress

    def sum_all_user_balances(self) -> float:
        rows = self.conn.execute("SELECT balance FROM users").fetchall()
        total = 0.0
        for row in rows:
            try:
                value = float(row["balance"] or 0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                total += value
        return total

    def find_number_full(self, phone: str):
        return self.conn.execute(
            """
            SELECT n.*,u.username AS user_username,a.username AS admin_username
            FROM numbers n
            LEFT JOIN users u ON u.user_id=n.user_id
            LEFT JOIN users a ON a.user_id=n.taken_by
            WHERE n.phone=?
            ORDER BY n.id DESC
            LIMIT 1
            """,
            (phone,),
        ).fetchone()

    def admin_stats_total(self, admin_id: int):
        ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status='success'", (admin_id,)).fetchone()["c"]
        bad = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status IN ('fail','rejected')", (admin_id,)).fetchone()["c"]
        return int(ok), int(bad)

    def admin_stats_day(self, admin_id: int):
        since = int((datetime.utcnow() - timedelta(days=1)).timestamp())
        ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status='success' AND updated_at>=?", (admin_id, since)).fetchone()["c"]
        bad = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status IN ('fail','rejected') AND updated_at>=?", (admin_id, since)).fetchone()["c"]
        return int(ok), int(bad)

    def service_stats_total(self, service: str):
        ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE service=? AND status='success'", (service,)).fetchone()["c"]
        bad = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE service=? AND status IN ('fail','rejected')", (service,)).fetchone()["c"]
        return int(ok), int(bad)

    def service_stats_day(self, service: str):
        since = int((datetime.utcnow() - timedelta(days=1)).timestamp())
        ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE service=? AND status='success' AND updated_at>=?", (service, since)).fetchone()["c"]
        bad = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE service=? AND status IN ('fail','rejected') AND updated_at>=?", (service, since)).fetchone()["c"]
        return int(ok), int(bad)

    def service_top_admin(self, service: str, day: bool = False):
        params = [service]
        where_day = ""
        if day:
            since = int((datetime.utcnow() - timedelta(days=1)).timestamp())
            where_day = " AND n.updated_at>=?"
            params.append(since)
        return self.conn.execute(
            f"""
            SELECT n.taken_by, COUNT(*) c, u.username
            FROM numbers n
            LEFT JOIN users u ON u.user_id=n.taken_by
            WHERE n.service=? AND n.status='success' AND n.taken_by IS NOT NULL{where_day}
            GROUP BY n.taken_by
            ORDER BY c DESC, n.taken_by ASC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    def service_top_user(self, service: str, day: bool = False):
        params = [service]
        where_day = ""
        if day:
            since = int((datetime.utcnow() - timedelta(days=1)).timestamp())
            where_day = " AND n.updated_at>=?"
            params.append(since)
        return self.conn.execute(
            f"""
            SELECT n.user_id, COUNT(*) c, u.username
            FROM numbers n
            LEFT JOIN users u ON u.user_id=n.user_id
            WHERE n.service=? AND n.status='success'{where_day}
            GROUP BY n.user_id
            ORDER BY c DESC, n.user_id ASC
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    def platform_day_stats(self):
        since = int((datetime.utcnow() - timedelta(days=1)).timestamp())
        total = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE updated_at>=?", (since,)).fetchone()["c"]
        ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status='success' AND updated_at>=?", (since,)).fetchone()["c"]
        bad = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status IN ('fail','rejected') AND updated_at>=?", (since,)).fetchone()["c"]
        top = self.conn.execute(
            """
            SELECT n.taken_by, COUNT(*) c, u.username
            FROM numbers n
            LEFT JOIN users u ON u.user_id=n.taken_by
            WHERE n.status='success' AND n.updated_at>=? AND n.taken_by IS NOT NULL
            GROUP BY n.taken_by
            ORDER BY c DESC, n.taken_by ASC
            LIMIT 1
            """,
            (since,),
        ).fetchone()
        return int(total), int(ok), int(bad), top

    def junior_numbers(self, admin_id: int, successful: bool, limit: int = 10, offset: int = 0):
        statuses = ('success',) if successful else ('fail', 'rejected')
        placeholders = ",".join("?" for _ in statuses)
        return self.conn.execute(
            f"""
            SELECT n.*,u.username AS user_username,a.username AS admin_username
            FROM numbers n
            LEFT JOIN users u ON u.user_id=n.user_id
            LEFT JOIN users a ON a.user_id=n.taken_by
            WHERE n.taken_by=? AND n.status IN ({placeholders})
            ORDER BY n.updated_at DESC, n.id DESC
            LIMIT ? OFFSET ?
            """,
            (admin_id, *statuses, limit, offset),
        ).fetchall()

    def junior_numbers_count(self, admin_id: int, successful: bool) -> int:
        statuses = ('success',) if successful else ('fail', 'rejected')
        placeholders = ",".join("?" for _ in statuses)
        r = self.conn.execute(
            f"SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status IN ({placeholders})",
            (admin_id, *statuses),
        ).fetchone()
        return int(r["c"])

    def number_full_by_id(self, number_id: int):
        return self.conn.execute(
            """
            SELECT n.*,u.username AS user_username,a.username AS admin_username
            FROM numbers n
            LEFT JOIN users u ON u.user_id=n.user_id
            LEFT JOIN users a ON a.user_id=n.taken_by
            WHERE n.id=?
            LIMIT 1
            """,
            (number_id,),
        ).fetchone()

    def junior_profit_live(self, admin_id: int) -> float:
        row = self.junior_admin_row(admin_id)
        if not row:
            return 0.0
        reg_ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status='success' AND acc_type='reg'", (admin_id,)).fetchone()["c"]
        noreg_ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status='success' AND acc_type='noreg'", (admin_id,)).fetchone()["c"]
        base_reg = float(self.get_setting("price_reg") or 0)
        base_noreg = float(self.get_setting("price_noreg") or 0)
        reg_diff = max(0.0, float(row["reg_price"]) - base_reg)
        noreg_diff = max(0.0, float(row["noreg_price"]) - base_noreg)
        max_ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status='success' AND acc_type='max'", (admin_id,)).fetchone()["c"]
        imo_ok = self.conn.execute("SELECT COUNT(*) c FROM numbers WHERE taken_by=? AND status='success' AND acc_type='imo'", (admin_id,)).fetchone()["c"]
        base_max = float(self.get_setting("price_max") or 0)
        base_imo = float(self.get_setting("price_imo") or 0)
        max_diff = max(0.0, float(row["max_price"]) - base_max)
        imo_diff = max(0.0, float(row["imo_price"]) - base_imo)
        return float(reg_ok) * reg_diff + float(noreg_ok) * noreg_diff + float(max_ok) * max_diff + float(imo_ok) * imo_diff

    def create_admin_treasury_invoice(self, admin_id: int, invoice_id: int, amount: float, pay_url: str):
        now = int(time.time())
        self.conn.execute(
            "INSERT INTO admin_treasury_invoices(admin_id,invoice_id,amount,status,pay_url,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (admin_id, invoice_id, amount, "active", pay_url, now, now),
        )
        self.conn.commit()

    def active_admin_treasury_invoices(self, admin_id: int):
        return self.conn.execute(
            "SELECT * FROM admin_treasury_invoices WHERE admin_id=? AND status='active' ORDER BY id DESC LIMIT 20",
            (admin_id,),
        ).fetchall()

    def close_admin_treasury_invoice(self, admin_id: int, invoice_id: int, status: str):
        self.conn.execute(
            "UPDATE admin_treasury_invoices SET status=?,updated_at=? WHERE admin_id=? AND invoice_id=?",
            (status, int(time.time()), admin_id, invoice_id),
        )
        self.conn.commit()



db = Storage(DB_PATH)
withdraw_lock = asyncio.Lock()


def user_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([
        ["📲 Сдать номер", "📋 Мои номера"],
        ["💰 Баланс", "📊 Статистика"],
        ["👥 Рефералка", "💎 VIP"],
    ], resize_keyboard=True)




ADMIN_ACCESS_LABELS = {
    "numbers": "🛠 Номера",
    "users": "👥 Пользователи",
    "price": "💵 Цена",
    "stats": "📈 Админ-статистика",
    "work": "⏯ Старт/Стоп ворк",
    "mailing": "📣 Рассылка",
    "treasury": "🏦 Казна",
    "payouts": "💸 Выплаты",
    "block": "🔒 Блок номера",
    "admins": "👮 Админы",
    "vip": "💎 VIP",
}


def has_admin_access(user_id: int, key: str) -> bool:
    if is_senior_admin(user_id):
        return True
    if not db.is_junior_admin(user_id):
        return False
    return db.junior_access(user_id, key)


def _junior_admin_rows(user_id: int):
    rows = []
    if has_admin_access(user_id, "numbers"):
        rows.append([ADMIN_ACCESS_LABELS["numbers"]])
    row2 = []
    if has_admin_access(user_id, "work"):
        row2.append(ADMIN_ACCESS_LABELS["work"])
    if has_admin_access(user_id, "mailing"):
        row2.append(ADMIN_ACCESS_LABELS["mailing"])
    if row2:
        rows.append(row2)
    row3 = []
    if has_admin_access(user_id, "treasury"):
        row3.append(ADMIN_ACCESS_LABELS["treasury"])
    if has_admin_access(user_id, "stats"):
        row3.append(ADMIN_ACCESS_LABELS["stats"])
    if row3:
        rows.append(row3)
    row4 = []
    if has_admin_access(user_id, "users"):
        row4.append(ADMIN_ACCESS_LABELS["users"])
    if has_admin_access(user_id, "price"):
        row4.append(ADMIN_ACCESS_LABELS["price"])
    if row4:
        rows.append(row4)
    row5 = []
    if has_admin_access(user_id, "payouts"):
        row5.append(ADMIN_ACCESS_LABELS["payouts"])
    if has_admin_access(user_id, "block"):
        row5.append(ADMIN_ACCESS_LABELS["block"])
    if row5:
        rows.append(row5)
    row6 = []
    if has_admin_access(user_id, "admins"):
        row6.append(ADMIN_ACCESS_LABELS["admins"])
    if has_admin_access(user_id, "vip"):
        row6.append(ADMIN_ACCESS_LABELS["vip"])
    if row6:
        rows.append(row6)
    return rows or [[ADMIN_ACCESS_LABELS["numbers"]], [ADMIN_ACCESS_LABELS["work"]], [ADMIN_ACCESS_LABELS["treasury"]]]

def admin_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    if is_senior_admin(user_id):
        return ReplyKeyboardMarkup([
            ["🛠 Номера", "👥 Пользователи"],
            ["💵 Цена", "📈 Админ-статистика"],
            ["⏯ Старт/Стоп ворк", "📣 Рассылка"],
            ["🏦 Казна", "💸 Выплаты"],
            ["🔒 Блок номера", "👮 Админы"],
            ["💎 VIP"],
        ], resize_keyboard=True)
    return ReplyKeyboardMarkup(_junior_admin_rows(user_id), resize_keyboard=True)


def is_senior_admin(user_id: int) -> bool:
    return user_id in SENIOR_ADMIN_IDS


def is_admin(user_id: int) -> bool:
    return is_senior_admin(user_id) or db.is_junior_admin(user_id)


def _normalize_menu_text(text: str) -> str:
    cleaned = (text or "").replace("\ufe0f", "").strip()
    return re.sub(r"\s+", " ", cleaned)


def _is_treasury_text(text: str) -> bool:
    t = _normalize_menu_text(text).replace(" ", "")
    return t in {"🏦Казна", "🏛Казна", "Казна"}






SERVICE_LABELS = {"telegram": "Telegram", "max": "MAX", "imo": "IMO"}


def service_for_acc_type(acc_type: str) -> str:
    if acc_type in {"reg", "noreg"}:
        return "telegram"
    if acc_type == "max":
        return "max"
    if acc_type == "imo":
        return "imo"
    return "telegram"


def acc_type_label(acc_type: str) -> str:
    if acc_type == "reg":
        return "рег"
    if acc_type == "noreg":
        return "не рег"
    if acc_type == "max":
        return "MAX"
    if acc_type == "imo":
        return "IMO"
    return acc_type


def _work_enabled_for_type(acc_type: str) -> bool:
    if not db.work_enabled():
        return False
    key = {"reg": "work_tg_reg", "noreg": "work_tg_noreg", "max": "work_max", "imo": "work_imo"}.get(acc_type)
    return (db.get_setting(key) or "1") == "1" if key else True


def _work_toggle_label(key: str) -> str:
    names = {
        "work_enabled": "Все сервисы",
        "work_tg_reg": "TG REG",
        "work_tg_noreg": "TG NOREG",
        "work_max": "MAX",
        "work_imo": "IMO",
    }
    state = (db.get_setting(key) or "0") == "1"
    return f"{'🟢' if state else '🔴'} {names[key]}"

def _queue_owner_label(viewer_id: int, row) -> str:
    if is_senior_admin(viewer_id):
        return f"@{row['username'] or row['user_id']}"
    return "скрыто"




JUNIOR_ACCESS_EDITABLE = [
    "numbers",
    "users",
    "price",
    "stats",
    "work",
    "mailing",
    "treasury",
    "payouts",
    "block",
    "admins",
    "vip",
]


def _junior_access_text(aid: int) -> str:
    row = db.junior_admin_row(aid)
    if not row:
        return "Младший админ не найден."
    lines = ["Выберите доступы младшего админа:"]
    for key in JUNIOR_ACCESS_EDITABLE:
        mark = "✅" if db.junior_access(aid, key) else "❌"
        lines.append(f"{mark} {ADMIN_ACCESS_LABELS[key]}")
    return "\n".join(lines)


def _junior_access_keyboard(aid: int) -> InlineKeyboardMarkup:
    kb = []
    for key in JUNIOR_ACCESS_EDITABLE:
        enabled = db.junior_access(aid, key)
        mark = "✅" if enabled else "❌"
        toggle = 0 if enabled else 1
        kb.append([InlineKeyboardButton(f"{mark} {ADMIN_ACCESS_LABELS[key]}", callback_data=f"admins:access_toggle:{aid}:{key}:{toggle}")])
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admins:view:{aid}")])
    return InlineKeyboardMarkup(kb)

def _build_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


GET_METHODS = {"getBalance", "getCurrencies", "getExchangeRates", "getMe", "getInvoices", "getChecks"}


async def _crypto_http_call(method: str, payload: dict, verify_ssl: bool = True) -> dict:
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    url = f"{CRYPTO_API}/{method}"
    connector = aiohttp.TCPConnector(ssl=_build_ssl_context() if verify_ssl else False)
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=20), connector=connector) as session:
        if method in GET_METHODS:
            async with session.get(url, params=payload or None) as resp:
                data = await resp.json(content_type=None)
        else:
            async with session.post(url, json=payload or {}) as resp:
                data = await resp.json(content_type=None)
    if not data.get("ok"):
        err = data.get("error", {}).get("name") or data.get("error") or "unknown_error"
        raise RuntimeError(f"{method} failed: {err}")
    return data


async def crypto_request(method: str, payload: dict) -> dict:
    try:
        return await _crypto_http_call(method, payload, True)
    except Exception as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSLCertVerificationError" in str(e):
            return await _crypto_http_call(method, payload, False)
        raise


async def sync_treasury_balance() -> float:
    data = await crypto_request("getBalance", {})
    usdt = 0.0
    for item in data.get("result", []):
        if item.get("currency_code") == "USDT":
            usdt = float(item.get("available", 0))
            break
    return usdt


def normalize_phone(raw: str) -> Optional[str]:
    s = raw.strip()
    if not s:
        return None
    keep = ''.join(ch for ch in s if ch.isdigit() or ch == '+')
    if keep.startswith('+7'):
        d = '+7' + ''.join(ch for ch in keep[2:] if ch.isdigit())
        return d if re.fullmatch(r"\+79\d{9}", d) else None
    digits = ''.join(ch for ch in keep if ch.isdigit())
    if len(digits) == 11 and digits.startswith('89'):
        return '+7' + digits[1:]
    if len(digits) == 11 and digits.startswith('79'):
        return '+7' + digits[1:]
    if len(digits) == 10 and digits.startswith('9'):
        return '+7' + digits
    return None


async def subscribed_in_channel(bot, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(SUB_CHANNEL, user_id)
        return m.status in {"member", "administrator", "creator"}
    except Exception:
        return False


def sub_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url="https://t.me/VINTAGEINF")],
        [InlineKeyboardButton("✅ Подписался", callback_data="sub:check")],
    ])


async def ensure_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    if db.is_subscribed(user_id):
        return True
    ok = await subscribed_in_channel(context.bot, user_id)
    if ok:
        db.set_subscribed(user_id, True)
        return True
    target = update.message or (update.callback_query.message if update.callback_query else None)
    if target:
        await target.reply_text("Чтобы пользоваться ботом, подпишитесь на канал и нажмите «Подписался».", reply_markup=sub_keyboard())
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    is_new = db.ensure_user(u.id, u.username or "", u.full_name)
    if db.is_banned(u.id):
        await update.message.reply_text("⛔ Вы заблокированы администратором.")
        return
    if context.args and is_new and str(context.args[0]).startswith("ref_"):
        ref_part = str(context.args[0]).split("_", 1)[1]
        ref_id = int(ref_part) if ref_part.isdigit() else 0
        if ref_id and ref_id != u.id and db.user_row(ref_id):
            db.set_referrer_if_empty(u.id, ref_id)
    if not await ensure_subscription(update, context, u.id):
        return
    await update.message.reply_text("✨ <b>Добро пожаловать в сервис сдачи номеров</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard(u.id) if is_admin(u.id) else user_keyboard())


async def cb_sub_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if await subscribed_in_channel(context.bot, uid):
        db.set_subscribed(uid, True)
        await q.message.reply_text("✅ Подписка подтверждена!", reply_markup=admin_keyboard(uid) if is_admin(uid) else user_keyboard())
    else:
        await q.message.reply_text("❌ Подписка не найдена.", reply_markup=sub_keyboard())


async def menu_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    db.ensure_user(user.id, user.username or "", user.full_name)
    if db.is_banned(user.id):
        await update.message.reply_text("⛔ Вы заблокированы.")
        return
    if not await ensure_subscription(update, context, user.id):
        return
    t = _normalize_menu_text(update.message.text)
    if t == "📲 Сдать номер":
        await show_submit_menu(update)
    elif t == "📋 Мои номера":
        await show_my_numbers(update)
    elif t == "💰 Баланс":
        await show_balance(update)
    elif t == "📊 Статистика":
        await show_stats(update)
    elif t == "👥 Рефералка":
        await show_referral(update, context)
    elif t == "💎 VIP":
        if is_admin(user.id) and has_admin_access(user.id, "vip"):
            await show_admin_vip_menu(update)
        else:
            await show_user_vip(update)
    elif t == "🛠 Номера" and has_admin_access(user.id, "numbers"):
        await show_admin_numbers(update, context, 0)
    elif t == "👥 Пользователи" and has_admin_access(user.id, "users"):
        await show_admin_users_menu(update)
    elif t == "💵 Цена" and has_admin_access(user.id, "price"):
        await show_price_menu(update)
    elif t == "📈 Админ-статистика" and has_admin_access(user.id, "stats"):
        await show_admin_stats(update)
    elif t == "⏯ Старт/Стоп ворк" and has_admin_access(user.id, "work"):
        await show_work_menu(update)
    elif t == "📣 Рассылка" and has_admin_access(user.id, "mailing"):
        context.user_data["mailing_mode"] = True
        await update.message.reply_text("✉️ Отправьте текст для рассылки всем пользователям.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить рассылку", callback_data="mailing:cancel")]]))
    elif _is_treasury_text(t) and has_admin_access(user.id, "treasury"):
        await show_treasury_menu(update)
    elif t == "💸 Выплаты" and has_admin_access(user.id, "payouts"):
        await show_admin_payouts(update)
    elif t == "🔒 Блок номера" and has_admin_access(user.id, "block"):
        await show_block_menu(update)
    elif t == "👮 Админы" and has_admin_access(user.id, "admins"):
        await show_admins_menu(update)



async def show_submit_menu(update: Update):
    if not db.work_enabled():
        await update.message.reply_text("🛑 <b>Сейчас STOP WORK</b>\nСдача номеров временно отключена.", parse_mode=ParseMode.HTML)
        return
    uid = update.effective_user.id
    if db.is_vip(uid):
        tg_reg = float(db.get_setting("vip_price_reg"))
        tg_noreg = float(db.get_setting("vip_price_noreg"))
    else:
        tg_reg = float(db.get_setting("price_reg"))
        tg_noreg = float(db.get_setting("price_noreg"))
    max_price = float(db.get_setting("price_max") or 0)
    imo_price = float(db.get_setting("price_imo") or 0)

    def btn(label: str, price: float, acc_type: str, icon: str):
        enabled = _work_enabled_for_type(acc_type)
        mark = "✅" if enabled else "❌"
        return [InlineKeyboardButton(f"{mark} {icon} {label} (${price:.2f})", callback_data=f"submit:{acc_type}")]

    kb_rows = [
        btn("Telegram REG", tg_reg, "reg", "📲"),
        btn("Telegram NOREG", tg_noreg, "noreg", "📲"),
        btn("MAX", max_price, "max", "📨"),
        btn("IMO", imo_price, "imo", "💬"),
    ]
    await update.message.reply_text("Выберите сервис/тип аккаунта:", reply_markup=InlineKeyboardMarkup(kb_rows))


async def cb_submit_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    acc_type = q.data.split(":")[1]
    if not _work_enabled_for_type(acc_type):
        await q.message.reply_text("🛑 Сдача по этому сервису сейчас отключена.")
        return
    context.user_data["waiting_phone_type"] = acc_type
    await q.message.reply_text("Отправьте номер телефона или сразу несколько номеров (по одному в строке).")


async def contact_or_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "waiting_phone_type" not in context.user_data:
        return
    user = update.effective_user
    acc_type = context.user_data["waiting_phone_type"]
    service = service_for_acc_type(acc_type)
    text = (update.message.text or "").strip()
    if not text and update.message.contact:
        text = update.message.contact.phone_number
    tokens = re.split(r"[\s,;]+", text)
    parsed = []
    for token in tokens:
        p = normalize_phone(token)
        if p:
            parsed.append(p)
    parsed = list(dict.fromkeys(parsed))
    if not parsed:
        await update.message.reply_text("❌ Не найдено корректных номеров. Допустимы только РФ мобильные номера формата +79XXXXXXXXX.")
        return
    added = []
    rejected = []
    for p in parsed:
        if db.is_phone_blocked(p):
            rejected.append((p, "заблокирован"))
            continue
        successful, in_progress = db.phone_status_flags(p)
        if successful:
            rejected.append((p, "уже успешно сдавался"))
            continue
        if in_progress:
            rejected.append((p, "уже находится в очереди/работе"))
            continue
        db.add_number(user.id, p, acc_type, service)
        num_id = db.conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        added.append((p, db.pending_position(num_id, service)))
    context.user_data.pop("waiting_phone_type", None)
    parts = []
    if added:
        preview = "\n".join([f"• {x[0]} — очередь #{x[1]}" for x in added[:20]])
        parts.append(f"✅ Добавлено номеров: <b>{len(added)}</b>\n{preview}")
    if rejected:
        bad = "\n".join([f"• {p} — {reason}" for p, reason in rejected[:20]])
        parts.append(f"⚠️ Отклонено: <b>{len(rejected)}</b>\n{bad}")
    await update.message.reply_text("\n\n".join(parts), parse_mode=ParseMode.HTML, reply_markup=admin_keyboard(user.id) if is_admin(user.id) else user_keyboard())


async def show_my_numbers(update: Update):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Ожидающие", callback_data="my:pending")], [InlineKeyboardButton("🗂 Архивные", callback_data="my:archive:0")]])
    await update.message.reply_text("Раздел «Мои номера»", reply_markup=kb)


async def cb_my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = q.data.split(":")
    if parts[1] == "pending":
        rows = db.user_pending(uid)
        if not rows:
            await q.message.edit_text("⏳ У вас нет ожидающих номеров.")
            return
        lines = ["<b>Ваши ожидающие номера:</b>"]
        btns = []
        for r in rows:
            pos = db.pending_position(r["id"], r["service"] if "service" in r.keys() else None)
            lines.append(f"• {r['phone']} — очередь #{pos}")
            btns.append([InlineKeyboardButton(f"Удалить {r['phone']}", callback_data=f"mydel:{r['id']}")])
        await q.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(btns))
    else:
        page = int(parts[2])
        per = 10
        total = db.user_archive_count(uid)
        rows = db.user_archive(uid, per, page * per)
        if not rows:
            await q.message.edit_text("🗂 Архив пуст.")
            return
        lines = [f"<b>Архив (стр. {page+1})</b>"]
        for i, r in enumerate(rows, 1 + page * per):
            lines.append(f"{i}. {r['phone']} {'🟢✅' if r['status']=='success' else '🔴❌'}")
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"my:archive:{page-1}"))
        if (page + 1) * per < total:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"my:archive:{page+1}"))
        await q.message.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([nav]) if nav else None)


async def cb_my_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["user_id"] != q.from_user.id or r["status"] != "pending":
        return
    db.reject_number(num_id)
    await q.message.reply_text("🗑 Номер удален из очереди.")


async def show_balance(update: Update):
    bal = db.get_balance(update.effective_user.id)
    await update.message.reply_text(f"Ваш баланс: <b>${bal:.2f}</b>", parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💸 Вывод", callback_data="balance:withdraw")]]))


async def cb_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    amount = db.get_balance(uid)
    if amount <= 0:
        await q.message.reply_text("Недостаточно средств для вывода.")
        return
    last_ok = db.user_last_success_time(uid)
    wait_left = 240 - (int(time.time()) - last_ok) if last_ok else 240
    if wait_left > 0:
        await q.message.reply_text(f"Вывод доступен через {wait_left} сек после последней успешной сдачи номера.")
        return
    if withdraw_lock.locked():
        await q.message.reply_text("Сейчас уже есть активная заявка на вывод.")
        return
    async with withdraw_lock:
        db.create_payout(uid, amount)
        try:
            if await sync_treasury_balance() < amount:
                db.finish_payout(uid, "rejected")
                await q.message.reply_text("На балансе сервиса не достаточно денег для вывода баланса, администратор скоро пополнит баланс.")
                return
            res = await crypto_request("createCheck", {"asset": "USDT", "amount": f"{amount:.2f}", "pin_to_user_id": uid, "spend_id": str(uuid.uuid4())})
            url = res["result"]["bot_check_url"]
            junior_covered = db.consume_prepaid_junior_balance(uid, amount)
            senior_part = max(0.0, amount - junior_covered)
            db.sub_balance(uid, amount)
            if senior_part > 0:
                db.set_treasury_balance(max(0.0, db.get_treasury_balance() - senior_part))
            db.finish_payout(uid, "done", url)
            await q.message.reply_text(f"✅ Выплата успешно создана на сумму ${amount:.2f}\n{url}")
        except Exception as e:
            db.finish_payout(uid, "failed")
            await q.message.reply_text(f"Не удалось выполнить вывод: {e}")


async def show_stats(update: Update):
    ok, bad = db.user_stats_day(update.effective_user.id)
    total = ok + bad
    await update.message.reply_text(f"📊 Статистика за 24ч\nУспешно: {ok}\nНе успешно: {bad}\nПроцент успеха: {(ok/total*100 if total else 0):.1f}%")


async def show_referral(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    me = await context.bot.get_me()
    link = f"https://t.me/{me.username}?start=ref_{uid}"
    ref_percent = float(db.get_setting("ref_percent"))
    refs = db.conn.execute("SELECT COUNT(*) c FROM users WHERE referrer_id=?", (uid,)).fetchone()["c"]
    earned = db.user_row(uid)["referral_earned"]
    await update.message.reply_text(f"👥 Ваша реферальная ссылка:\n{link}\n\nРеф. процент: {ref_percent:.1f}%\nРефералов: {refs}\nЗаработано: ${float(earned):.2f}")


async def show_user_vip(update: Update):
    uid = update.effective_user.id
    vip_until = db.vip_until(uid)
    now = int(time.time())
    remain_days = max(0, math.ceil((vip_until - now) / 86400)) if vip_until > now else 0
    sub_price = float(db.get_setting("vip_sub_price") or 0)
    txt = (
        "💎 <b>VIP доступ</b>\n"
        "Преимущества:\n"
        "• Приоритетная очередь перед обычными\n"
        "• Повышенная цена за REG/NOREG\n\n"
        f"Цена на 30 дней: ${sub_price:.2f}\n"
        f"Статус: {'активен' if vip_until>now else 'не активен'}\n"
        f"Осталось: {remain_days} дней"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Купить VIP", callback_data="vip:buy")],
        [InlineKeyboardButton("✅ Проверить оплату", callback_data="vip:check")],
    ])
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_user_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    action = q.data.split(":")[1]
    if action == "buy":
        amount = float(db.get_setting("vip_sub_price") or 0)
        try:
            res = await crypto_request("createInvoice", {"asset": "USDT", "amount": f"{amount:.2f}", "description": "VIP подписка на 30 дней", "expires_in": 3600})
            inv = res["result"]
            db.create_vip_invoice(uid, int(inv["invoice_id"]), amount, inv["pay_url"])
            await q.message.reply_text(f"Счет на VIP создан: ${amount:.2f}\n{inv['pay_url']}")
        except Exception as e:
            await q.message.reply_text(f"Ошибка создания VIP-счета: {e}")
    else:
        inv = db.active_vip_invoice(uid)
        if not inv:
            await q.message.reply_text("Активного счета на VIP нет.")
            return
        try:
            data = await crypto_request("getInvoices", {"invoice_ids": str(inv["invoice_id"])})
            items = data.get("result", {}).get("items", [])
            if not items:
                await q.message.reply_text("Инвойс не найден.")
                return
            st = items[0].get("status")
            if st == "paid":
                db.close_vip_invoice(inv["invoice_id"], "paid")
                db.add_vip_month(uid)
                await q.message.reply_text("✅ VIP активирован на 30 дней!")
            elif st in ("expired", "invalid"):
                db.close_vip_invoice(inv["invoice_id"], st)
                await q.message.reply_text("Счет просрочен. Создайте новый.")
            else:
                await q.message.reply_text("Оплата еще не поступила.")
        except Exception as e:
            await q.message.reply_text(f"Ошибка проверки VIP-оплаты: {e}")


async def show_admin_numbers(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Telegram", callback_data="admqueue:telegram:0")],
        [InlineKeyboardButton("MAX", callback_data="admqueue:max:0")],
        [InlineKeyboardButton("IMO", callback_data="admqueue:imo:0")],
    ])
    await update.message.reply_text("Выберите сервис очереди:", reply_markup=kb)


async def _render_admin_queue_msg(message, service: str, page: int, viewer_id: int):
    text, markup = _admin_queue_payload(service, page, viewer_id)
    await message.edit_text(text, reply_markup=markup)


def _admin_queue_payload(service: str, page: int, viewer_id: int):
    service = service if service in {"telegram", "max", "imo"} else "telegram"
    total = db.pending_count(service)
    rows = db.get_pending_for_admin(5, page * 5, service)
    if not rows:
        return f"Очередь {SERVICE_LABELS[service]} пуста.", None
    lines = [f"🛠 Очередь {SERVICE_LABELS[service]}: {total}", f"⏱ Обновлено: {datetime.now().strftime('%H:%M:%S')}"]
    kb = [
        [InlineKeyboardButton("🗑 Очистить всю очередь", callback_data=f"admclear:ask:{service}")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"admqueue:{service}:{page}")],
        [InlineKeyboardButton("📂 Сменить сервис", callback_data="admqueue:services")],
    ]
    for idx, r in enumerate(rows, 1 + page * 5):
        vip_mark = "💎 " if int(r["vip_until"] or 0) > int(time.time()) and service == "telegram" else ""
        lines.append(f"{idx}. {vip_mark}{r['phone']} ({acc_type_label(r['acc_type'])}) {_queue_owner_label(viewer_id, r)}")
        kb.append([InlineKeyboardButton(f"Открыть очередь #{idx}", callback_data=f"admnum:{r['id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admqueue:{service}:{page-1}"))
    if (page + 1) * 5 < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admqueue:{service}:{page+1}"))
    if nav:
        kb.append(nav)
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def cb_admin_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    parts = q.data.split(":")
    if len(parts) == 2 and parts[1] == "services":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Telegram", callback_data="admqueue:telegram:0")],
            [InlineKeyboardButton("MAX", callback_data="admqueue:max:0")],
            [InlineKeyboardButton("IMO", callback_data="admqueue:imo:0")],
        ])
        await q.message.reply_text("Выберите сервис очереди:", reply_markup=kb)
        return
    service = parts[1] if len(parts) > 1 else "telegram"
    page = int(parts[2]) if len(parts) > 2 else 0
    await _render_admin_queue_msg(q.message, service, page, q.from_user.id)


async def _auto_queue_tick(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    chat_id = job.data["chat_id"]
    message_id = job.data["message_id"]
    page = job.data["page"]
    service = job.data.get("service", "telegram")
    try:
        viewer_id = int(job.data.get("viewer_id", 0))
        text, markup = _admin_queue_payload(service, page, viewer_id)
        await context.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except Exception:
        pass


async def cb_admin_clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    parts = q.data.split(":")
    action = parts[1]
    service = parts[2] if len(parts) > 2 else "telegram"
    if action == "ask":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Да, очистить", callback_data=f"admclear:do:{service}")],
            [InlineKeyboardButton("❌ Нет", callback_data=f"admclear:cancel:{service}")],
        ])
        await q.message.reply_text("Очистить всю очередь (все pending номера)?", reply_markup=kb)
        return
    if action == "cancel":
        await remove_buttons(q)
        await q.message.reply_text("Очистка очереди отменена.")
        return
    if service in {"telegram", "max", "imo"}:
        now = int(time.time())
        cur = db.conn.execute("UPDATE numbers SET status='rejected',updated_at=? WHERE status='pending' AND service=?", (now, service))
        db.conn.commit()
        cleared = int(cur.rowcount or 0)
    else:
        cleared = db.clear_pending_queue()
    await remove_buttons(q)
    await q.message.reply_text(f"🗑 Очередь очищена. Удалено из pending: {cleared}.")


async def remove_buttons(q):
    try:
        await q.message.edit_reply_markup(None)
    except Exception:
        pass


async def cb_admin_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r:
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Отклонить", callback_data=f"admreject:{num_id}"), InlineKeyboardButton("✅ Взять", callback_data=f"admtake:{num_id}")],
    ])
    await q.message.reply_text(f"Номер: {r['phone']}\nТип: {acc_type_label(r['acc_type'])}\nСтатус: {r['status']}", reply_markup=kb)


async def cb_admin_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["status"] != "pending":
        await remove_buttons(q)
        return
    db.reject_number(num_id)
    await remove_buttons(q)
    await context.bot.send_message(r["user_id"], f"❌ Ваш номер {r['phone']} был отклонен администратором.")
    await q.message.reply_text("Номер отклонен.")


async def cb_admin_take(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["status"] != "pending":
        await q.message.reply_text("❌ Номер уже взят или обработан другим админом.")
        await remove_buttons(q)
        return
    if db.is_junior_admin(q.from_user.id):
        admin_price = db.junior_admin_price(q.from_user.id, r["acc_type"])
        if admin_price <= 0:
            await q.message.reply_text("❌ У вас не настроен прайс. Обратитесь к старшему админу.")
            return
        if db.junior_treasury_balance(q.from_user.id) < admin_price:
            await q.message.reply_text("❌ Недостаточно средств в вашей казне для успешного закрытия номера.")
            return
    if not db.try_take_number(num_id, q.from_user.id):
        await q.message.reply_text("❌ Этот номер уже взял другой админ.")
        await remove_buttons(q)
        return
    await remove_buttons(q)
    await context.bot.send_message(r["user_id"], f"🔔 Ваш номер {r['phone']} взяли в работу.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Запросить код с аккаунта" if r["acc_type"] == "reg" else "Запросить код с звонка", callback_data=f"askcode:{num_id}")],
        [InlineKeyboardButton("❌ Отменить номер", callback_data=f"admcancel:{num_id}")],
    ])
    await q.message.reply_text("Номер взят. Следующий шаг:", reply_markup=kb)


async def cb_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["status"] not in ("pending", "in_work", "waiting_code", "awaiting_admin", "awaiting_user_exit", "awaiting_exit_confirm"):
        await remove_buttons(q)
        return
    db.reject_number(num_id)
    await remove_buttons(q)
    await context.bot.send_message(r["user_id"], f"ℹ️ Номер {r['phone']} отменен администратором и удален из очереди.")
    await q.message.reply_text("Номер отменен и удален из очереди.")


async def cb_admin_ask_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["status"] not in ("in_work", "waiting_code"):
        await remove_buttons(q)
        return
    db.set_code_request(num_id, "код с аккаунта" if r["acc_type"] == "reg" else "код с звонка")
    await remove_buttons(q)
    await context.bot.send_message(
        r["user_id"],
        f"📩 Администратор запросил {'код с аккаунта' if r['acc_type']=='reg' else 'код с звонка'} для номера {r['phone']}.\nОтветьте на это сообщение кодом.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❗ Не пришел код", callback_data=f"nocode:{num_id}")]]),
    )
    await q.message.reply_text(
        "Запрос кода отправлен пользователю.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отменить номер", callback_data=f"admcancel:{num_id}")]]),
    )


async def on_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    txt = (update.message.text or "").strip()
    if not txt:
        return
    uid = update.effective_user.id
    rows = db.conn.execute("SELECT * FROM numbers WHERE user_id=? AND status='waiting_code' ORDER BY updated_at DESC LIMIT 1", (uid,)).fetchall()
    if not rows:
        return
    r = rows[0]
    code_len = 5 if r["acc_type"] in ("reg", "noreg") else (4 if r["acc_type"] == "imo" else 6)
    if not re.fullmatch(rf"\d{{{code_len}}}", txt):
        await update.message.reply_text(f"❌ Код должен состоять ровно из {code_len} цифр. Если код не пришел — нажмите кнопку «❗ Не пришел код».")
        return
    db.save_code(r["id"], txt)
    if r["acc_type"] == "max":
        await update.message.reply_text("✅ Код принят. Обработка аккаунта администратором может занять до 5 минут. Ожидайте.")
    else:
        await update.message.reply_text("✅ Код принят. Ожидайте подтверждения от администратора.")
    if r["acc_type"] == "reg":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Вышел", callback_data=f"regexit:ok:{r['id']}"), InlineKeyboardButton("❌ Не вышел", callback_data=f"regexit:bad:{r['id']}")],
            [InlineKeyboardButton("🛑 Отменить номер", callback_data=f"admcancel:{r['id']}")],
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Встал", callback_data=f"finalok:{r['id']}"), InlineKeyboardButton("❌ Не встал", callback_data=f"finalbad:{r['id']}")],
            [InlineKeyboardButton("🛑 Отменить номер", callback_data=f"admcancel:{r['id']}")],
        ])
    target_admin = int(r["taken_by"] or 0)
    if target_admin and is_admin(target_admin):
        await context.bot.send_message(target_admin, f"Поступил код по номеру {r['phone']}: <code>{txt}</code>", parse_mode=ParseMode.HTML, reply_markup=kb)


async def cb_reg_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    _, decision, num = q.data.split(":")
    num_id = int(num)
    r = db.get_number(num_id)
    if not r or r["status"] != "awaiting_admin":
        await remove_buttons(q)
        return
    await remove_buttons(q)
    if decision == "ok":
        await _finalize_and_notify(context, q, num_id, True)
        return
    db.set_number_status(num_id, "awaiting_user_exit")
    await context.bot.send_message(
        r["user_id"],
        f"❗ По номеру {r['phone']} нужно выйти из аккаунта в течение 2 минут и нажать кнопку ниже.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Вышел", callback_data=f"userexit:{num_id}")]]),
    )
    await q.message.reply_text("Пользователю отправлен запрос на выход из аккаунта.")


async def cb_user_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["user_id"] != q.from_user.id or r["status"] != "awaiting_user_exit":
        return
    db.set_number_status(num_id, "awaiting_exit_confirm")
    await remove_buttons(q)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data=f"finalok:{num_id}"), InlineKeyboardButton("❌ Отклонить", callback_data=f"finalbad:{num_id}")],
        [InlineKeyboardButton("🛑 Отменить номер", callback_data=f"admcancel:{num_id}")],
    ])
    target_admin = int(r["taken_by"] or 0)
    if target_admin and is_admin(target_admin):
        await context.bot.send_message(target_admin, f"Пользователь подтвердил выход из аккаунта по номеру {r['phone']}.", reply_markup=kb)
    await q.message.reply_text("✅ Отправили админу подтверждение, ожидайте решения.")


async def cb_no_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r or r["user_id"] != q.from_user.id:
        return
    db.report_no_code(num_id)
    await remove_buttons(q)
    await q.message.reply_text("Информация отправлена администратору.")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Встал", callback_data=f"finalok:{num_id}"), InlineKeyboardButton("❌ Не встал", callback_data=f"finalbad:{num_id}")],
        [InlineKeyboardButton("🛑 Отменить номер", callback_data=f"admcancel:{num_id}")],
    ])
    target_admin = int(r["taken_by"] or 0)
    if target_admin and is_admin(target_admin):
        await context.bot.send_message(target_admin, f"Пользователь сообщил: код не пришел по номеру {r['phone']}.", reply_markup=kb)


async def cb_finalize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "numbers"):
        return
    num_id = int(q.data.split(":")[1])
    ok = q.data.startswith("finalok")
    await _finalize_and_notify(context, q, num_id, ok)


async def _finalize_and_notify(context: ContextTypes.DEFAULT_TYPE, q, num_id: int, ok: bool):
    r = db.get_number(num_id)
    if not r or r["status"] in ("success", "fail", "rejected"):
        await remove_buttons(q)
        return
    urow = db.user_row(r["user_id"])
    price_key = ("vip_price_reg" if r["acc_type"] == "reg" else "vip_price_noreg") if (urow and db.is_vip(r["user_id"]) and r["acc_type"] in ("reg", "noreg")) else ({"reg": "price_reg", "noreg": "price_noreg", "max": "price_max", "imo": "price_imo"}.get(r["acc_type"], "price_noreg"))
    price = float(db.get_setting(price_key))
    db.finalize_number(num_id, ok, price)
    await remove_buttons(q)
    if ok:
        db.add_balance(r["user_id"], price)
        taken_by = int(r["taken_by"] or 0)
        if taken_by and db.is_junior_admin(taken_by) and not is_senior_admin(taken_by):
            admin_price = db.junior_admin_price(taken_by, r["acc_type"])
            if admin_price > 0:
                db.sub_junior_treasury_balance(taken_by, admin_price)
                db.add_prepaid_junior_balance(r["user_id"], price)
                diff = max(0.0, admin_price - price)
                if diff > 0:
                    db.add_treasury_balance(diff)
                    db.add_junior_profit(taken_by, diff)
        u = db.user_row(r["user_id"])
        if u and u["referrer_id"]:
            ref_percent = float(db.get_setting("ref_percent") or 0)
            ref_amount = round(price * ref_percent / 100, 2)
            if ref_amount > 0:
                db.add_balance(u["referrer_id"], ref_amount)
                db.conn.execute("UPDATE users SET referral_earned=referral_earned+? WHERE user_id=?", (ref_amount, u["referrer_id"]))
                db.conn.commit()
                await context.bot.send_message(u["referrer_id"], f"🎁 Реферальный бонус +${ref_amount:.2f} за сдачу номера вашим рефералом.")
        await context.bot.send_message(r["user_id"], f"✅ Номер {r['phone']} успешно подтвержден. +${price:.2f} зачислено на баланс.")
        await q.message.reply_text("Успешно закрыто. Баланс пользователю начислен.")
    else:
        await context.bot.send_message(r["user_id"], f"❌ Номер {r['phone']} не был зарегистрирован. Вознаграждение не начислено.")
        await q.message.reply_text("Отмечено как неуспешно.")


async def show_block_menu(update: Update):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Заблокировать", callback_data="blockmenu:block")],
        [InlineKeyboardButton("Разблокировать", callback_data="blockmenu:unblock")],
    ])
    await update.message.reply_text("🔒 Управление блокировкой номеров", reply_markup=kb)


async def cb_block_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "block"):
        return
    mode = q.data.split(":")[1]
    if mode == "block":
        rows = db.recent_taken_numbers(10)
        if not rows:
            await q.message.reply_text("Нет недавно взятых номеров.")
            return
        kb = [[InlineKeyboardButton(f"{r['phone']}", callback_data=f"blockpick:{r['id']}")] for r in rows]
        await q.message.reply_text("Выберите номер для блокировки:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        rows = db.blocked_phones()
        if not rows:
            await q.message.reply_text("Список блокировок пуст.")
            return
        kb, lines = [], ["Заблокированные номера:"]
        for r in rows[:30]:
            dt = datetime.fromtimestamp(int(r["created_at"] or 0)).strftime("%d.%m %H:%M")
            lines.append(f"• {r['phone']} ({dt})")
            kb.append([InlineKeyboardButton(f"🔓 {r['phone']}", callback_data=f"unblock:{r['phone']}")])
        await q.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def cb_block_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "block"):
        return
    num_id = int(q.data.split(":")[1])
    r = db.get_number(num_id)
    if not r:
        return
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да", callback_data=f"blockconfirm:yes:{num_id}"), InlineKeyboardButton("❌ Нет", callback_data="blockconfirm:no:0")]])
    await q.message.reply_text(f"Заблокировать номер {r['phone']}?", reply_markup=kb)


async def cb_block_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "block"):
        return
    _, decision, raw_id = q.data.split(":")
    if decision == "no":
        await q.message.reply_text("Отменено.")
        return
    r = db.get_number(int(raw_id))
    if not r:
        return
    db.block_phone(r["phone"], q.from_user.id)
    await q.message.reply_text(f"🔒 Номер {r['phone']} заблокирован.")


async def cb_unblock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "block"):
        return
    phone = q.data.split(":", 1)[1]
    db.unblock_phone(phone)
    await q.message.reply_text(f"🔓 Номер {phone} разблокирован.")


async def show_admin_users_menu(update: Update):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Поиск", callback_data="usersmenu:search")],
        [InlineKeyboardButton("📋 Все", callback_data="usersmenu:all")],
        [InlineKeyboardButton("📱 Номер", callback_data="usersmenu:number")],
    ])
    await update.message.reply_text("Раздел пользователей:", reply_markup=kb)


async def cb_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "users"):
        return
    mode = q.data.split(":")[1]
    if mode == "all":
        await show_admin_users_obj(q.message, 0)
    elif mode == "search":
        context.user_data["users_search_mode"] = True
        await q.message.reply_text("Введите ник пользователя для поиска (например @username):")
    else:
        context.user_data["number_search_mode"] = True
        await q.message.reply_text("Введите номер для поиска (например +79991234567):")


async def show_admin_users_obj(message, page: int):
    users = db.all_users()
    per = 8
    part = users[page * per: page * per + per]
    if not part:
        await message.edit_text("Пусто")
        return
    lines, kb = [f"👥 Пользователи (стр. {page+1})"], []
    for u in part:
        nums = db.user_numbers(u["user_id"])
        ok = len([x for x in nums if x["status"] == "success"])
        bad = len([x for x in nums if x["status"] in ("fail", "rejected")])
        total = ok + bad
        lines.append(f"@{u['username'] or u['user_id']} | баланс ${u['balance']:.2f} | успех {(ok/total*100 if total else 0):.1f}%")
        kb.append([InlineKeyboardButton(f"Профиль {u['user_id']}", callback_data=f"admuser:{u['user_id']}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"admusers:{page-1}"))
    if (page + 1) * per < len(users):
        nav.append(InlineKeyboardButton("➡️", callback_data=f"admusers:{page+1}"))
    if nav:
        kb.append(nav)
    await message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def show_admin_users(update: Update, page: int):
    users = db.all_users()
    if not users:
        await update.message.reply_text("Пользователей нет.")
        return
    lines, kb = ["👥 Пользователи"], [[InlineKeyboardButton("🔎 Поиск", callback_data="usersmenu:search")], [InlineKeyboardButton("📋 Все", callback_data="usersmenu:all")], [InlineKeyboardButton("📱 Номер", callback_data="usersmenu:number")]]
    await update.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def cb_admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if has_admin_access(q.from_user.id, "users"):
        await show_admin_users_obj(q.message, int(q.data.split(":")[1]))


async def admin_user_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "users") or not context.user_data.get("users_search_mode"):
        return
    row = db.user_by_username(update.message.text or "")
    context.user_data.pop("users_search_mode", None)
    if not row:
        await update.message.reply_text("Пользователь не найден.")
        return
    await update.message.reply_text(f"Найден: @{row['username'] or row['user_id']}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(f"Открыть профиль {row['user_id']}", callback_data=f"admuser:{row['user_id']}")]]))


async def admin_number_search_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "users") or not context.user_data.get("number_search_mode"):
        return
    raw = (update.message.text or "").strip()
    context.user_data.pop("number_search_mode", None)
    phone = normalize_phone(raw)
    if not phone:
        await update.message.reply_text("Введите корректный номер РФ формата +79XXXXXXXXX")
        return
    row = db.find_number_full(phone)
    if not row:
        await update.message.reply_text("Этот номер никогда не был добавлен в бот.")
        return
    dt = datetime.fromtimestamp(int(row["created_at"] or 0)).strftime("%d.%m.%Y %H:%M:%S")
    st = "успешно" if row["status"] == "success" else "не успешно"
    await update.message.reply_text(
        f"Номер: {row['phone']}\n"
        f"Пользователь: @{row['user_username'] or row['user_id']}\n"
        f"Админ: @{row['admin_username'] or row['taken_by'] or '-'}\n"
        f"Дата сдачи: {dt}\n"
        f"Статус: {st}"
    )


async def cb_admin_user_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "users"):
        return
    uid = int(q.data.split(":")[1])
    u = db.user_row(uid)
    if not u:
        return
    nums = db.user_numbers(uid)
    ok = len([x for x in nums if x["status"] == "success"])
    bad = len([x for x in nums if x["status"] in ("fail", "rejected")])
    pending = len([x for x in nums if x["status"] in ("pending", "in_work", "waiting_code", "awaiting_admin")])
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Бан", callback_data=f"ban:{uid}"), InlineKeyboardButton("♻️ Разбан", callback_data=f"unban:{uid}")],
        [InlineKeyboardButton("➕ Добавить баланс", callback_data=f"baladd:{uid}"), InlineKeyboardButton("➖ Удалить баланс", callback_data=f"balsub:{uid}")],
    ])
    await q.message.reply_text(f"Пользователь: @{u['username'] or uid}\nБаланс: ${u['balance']:.2f}\nУспешно: {ok}\nНеуспешно: {bad}\nВ работе: {pending}", reply_markup=kb)


async def cb_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "users"):
        return
    action, uid = q.data.split(":")
    db.set_ban(int(uid), action == "ban")
    await q.message.reply_text("Готово.")


async def cb_balance_manage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "users"):
        return
    act, uid = q.data.split(":")
    context.user_data["bal_manage"] = (act, int(uid))
    await q.message.reply_text("Введите сумму в $:")


async def admin_balance_manage_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "users") or "bal_manage" not in context.user_data:
        return
    try:
        amount = float((update.message.text or "").replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число.")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть > 0")
        return
    act, uid = context.user_data.pop("bal_manage")
    if act == "baladd":
        db.add_balance(uid, amount)
        await update.message.reply_text("Баланс добавлен ✅")
    else:
        db.sub_balance(uid, amount)
        await update.message.reply_text("Баланс уменьшен ✅")


async def show_price_menu(update: Update):
    reg = db.get_setting("price_reg")
    noreg = db.get_setting("price_noreg")
    maxp = db.get_setting("price_max")
    imop = db.get_setting("price_imo")
    ref = db.get_setting("ref_percent")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Изменить TG REG", callback_data="price:reg")],
        [InlineKeyboardButton("Изменить TG NOREG", callback_data="price:noreg")],
        [InlineKeyboardButton("Изменить MAX", callback_data="price:max")],
        [InlineKeyboardButton("Изменить IMO", callback_data="price:imo")],
        [InlineKeyboardButton("Изменить реф %", callback_data="price:ref")],
    ])
    await update.message.reply_text(f"Текущие цены:\nTG REG: ${reg}\nTG NOREG: ${noreg}\nMAX: ${maxp}\nIMO: ${imop}\nРеф %: {ref}", reply_markup=kb)



async def cb_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if has_admin_access(q.from_user.id, "price"):
        context.user_data["price_edit"] = q.data.split(":")[1]
        await q.message.reply_text("Введите новое значение:")


async def admin_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "price") or "price_edit" not in context.user_data:
        return
    try:
        val = float((update.message.text or "").replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число.")
        return
    mode = context.user_data.pop("price_edit")
    if mode == "reg":
        db.set_setting("price_reg", f"{val:.2f}")
    elif mode == "noreg":
        db.set_setting("price_noreg", f"{val:.2f}")
    elif mode == "max":
        db.set_setting("price_max", f"{val:.2f}")
    elif mode == "imo":
        db.set_setting("price_imo", f"{val:.2f}")
    else:
        db.set_setting("ref_percent", f"{val:.2f}")
    await update.message.reply_text("Обновлено ✅")


async def show_admin_stats(update: Update):
    total = db.conn.execute("SELECT COUNT(*) c FROM numbers").fetchone()["c"]
    pending = db.pending_count()
    success = db.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status='success'").fetchone()["c"]
    failed = db.conn.execute("SELECT COUNT(*) c FROM numbers WHERE status IN ('fail','rejected')").fetchone()["c"]
    day_total, day_success, day_failed, top = db.platform_day_stats()
    top_line = f"@{top['username'] or top['taken_by']} — {int(top['c'])}" if top else "-"

    lines = [
        "📈 Общая статистика",
        f"Всего номеров: {total}",
        f"В очереди: {pending}",
        f"Успешно: {success}",
        f"Неуспешно: {failed}",
        "",
        "🕒 Статистика за 24ч",
        f"Всего: {day_total}",
        f"Успешно: {day_success}",
        f"Неуспешно: {day_failed}",
        f"Топ админ (24ч): {top_line}",
        "",
    ]
    for service in ("telegram", "max", "imo"):
        ok_total, bad_total = db.service_stats_total(service)
        ok_day, bad_day = db.service_stats_day(service)
        top_admin_total = db.service_top_admin(service, day=False)
        top_user_total = db.service_top_user(service, day=False)
        top_admin_day = db.service_top_admin(service, day=True)
        top_user_day = db.service_top_user(service, day=True)
        lines.extend([
            f"📦 {SERVICE_LABELS[service]}",
            f"Всего успешно/неуспешно: {ok_total}/{bad_total}",
            f"За 24ч успешно/неуспешно: {ok_day}/{bad_day}",
            f"Лучший админ (всего): @{top_admin_total['username'] or top_admin_total['taken_by']} — {int(top_admin_total['c'])}" if top_admin_total else "Лучший админ (всего): -",
            f"Лучший пользователь (всего): @{top_user_total['username'] or top_user_total['user_id']} — {int(top_user_total['c'])}" if top_user_total else "Лучший пользователь (всего): -",
            f"Лучший админ (24ч): @{top_admin_day['username'] or top_admin_day['taken_by']} — {int(top_admin_day['c'])}" if top_admin_day else "Лучший админ (24ч): -",
            f"Лучший пользователь (24ч): @{top_user_day['username'] or top_user_day['user_id']} — {int(top_user_day['c'])}" if top_user_day else "Лучший пользователь (24ч): -",
            "",
        ])
    await update.message.reply_text("\n".join(lines))


async def show_admin_payouts(update: Update):
    rows = db.daily_done_payouts()
    if not rows:
        await update.message.reply_text("За сегодня (UTC+3) завершенных выплат нет.")
        return
    tz = timezone(timedelta(hours=3))
    lines = ["💸 Выплаты за сегодня (UTC+3):"]
    for r in rows[:50]:
        dt = datetime.fromtimestamp(r["updated_at"], tz).strftime("%H:%M:%S")
        lines.append(f"• ${float(r['amount']):.2f} | @{r['username'] or r['user_id']} | {dt}")
    await update.message.reply_text("\n".join(lines))


async def show_admins_menu(update: Update):
    rows = db.all_junior_admins()
    kb = [
        [InlineKeyboardButton("➕ Добавить младшего", callback_data="admins:add")],
    ]
    for r in rows[:50]:
        kb.append([InlineKeyboardButton(f"@{r['username'] or r['user_id']}", callback_data=f"admins:view:{r['user_id']}")])
    text = "👮 Управление админами\nВыберите админа для просмотра статистики."
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def cb_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "admins"):
        return
    parts = q.data.split(":")
    action = parts[1]
    if action == "add":
        context.user_data["junior_add_mode"] = True
        await q.message.reply_text("Введите @username и через пробел цены TG_REG TG_NOREG MAX IMO. Пример: @user 1.40 1.30 1.20 1.10")
    elif action == "view":
        aid = int(parts[2])
        row = db.junior_admin_row(aid)
        if not row:
            await q.message.reply_text("Младший админ не найден.")
            return
        u = db.user_row(aid)
        ok_total, bad_total = db.admin_stats_total(aid)
        ok_day, bad_day = db.admin_stats_day(aid)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 Удалить админа", callback_data=f"admins:del:{aid}")],
            [InlineKeyboardButton("💵 Изм. прайс", callback_data=f"admins:price:{aid}")],
            [InlineKeyboardButton("🔐 Изменить доступ", callback_data=f"admins:access:{aid}")],
            [InlineKeyboardButton("📱 Номера", callback_data=f"admins:numbers:{aid}")],
        ])
        await q.message.reply_text(
            f"👮 @{u['username'] if u else aid}\n"
            f"Успешно всего: {ok_total}\n"
            f"Не успешно всего: {bad_total}\n"
            f"Успешно за 24ч: {ok_day}\n"
            f"Не успешно за 24ч: {bad_day}\n"
            f"Казна: ${float(row['treasury_balance']):.2f}\n"
            f"Прайс TG REG/NOREG: ${float(row['reg_price']):.2f}/${float(row['noreg_price']):.2f}\n"
            f"Прайс MAX/IMO: ${float(row['max_price']):.2f}/${float(row['imo_price']):.2f}\n"
            f"Прибыль от админа: ${db.junior_profit_live(aid):.2f}",
            reply_markup=kb,
        )
    elif action == "del":
        aid = int(parts[2])
        db.remove_junior_admin(aid)
        await q.message.reply_text("Младший админ удален.")
    elif action == "price":
        aid = int(parts[2])
        context.user_data["junior_price_mode"] = aid
        await q.message.reply_text("Введите новый прайс TG_REG TG_NOREG MAX IMO через пробел. Пример: 1.40 1.30 1.20 1.10")
    elif action == "access":
        aid = int(parts[2])
        await q.message.reply_text(_junior_access_text(aid), reply_markup=_junior_access_keyboard(aid))
    elif action == "access_toggle":
        aid = int(parts[2])
        key = parts[3]
        enabled = parts[4] == "1"
        db.set_junior_access(aid, key, enabled)
        await q.message.edit_text(_junior_access_text(aid), reply_markup=_junior_access_keyboard(aid))
    elif action == "numbers":
        aid = int(parts[2])
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Успешные", callback_data=f"admins:numlist:{aid}:ok:0")],
            [InlineKeyboardButton("❌ Отклоненные", callback_data=f"admins:numlist:{aid}:bad:0")],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"admins:view:{aid}")],
        ])
        await q.message.reply_text("Выберите список номеров:", reply_markup=kb)
    elif action == "numlist":
        aid = int(parts[2])
        mode = parts[3]
        page = int(parts[4])
        successful = mode == "ok"
        per = 8
        total = db.junior_numbers_count(aid, successful)
        rows = db.junior_numbers(aid, successful, per, page * per)
        if not rows:
            await q.message.reply_text("Номеров в этом разделе нет.")
            return
        u = db.user_row(aid)
        lines = [f"Номера админа @{u['username'] if u else aid} (стр. {page + 1})"]
        kb = []
        for r in rows:
            st = "✅" if r["status"] == "success" else "❌"
            lines.append(f"{st} {r['phone']}")
            kb.append([InlineKeyboardButton(f"{st} {r['phone']}", callback_data=f"admins:numview:{aid}:{r['id']}:{mode}:{page}")])
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️", callback_data=f"admins:numlist:{aid}:{mode}:{page-1}"))
        if (page + 1) * per < total:
            nav.append(InlineKeyboardButton("➡️", callback_data=f"admins:numlist:{aid}:{mode}:{page+1}"))
        if nav:
            kb.append(nav)
        kb.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"admins:numbers:{aid}")])
        await q.message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))
    elif action == "numview":
        aid = int(parts[2])
        number_id = int(parts[3])
        mode = parts[4]
        page = int(parts[5])
        row = db.number_full_by_id(number_id)
        if not row:
            await q.message.reply_text("Номер не найден.")
            return
        dt = datetime.fromtimestamp(int(row["created_at"] or 0)).strftime("%d.%m.%Y %H:%M:%S")
        await q.message.reply_text(
            f"Номер: {row['phone']}\n"
            f"Пользователь: @{row['user_username'] or row['user_id']}\n"
            f"Админ: @{row['admin_username'] or row['taken_by'] or '-'}\n"
            f"Дата/время: {dt}\n"
            f"Статус: {row['status']}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ К списку", callback_data=f"admins:numlist:{aid}:{mode}:{page}")]])
        )


async def admin_junior_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "admins") or not context.user_data.get("junior_add_mode"):
        return
    raw = (update.message.text or "").strip().split()
    context.user_data.pop("junior_add_mode", None)
    if len(raw) != 5:
        await update.message.reply_text("Неверный формат. Пример: @user 1.40 1.30 1.20 1.10")
        return
    row = db.user_by_username(raw[0])
    if not row:
        await update.message.reply_text("Пользователь не найден.")
        return
    try:
        reg_price = float(raw[1].replace(",", "."))
        noreg_price = float(raw[2].replace(",", "."))
        max_price = float(raw[3].replace(",", "."))
        imo_price = float(raw[4].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Цены должны быть числами.")
        return
    db.add_junior_admin(int(row["user_id"]), reg_price, noreg_price, max_price, imo_price)
    await update.message.reply_text("Младший админ добавлен ✅")


async def admin_junior_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "admins") or "junior_price_mode" not in context.user_data:
        return
    aid = int(context.user_data.pop("junior_price_mode"))
    raw = (update.message.text or "").strip().split()
    if len(raw) != 4:
        await update.message.reply_text("Неверный формат. Пример: 1.40 1.30 1.20 1.10")
        return
    try:
        reg_price = float(raw[0].replace(",", "."))
        noreg_price = float(raw[1].replace(",", "."))
        max_price = float(raw[2].replace(",", "."))
        imo_price = float(raw[3].replace(",", "."))
    except ValueError:
        await update.message.reply_text("Цены должны быть числами.")
        return
    db.set_junior_prices(aid, reg_price, noreg_price, max_price, imo_price)
    await update.message.reply_text("Прайс младшего админа обновлен ✅")


async def show_work_menu(update: Update):
    global_state = "🟢 WORK" if db.work_enabled() else "🔴 STOP WORK"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(("Остановить ворк (всё)" if db.work_enabled() else "Запустить ворк (всё)"), callback_data="work:toggle:work_enabled")],
        [InlineKeyboardButton(_work_toggle_label("work_tg_reg"), callback_data="work:toggle:work_tg_reg")],
        [InlineKeyboardButton(_work_toggle_label("work_tg_noreg"), callback_data="work:toggle:work_tg_noreg")],
        [InlineKeyboardButton(_work_toggle_label("work_max"), callback_data="work:toggle:work_max")],
        [InlineKeyboardButton(_work_toggle_label("work_imo"), callback_data="work:toggle:work_imo")],
    ])
    await update.message.reply_text(f"<b>Режим работы сервиса:</b> {global_state}", parse_mode=ParseMode.HTML, reply_markup=kb)


async def notify_all_users(context: ContextTypes.DEFAULT_TYPE, text: str):
    for uid in db.all_user_ids():
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
        except Exception:
            pass


async def cb_work_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "work"):
        return
    parts = q.data.split(":")
    key = parts[2] if len(parts) > 2 else "work_enabled"
    if key == "work_enabled":
        state = not db.work_enabled()
        db.set_work_enabled(state)
        await notify_all_users(context, "🚀 <b>WORK ЗАПУЩЕН</b>" if state else "🛑 <b>STOP WORK</b>")
        await q.message.reply_text("Глобальный статус ворка переключен.")
    else:
        cur = (db.get_setting(key) or "1") == "1"
        db.set_setting(key, "0" if cur else "1")
        await q.message.reply_text(f"Обновлен переключатель: {_work_toggle_label(key)}")


async def show_treasury_menu(update: Update):
    uid = update.effective_user.id
    if db.is_junior_admin(uid) and not is_senior_admin(uid):
        row = db.junior_admin_row(uid)
        bal = float(row["treasury_balance"] if row else 0)
        reg_price = float(row["reg_price"] if row else 0)
        noreg_price = float(row["noreg_price"] if row else 0)
        reg_count = int(bal // reg_price) if reg_price > 0 and bal > 0 else 0
        noreg_count = int(bal // noreg_price) if noreg_price > 0 and bal > 0 else 0
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Пополнить казну", callback_data="treasury:topup")],
            [InlineKeyboardButton("✅ Проверить пополнение", callback_data="treasury:check")],
        ])
        await update.message.reply_text(
            "🏦 <b>Казна младшего админа</b>\n"
            f"Ваш баланс: <b>{bal:.2f}</b> USDT\n"
            f"Ваш прайс REG/NOREG: <b>{reg_price:.2f}</b>/<b>{noreg_price:.2f}</b>\n"
            f"Хватит сдач: REG ~ <b>{reg_count}</b>, NOREG ~ <b>{noreg_count}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )
        return

    bal = float(db.get_treasury_balance())
    total_users_balance = float(db.sum_all_user_balances())
    if not math.isfinite(bal):
        bal = 0.0
    if not math.isfinite(total_users_balance):
        total_users_balance = 0.0
    free_funds = bal - total_users_balance
    if not math.isfinite(free_funds):
        free_funds = 0.0
    reg_price = float(db.get_setting("price_reg") or 0)
    noreg_price = float(db.get_setting("price_noreg") or 0)
    if not math.isfinite(reg_price):
        reg_price = 0.0
    if not math.isfinite(noreg_price):
        noreg_price = 0.0
    reg_count = int(free_funds // reg_price) if reg_price > 0 and free_funds > 0 else 0
    noreg_count = int(free_funds // noreg_price) if noreg_price > 0 and free_funds > 0 else 0
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Пополнить казну", callback_data="treasury:topup")],
        [InlineKeyboardButton("🔄 Обновить баланс", callback_data="treasury:refresh")],
        [InlineKeyboardButton("✅ Проверить пополнение", callback_data="treasury:check")],
        [InlineKeyboardButton("💸 Вывести с казны", callback_data="treasury:withdraw")],
    ])
    await update.message.reply_text(
        "🏦 <b>Казна сервиса</b>\n"
        f"Текущий баланс (USDT): <b>{bal:.2f}</b>\n"
        f"Общий баланс пользователей: <b>{total_users_balance:.2f}</b>\n"
        f"Свободные средства казны: <b>{free_funds:.2f}</b>\n"
        f"Хватит сдач: REG ~ <b>{reg_count}</b>, NOREG ~ <b>{noreg_count}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )


async def cb_treasury_actions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "treasury"):
        return
    action = q.data.split(":")[1]
    if db.is_junior_admin(q.from_user.id):
        if action == "topup":
            context.user_data["junior_treasury_topup_mode"] = True
            await q.message.reply_text("Введите сумму пополнения вашей казны в USDT:")
            return
        if action == "check":
            active = db.active_admin_treasury_invoices(q.from_user.id)
            if not active:
                await q.message.reply_text("Активных инвойсов на пополнение нет.")
                return
            try:
                data = await crypto_request("getInvoices", {"invoice_ids": ",".join(str(x["invoice_id"]) for x in active)})
                paid_sum = 0.0
                paid = 0
                for inv in data.get("result", {}).get("items", []):
                    if inv.get("status") == "paid":
                        db.close_admin_treasury_invoice(q.from_user.id, int(inv["invoice_id"]), "paid")
                        paid += 1
                        paid_sum += float(inv.get("amount") or 0)
                    elif inv.get("status") in ("expired", "invalid"):
                        db.close_admin_treasury_invoice(q.from_user.id, int(inv["invoice_id"]), inv.get("status"))
                if paid_sum > 0:
                    db.add_junior_treasury_balance(q.from_user.id, paid_sum)
                await q.message.reply_text(f"Проверка завершена. Оплачено счетов: {paid}. Ваша казна: {db.junior_treasury_balance(q.from_user.id):.2f} USDT")
            except Exception as e:
                await q.message.reply_text(f"Ошибка при проверке пополнения: {e}")
            return
        await q.message.reply_text("Для младшего админа доступно только пополнение и проверка пополнения казны.")
        return

    if action == "refresh":
        try:
            wallet_bal = await sync_treasury_balance()
            await q.message.reply_text(
                f"🔄 Обновлено. Баланс CryptoPay: {wallet_bal:.2f} USDT\n"
                f"Внутренняя казна старших: {db.get_treasury_balance():.2f} USDT"
            )
        except Exception as e:
            await q.message.reply_text(f"Не удалось обновить баланс казны: {e}")
    elif action == "topup":
        context.user_data["treasury_topup_mode"] = True
        await q.message.reply_text("Введите сумму пополнения в USDT (например 100):")
    elif action == "withdraw":
        context.user_data["treasury_withdraw_mode"] = True
        await q.message.reply_text("Введите сумму вывода из казны в USDT:")
    elif action == "check":
        active = db.get_active_treasury_invoices(q.from_user.id)
        if not active:
            await q.message.reply_text("Активных инвойсов на пополнение нет.")
            return
        try:
            data = await crypto_request("getInvoices", {"invoice_ids": ",".join(str(x["invoice_id"]) for x in active)})
            paid = 0
            paid_sum = 0.0
            for inv in data.get("result", {}).get("items", []):
                if inv.get("status") == "paid":
                    db.close_treasury_invoice(int(inv["invoice_id"]), "paid")
                    paid += 1
                    paid_sum += float(inv.get("amount") or 0)
                elif inv.get("status") in ("expired", "invalid"):
                    db.close_treasury_invoice(int(inv["invoice_id"]), inv.get("status"))
            if paid_sum > 0:
                db.add_treasury_balance(paid_sum)
            await q.message.reply_text(f"Проверка завершена. Оплачено: {paid}. Баланс казны: {db.get_treasury_balance():.2f} USDT")
        except Exception as e:
            await q.message.reply_text(f"Ошибка при проверке пополнения: {e}")


async def admin_treasury_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if has_admin_access(update.effective_user.id, "treasury") and context.user_data.get("junior_treasury_topup_mode"):
        raw = (update.message.text or "").strip().replace(" ", "").replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            await update.message.reply_text("Введите корректную сумму.")
            return
        if amount <= 0:
            await update.message.reply_text("Сумма должна быть больше 0.")
            return
        try:
            res = await crypto_request("createInvoice", {"asset": "USDT", "amount": f"{amount:.2f}", "description": "Пополнение казны младшего админа", "expires_in": 1800})
            inv = res["result"]
            db.create_admin_treasury_invoice(update.effective_user.id, int(inv["invoice_id"]), amount, inv["pay_url"])
            context.user_data.pop("junior_treasury_topup_mode", None)
            await update.message.reply_text(f"✅ Инвойс на пополнение создан: ${amount:.2f}\n{inv['pay_url']}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка при создании инвойса: {e}")
        return

    if has_admin_access(update.effective_user.id, "treasury") and context.user_data.get("treasury_withdraw_mode"):
        raw = (update.message.text or "").strip().replace(" ", "").replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            await update.message.reply_text("Введите корректную сумму.")
            return
        if amount <= 0:
            await update.message.reply_text("Сумма должна быть больше 0.")
            return
        bal = float(db.get_treasury_balance())
        liabilities = float(db.sum_all_user_balances())
        free = bal - liabilities
        if amount > free:
            await update.message.reply_text(f"Недостаточно свободных средств казны. Доступно: {max(0.0, free):.2f} USDT")
            return
        try:
            res = await crypto_request("createCheck", {"asset": "USDT", "amount": f"{amount:.2f}", "pin_to_user_id": update.effective_user.id, "spend_id": str(uuid.uuid4())})
            url = res["result"]["bot_check_url"]
            db.set_treasury_balance(max(0.0, db.get_treasury_balance() - amount))
            context.user_data.pop("treasury_withdraw_mode", None)
            await update.message.reply_text(f"✅ Вывод из казны создан: ${amount:.2f}\n{url}")
        except Exception as e:
            await update.message.reply_text(f"Ошибка при выводе из казны: {e}")
        return

    if not has_admin_access(update.effective_user.id, "treasury") or not context.user_data.get("treasury_topup_mode"):
        return
    raw = (update.message.text or "").strip()
    menu_texts = {"📲 Сдать номер", "📋 Мои номера", "💰 Баланс", "📊 Статистика", "👥 Рефералка", "🛠 Номера", "👥 Пользователи", "💵 Цена", "📈 Админ-статистика", "⏯ Старт/Стоп ворк", "📣 Рассылка", "🏦 Казна", "💸 Выплаты", "🔒 Блок номера", "👮 Админы", "💎 VIP"}
    if raw in menu_texts:
        context.user_data.pop("treasury_topup_mode", None)
        context.user_data.pop("treasury_withdraw_mode", None)
        return
    t = raw.replace(" ", "").replace(",", ".")
    if t.lower() in {"отмена", "cancel"}:
        context.user_data.pop("treasury_topup_mode", None)
        context.user_data.pop("treasury_withdraw_mode", None)
        await update.message.reply_text("Операция с казной отменена.")
        return
    try:
        amount = float(t)
    except ValueError:
        await update.message.reply_text("Введите корректную сумму или «Отмена».")
        return
    if amount <= 0:
        await update.message.reply_text("Сумма должна быть больше 0.")
        return
    try:
        res = await crypto_request("createInvoice", {"asset": "USDT", "amount": f"{amount:.2f}", "description": "Пополнение казны сервиса", "expires_in": 1800})
        inv = res["result"]
        db.create_treasury_invoice(int(inv["invoice_id"]), amount, inv["pay_url"], update.effective_user.id)
        context.user_data.pop("treasury_topup_mode", None)
        await update.message.reply_text(f"✅ Инвойс на пополнение создан: ${amount:.2f}\n{inv['pay_url']}")
    except Exception as e:
        await update.message.reply_text(f"Ошибка при создании инвойса: {e}")


async def cb_mailing_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "mailing"):
        return
    context.user_data.pop("mailing_mode", None)
    await q.message.reply_text("Рассылка отменена.")




async def admin_mailing_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "mailing") or not context.user_data.get("mailing_mode"):
        return
    text = (update.message.text or "").strip()
    if text.lower() in {"отмена", "cancel"}:
        context.user_data.pop("mailing_mode", None)
        await update.message.reply_text("Рассылка отменена.")
        return
    if not text:
        await update.message.reply_text("Текст рассылки пуст.")
        return
    sent = failed = 0
    for uid in db.all_user_ids():
        try:
            await context.bot.send_message(uid, f"📣 <b>Сообщение от администрации</b>\n\n{text}", parse_mode=ParseMode.HTML)
            sent += 1
        except Exception:
            failed += 1
    context.user_data.pop("mailing_mode", None)
    await update.message.reply_text(f"Рассылка завершена. Доставлено: {sent}, ошибок: {failed}.")


async def show_admin_vip_menu(update: Update):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 VIP пользователи", callback_data="admvip:list")],
        [InlineKeyboardButton("➕ Добавить VIP", callback_data="admvip:add")],
        [InlineKeyboardButton("🚫 Аннулировать VIP", callback_data="admvip:revoke")],
        [InlineKeyboardButton("💰 Цена VIP", callback_data="admvip:price")],
    ])
    await update.message.reply_text("VIP раздел админа:", reply_markup=kb)


async def cb_admin_vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "vip"):
        return
    action = q.data.split(":")[1]
    if action == "list":
        users = db.vip_users()
        if not users:
            await q.message.reply_text("VIP пользователей нет.")
            return
        lines = ["💎 VIP пользователи:"]
        for u in users[:100]:
            left = max(0, math.ceil((int(u["vip_until"]) - int(time.time())) / 86400)) if int(u["vip_until"]) > int(time.time()) else 0
            lines.append(f"• @{u['username'] or u['user_id']} — {left} дн")
        await q.message.reply_text("\n".join(lines))
    elif action == "add":
        context.user_data["vip_add_mode"] = True
        await q.message.reply_text("Введите @username пользователя для выдачи VIP на 30 дней:")
    elif action == "revoke":
        users = db.vip_users()
        if not users:
            await q.message.reply_text("VIP пользователей нет.")
            return
        kb = []
        for u in users[:50]:
            kb.append([InlineKeyboardButton(f"Аннулировать @{u['username'] or u['user_id']}", callback_data=f"admviprevoke:{u['user_id']}")])
        await q.message.reply_text("Выберите пользователя:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        reg = db.get_setting("vip_price_reg")
        noreg = db.get_setting("vip_price_noreg")
        sub = db.get_setting("vip_sub_price")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Изм VIP REG", callback_data="admvipprice:reg")],
            [InlineKeyboardButton("Изм VIP NOREG", callback_data="admvipprice:noreg")],
            [InlineKeyboardButton("Изм VIP подписку", callback_data="admvipprice:sub")],
        ])
        await q.message.reply_text(f"VIP цены:\nREG: ${reg}\nNOREG: ${noreg}\nПодписка: ${sub}", reply_markup=kb)


async def cb_admin_vip_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "vip"):
        return
    uid = int(q.data.split(":")[1])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Да", callback_data=f"admviprevokeok:{uid}"), InlineKeyboardButton("❌ Нет", callback_data="admviprevoke_cancel")]])
    await q.message.reply_text("Точно аннулировать VIP?", reply_markup=kb)


async def cb_admin_vip_revoke_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "vip"):
        return
    if q.data.endswith("cancel"):
        await remove_buttons(q)
        await q.message.reply_text("Отменено.")
        return
    uid = int(q.data.split(":")[1])
    db.set_vip_until(uid, 0)
    await remove_buttons(q)
    await q.message.reply_text("VIP аннулирован.")


async def cb_admin_vip_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not has_admin_access(q.from_user.id, "vip"):
        return
    mode = q.data.split(":")[1]
    context.user_data["vip_price_edit"] = mode
    await q.message.reply_text("Введите новое значение:")


async def admin_vip_add_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "vip") or not context.user_data.get("vip_add_mode"):
        return
    row = db.user_by_username(update.message.text or "")
    context.user_data.pop("vip_add_mode", None)
    if not row:
        await update.message.reply_text("Пользователь не найден.")
        return
    db.add_vip_month(row["user_id"])
    await update.message.reply_text("VIP выдан на 30 дней ✅")


async def admin_vip_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not has_admin_access(update.effective_user.id, "vip") or "vip_price_edit" not in context.user_data:
        return
    try:
        val = float((update.message.text or "").replace(",", "."))
    except ValueError:
        await update.message.reply_text("Нужно число.")
        return
    mode = context.user_data.pop("vip_price_edit")
    if mode == "reg":
        db.set_setting("vip_price_reg", f"{val:.2f}")
    elif mode == "noreg":
        db.set_setting("vip_price_noreg", f"{val:.2f}")
    else:
        db.set_setting("vip_sub_price", f"{val:.2f}")
    await update.message.reply_text("VIP цена обновлена ✅")


def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(cb_sub_check, pattern=r"^sub:check$"))
    app.add_handler(CallbackQueryHandler(cb_submit_type, pattern=r"^submit:"))
    app.add_handler(CallbackQueryHandler(cb_my, pattern=r"^my:"))
    app.add_handler(CallbackQueryHandler(cb_my_delete, pattern=r"^mydel:"))
    app.add_handler(CallbackQueryHandler(cb_withdraw, pattern=r"^balance:withdraw$"))
    app.add_handler(CallbackQueryHandler(cb_user_vip, pattern=r"^vip:(buy|check)$"))

    app.add_handler(CallbackQueryHandler(cb_admin_queue, pattern=r"^admqueue:"))
    app.add_handler(CallbackQueryHandler(cb_admin_clear_queue, pattern=r"^admclear:"))
    app.add_handler(CallbackQueryHandler(cb_admin_number, pattern=r"^admnum:"))
    app.add_handler(CallbackQueryHandler(cb_admin_reject, pattern=r"^admreject:"))
    app.add_handler(CallbackQueryHandler(cb_admin_take, pattern=r"^admtake:"))
    app.add_handler(CallbackQueryHandler(cb_admin_cancel, pattern=r"^admcancel:"))
    app.add_handler(CallbackQueryHandler(cb_admin_ask_code, pattern=r"^askcode:"))
    app.add_handler(CallbackQueryHandler(cb_no_code, pattern=r"^nocode:"))
    app.add_handler(CallbackQueryHandler(cb_reg_exit, pattern=r"^regexit:(ok|bad):"))
    app.add_handler(CallbackQueryHandler(cb_user_exit, pattern=r"^userexit:"))
    app.add_handler(CallbackQueryHandler(cb_finalize, pattern=r"^final(ok|bad):"))

    app.add_handler(CallbackQueryHandler(cb_block_menu, pattern=r"^blockmenu:"))
    app.add_handler(CallbackQueryHandler(cb_block_pick, pattern=r"^blockpick:"))
    app.add_handler(CallbackQueryHandler(cb_block_confirm, pattern=r"^blockconfirm:"))
    app.add_handler(CallbackQueryHandler(cb_unblock, pattern=r"^unblock:"))

    app.add_handler(CallbackQueryHandler(cb_users_menu, pattern=r"^usersmenu:"))
    app.add_handler(CallbackQueryHandler(cb_admin_users, pattern=r"^admusers:"))
    app.add_handler(CallbackQueryHandler(cb_admin_user_profile, pattern=r"^admuser:"))
    app.add_handler(CallbackQueryHandler(cb_ban, pattern=r"^(ban|unban):"))
    app.add_handler(CallbackQueryHandler(cb_balance_manage, pattern=r"^(baladd|balsub):"))

    app.add_handler(CallbackQueryHandler(cb_price, pattern=r"^price:"))
    app.add_handler(CallbackQueryHandler(cb_work_toggle, pattern=r"^work:toggle"))
    app.add_handler(CallbackQueryHandler(cb_treasury_actions, pattern=r"^treasury:"))
    app.add_handler(CallbackQueryHandler(cb_mailing_cancel, pattern=r"^mailing:cancel$"))
    app.add_handler(CallbackQueryHandler(cb_admins_menu, pattern=r"^admins:"))

    app.add_handler(CallbackQueryHandler(cb_admin_vip, pattern=r"^admvip:"))
    app.add_handler(CallbackQueryHandler(cb_admin_vip_revoke, pattern=r"^admviprevoke:"))
    app.add_handler(CallbackQueryHandler(cb_admin_vip_revoke_confirm, pattern=r"^admviprevoke(ok|_cancel):"))
    app.add_handler(CallbackQueryHandler(cb_admin_vip_price, pattern=r"^admvipprice:"))

    app.add_handler(MessageHandler((filters.TEXT | filters.CONTACT) & ~filters.COMMAND, contact_or_phone), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_price_input), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_treasury_input), group=2)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_mailing_input), group=3)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_balance_manage_input), group=4)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_user_search_input), group=5)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_number_search_input), group=6)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_junior_add_input), group=7)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_junior_price_input), group=8)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_vip_add_input), group=9)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_vip_price_input), group=10)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_user_text), group=11)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_router), group=12)
    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("Bot started")
    application.run_polling()
