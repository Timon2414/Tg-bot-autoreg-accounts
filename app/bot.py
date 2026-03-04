from __future__ import annotations

from dataclasses import dataclass

import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.config import Settings
from app.db import init_db
from app.telegram_service import PendingAuth, TelegramAuthService, now_iso


class FlowState(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_2fa = State()


@dataclass
class AppContext:
    settings: Settings
    auth: TelegramAuthService


MAIN_BUTTONS = {
    "add": "Добавить номер",
    "numbers": "Номера",
    "stats": "Статистика",
}


async def _is_admin(message: Message | CallbackQuery, settings: Settings) -> bool:
    user_id = message.from_user.id if message.from_user else 0
    return user_id == settings.admin_id


async def _upsert_account(db_path: str, phone: str, mode: str, session_file: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """
            INSERT INTO accounts(phone, mode, status, created_at, last_login_at, session_file)
            VALUES(?, ?, 'active', ?, ?, ?)
            ON CONFLICT(phone) DO UPDATE SET
                mode=excluded.mode,
                status='active',
                last_login_at=excluded.last_login_at,
                session_file=excluded.session_file
            """,
            (phone, mode, now_iso(), now_iso(), session_file),
        )
        await db.execute(
            "INSERT INTO auth_events(phone, event_type, details, created_at) VALUES (?, ?, ?, ?)",
            (phone, "login_ok", f"mode={mode}", now_iso()),
        )
        await db.commit()


async def _set_failed_event(db_path: str, phone: str, details: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO auth_events(phone, event_type, details, created_at) VALUES (?, ?, ?, ?)",
            (phone, "login_error", details, now_iso()),
        )
        await db.commit()


def main_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text=MAIN_BUTTONS["add"], callback_data="menu:add")
    kb.button(text=MAIN_BUTTONS["numbers"], callback_data="menu:numbers")
    kb.button(text=MAIN_BUTTONS["stats"], callback_data="menu:stats")
    kb.adjust(1)
    return kb.as_markup()


def add_type_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="рег", callback_data="add:reg")
    kb.button(text="не рег", callback_data="add:noreg")
    kb.button(text="⬅️ Назад", callback_data="menu:back")
    kb.adjust(2, 1)
    return kb.as_markup()


