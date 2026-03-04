from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.auth import LogOutRequest, ResetAuthorizationsRequest


@dataclass
class PendingAuth:
    phone: str
    phone_code_hash: str
    mode: str


class TelegramAuthService:
    def __init__(self, api_id: int, api_hash: str, sessions_dir: str) -> None:
        self.api_id = api_id
        self.api_hash = api_hash
        self.sessions_dir = Path(sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, phone: str) -> str:
        normalized = phone.replace("+", "").replace(" ", "")
        return str(self.sessions_dir / f"{normalized}.session")

    def build_client(self, phone: str) -> TelegramClient:
        return TelegramClient(self._session_path(phone), self.api_id, self.api_hash)

    async def request_login_code(self, phone: str, mode: str) -> PendingAuth:
        client = self.build_client(phone)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            return PendingAuth(phone=phone, phone_code_hash=sent.phone_code_hash, mode=mode)
        finally:
            await client.disconnect()

    async def resend_login_code(self, pending: PendingAuth) -> PendingAuth:
        client = self.build_client(pending.phone)
        await client.connect()
        try:
            sent = await client.send_code_request(pending.phone, force_sms=True)
            return PendingAuth(
                phone=pending.phone,
                phone_code_hash=sent.phone_code_hash,
                mode=pending.mode,
            )
        finally:
            await client.disconnect()

    async def confirm_login_code(
        self,
        pending: PendingAuth,
        code: str,
        password_2fa: str | None = None,
    ) -> tuple[bool, str]:
        client = self.build_client(pending.phone)
        await client.connect()
        try:
            try:
                await client.sign_in(
                    phone=pending.phone,
                    code=code,
                    phone_code_hash=pending.phone_code_hash,
                )
            except SessionPasswordNeededError:
                if not password_2fa:
                    return False, "Нужен пароль 2FA. Отправьте его отдельным сообщением."
                await client.sign_in(password=password_2fa)

            await client(ResetAuthorizationsRequest())
            me = await client.get_me()
            return True, f"Успешно авторизовано: {me.first_name or ''} {me.last_name or ''}".strip()
        except Exception as exc:  # noqa: BLE001
            return False, f"Ошибка авторизации: {exc}"
        finally:
            await client.disconnect()

    async def logout_account(self, phone: str) -> tuple[bool, str]:
        client = self.build_client(phone)
        await client.connect()
        try:
            await client(LogOutRequest())
            return True, "Аккаунт вышел из текущей сессии."
        except Exception as exc:  # noqa: BLE001
            return False, f"Ошибка выхода: {exc}"
        finally:
            await client.disconnect()

    async def latest_login_code_message(self, phone: str) -> str:
        client = self.build_client(phone)
        await client.connect()
        try:
            async for message in client.iter_messages(777000, limit=10):
                if message and message.raw_text:
                    return message.raw_text
            return "Сообщений с кодом не найдено."
        except Exception as exc:  # noqa: BLE001
            return f"Не удалось получить код: {exc}"
        finally:
            await client.disconnect()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