async def create_dispatcher(ctx: AppContext) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    @dp.message(CommandStart())
    async def start(message: Message):
        if not await _is_admin(message, ctx.settings):
            await message.answer("Доступ запрещен.")
            return
        await message.answer("Админ-панель управления аккаунтами", reply_markup=main_keyboard())

    @dp.callback_query(F.data == "menu:back")
    async def menu_back(query: CallbackQuery):
        if not await _is_admin(query, ctx.settings):
            return
        await query.message.edit_text("Админ-панель управления аккаунтами", reply_markup=main_keyboard())
        await query.answer()

    @dp.callback_query(F.data == "menu:add")
    async def menu_add(query: CallbackQuery):
        if not await _is_admin(query, ctx.settings):
            return
        await query.message.edit_text("Выберите режим добавления номера:", reply_markup=add_type_keyboard())
        await query.answer()

    @dp.callback_query(F.data.startswith("add:"))
    async def add_phone_start(query: CallbackQuery, state: FSMContext):
        if not await _is_admin(query, ctx.settings):
            return
        mode = query.data.split(":", 1)[1]
        await state.update_data(mode=mode)
        await state.set_state(FlowState.waiting_phone)
        await query.message.answer("Отправьте номер в формате +79990000000")
        await query.answer()

    @dp.message(FlowState.waiting_phone)
    async def receive_phone(message: Message, state: FSMContext):
        if not await _is_admin(message, ctx.settings):
            return
        phone = (message.text or "").strip()
        data = await state.get_data()
        mode = data.get("mode", "reg")

        pending = await ctx.auth.request_login_code(phone, mode)
        await state.update_data(pending=pending.__dict__)
        await state.set_state(FlowState.waiting_code)

        if mode == "noreg":
            await message.answer(
                "Режим 'не рег': для официальной регистрации с email/tempmail.ninja нужен внешний GUI-автоматизатор Windows. "
                "Сейчас запрошен код входа через Telegram API. Отправьте код из Telegram/SMS."
            )
        else:
            await message.answer("Код отправлен. Ответьте сообщением с кодом.")

    @dp.message(FlowState.waiting_code)
    async def receive_code(message: Message, state: FSMContext):
        if not await _is_admin(message, ctx.settings):
            return
        code = (message.text or "").strip()
        data = await state.get_data()
        pending_dict = data.get("pending")
        if not pending_dict:
            await message.answer("Сессия истекла. Нажмите /start")
            await state.clear()
            return

        pending = PendingAuth(**pending_dict)
        ok, result = await ctx.auth.confirm_login_code(pending, code)
        if ok:
            await _upsert_account(
                ctx.settings.db_path,
                pending.phone,
                pending.mode,
                ctx.auth._session_path(pending.phone),
            )
            await message.answer(f"✅ {result}", reply_markup=main_keyboard())
            await state.clear()
        else:
            await _set_failed_event(ctx.settings.db_path, pending.phone, result)
            await state.update_data(last_code=code)
            if "2FA" in result:
                await state.set_state(FlowState.waiting_2fa)
            await message.answer(f"❌ {result}")

    @dp.message(FlowState.waiting_2fa)
    async def receive_2fa(message: Message, state: FSMContext):
        if not await _is_admin(message, ctx.settings):
            return
        password = (message.text or "").strip()
        data = await state.get_data()
        pending = PendingAuth(**data["pending"])
        code = data.get("last_code", "")
        ok, result = await ctx.auth.confirm_login_code(pending, code=code, password_2fa=password)
        if ok:
            await _upsert_account(
                ctx.settings.db_path,
                pending.phone,
                pending.mode,
                ctx.auth._session_path(pending.phone),
            )
            await message.answer(f"✅ {result}", reply_markup=main_keyboard())
            await state.clear()
        else:
            await _set_failed_event(ctx.settings.db_path, pending.phone, result)
            await message.answer(f"❌ {result}")

    @dp.callback_query(F.data == "menu:numbers")
    async def menu_numbers(query: CallbackQuery):
        if not await _is_admin(query, ctx.settings):
            return
        async with aiosqlite.connect(ctx.settings.db_path) as db:
            rows = await db.execute_fetchall(
                "SELECT phone FROM accounts WHERE status='active' ORDER BY id DESC"
            )
        if not rows:
            await query.message.edit_text("Активных номеров нет.", reply_markup=main_keyboard())
            await query.answer()
            return

        kb = InlineKeyboardBuilder()
        text_lines = ["Активные номера:"]
        for (phone,) in rows:
            text_lines.append(f"• {phone}")
            kb.button(text=phone, callback_data=f"acct:{phone}")
        kb.button(text="⬅️ Назад", callback_data="menu:back")
        kb.adjust(1)
        await query.message.edit_text("\n".join(text_lines), reply_markup=kb.as_markup())
        await query.answer()

    @dp.callback_query(F.data.startswith("acct:"))
    async def manage_account(query: CallbackQuery):
        if not await _is_admin(query, ctx.settings):
            return
        phone = query.data.split(":", 1)[1]
        kb = InlineKeyboardBuilder()
        kb.button(text="Выйти с аккаунта", callback_data=f"acct_logout:{phone}")
        kb.button(text="Получить код", callback_data=f"acct_code:{phone}")
        kb.button(text="⬅️ Назад", callback_data="menu:numbers")
        kb.adjust(1)
        await query.message.edit_text(f"Управление {phone}", reply_markup=kb.as_markup())
        await query.answer()

    @dp.callback_query(F.data.startswith("acct_logout:"))
    async def account_logout(query: CallbackQuery):
        if not await _is_admin(query, ctx.settings):
            return
        phone = query.data.split(":", 1)[1]
        ok, msg = await ctx.auth.logout_account(phone)
        if ok:
            async with aiosqlite.connect(ctx.settings.db_path) as db:
                await db.execute("UPDATE accounts SET status='deleted' WHERE phone=?", (phone,))
                await db.execute(
                    "INSERT INTO auth_events(phone, event_type, details, created_at) VALUES (?, ?, ?, ?)",
                    (phone, "logout", "manual", now_iso()),
                )
                await db.commit()
        await query.message.answer(("✅ " if ok else "❌ ") + msg)
        await query.answer()

    @dp.callback_query(F.data.startswith("acct_code:"))
    async def account_code(query: CallbackQuery):
        if not await _is_admin(query, ctx.settings):
            return
        phone = query.data.split(":", 1)[1]
        code_text = await ctx.auth.latest_login_code_message(phone)
        await query.message.answer(f"Последний код/сообщение:\n\n{code_text}")
        await query.answer()

    @dp.callback_query(F.data == "menu:stats")
    async def menu_stats(query: CallbackQuery):
        if not await _is_admin(query, ctx.settings):
            return
        async with aiosqlite.connect(ctx.settings.db_path) as db:
            total = (await db.execute_fetchone("SELECT COUNT(*) FROM accounts"))[0]
            active = (await db.execute_fetchone("SELECT COUNT(*) FROM accounts WHERE status='active'"))[0]
            failed = (await db.execute_fetchone("SELECT COUNT(*) FROM auth_events WHERE event_type='login_error'"))[0]
            last = await db.execute_fetchall(
                "SELECT phone, created_at, mode FROM accounts ORDER BY id DESC LIMIT 10"
            )
        lines = [
            "Статистика:",
            f"• Всего номеров: {total}",
            f"• Активных: {active}",
            f"• Ошибок авторизации: {failed}",
            "",
            "Последние 10:",
        ]
        lines.extend([f"• {p} | {m} | {c}" for p, c, m in last] or ["(пусто)"])
        await query.message.edit_text("\n".join(lines), reply_markup=main_keyboard())
        await query.answer()

    return dp


async def run_bot(settings: Settings) -> None:
    await init_db(settings.db_path)
    auth = TelegramAuthService(
        api_id=settings.telegram_api_id,
        api_hash=settings.telegram_api_hash,
        sessions_dir=settings.sessions_dir,
    )
    bot = Bot(token=settings.bot_token)
    dp = await create_dispatcher(AppContext(settings=settings, auth=auth))
    await dp.start_polling(bot)
