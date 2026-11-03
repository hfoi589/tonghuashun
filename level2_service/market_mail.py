"""SMTP configuration and delivery for Market registration and password reset codes."""

from __future__ import annotations

import json
import re
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Any, Callable

from .market_accounts import EMAIL_PATTERN, SQLiteMarketAccountStore

SETTING_KEY = "smtp_config"
_HOST_PATTERN = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")

DEFAULT_CONFIG: dict[str, Any] = {
    "host": "smtp.exmail.qq.com",
    "port": 465,
    "ssl": True,
    "user": "",
    "password": "",
    "sender": "",
    "registration_enabled": False,
}


class MailError(RuntimeError):
    """Delivery failed; the message is safe to show to administrators."""


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    ssl: bool
    user: str
    password: str
    sender: str
    registration_enabled: bool

    @property
    def ready(self) -> bool:
        return bool(self.host and self.port and self.user and self.password and self.sender)


def _clean_line(value: Any, limit: int) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) > limit or any(ch in text for ch in "\r\n\x00"):
        raise ValueError("配置项包含非法字符或过长")
    return text


def _check_address(value: str, label: str) -> str:
    if not EMAIL_PATTERN.fullmatch(value):
        raise ValueError(f"{label}邮箱格式不正确")
    return value


class SmtpSettings:
    """Persisted SMTP settings; the password is never returned by public methods."""

    def __init__(self, accounts: SQLiteMarketAccountStore) -> None:
        self.accounts = accounts

    def load(self) -> SmtpConfig:
        raw = self.accounts.get_setting(SETTING_KEY)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        merged = {**DEFAULT_CONFIG, **(data if isinstance(data, dict) else {})}
        return SmtpConfig(
            host=str(merged["host"]),
            port=int(merged["port"]),
            ssl=bool(merged["ssl"]),
            user=str(merged["user"]),
            password=str(merged["password"]),
            sender=str(merged["sender"]),
            registration_enabled=bool(merged["registration_enabled"]),
        )

    def public(self) -> dict[str, Any]:
        config = self.load()
        return {
            "host": config.host,
            "port": config.port,
            "ssl": config.ssl,
            "user": config.user,
            "password_configured": bool(config.password),
            "sender": config.sender,
            "registration_enabled": config.registration_enabled,
            "ready": config.ready,
        }

    def save(self, incoming: dict[str, Any]) -> dict[str, Any]:
        current = self.load()
        host = _clean_line(incoming.get("host", current.host), 253)
        if host and not _HOST_PATTERN.fullmatch(host):
            raise ValueError("SMTP Host 格式不正确")
        try:
            port = int(incoming.get("port", current.port))
        except (TypeError, ValueError):
            raise ValueError("SMTP 端口必须是数字") from None
        if not 1 <= port <= 65535:
            raise ValueError("SMTP 端口范围为 1-65535")
        user = _clean_line(incoming.get("user", current.user), 254)
        sender = _clean_line(incoming.get("sender", current.sender), 254)
        if sender:
            _check_address(sender, "发件人")
        password = incoming.get("password")
        password = current.password if password in (None, "") else _clean_line(password, 256)
        data = {
            "host": host,
            "port": port,
            "ssl": bool(incoming.get("ssl", current.ssl)),
            "user": user,
            "password": password,
            "sender": sender,
            "registration_enabled": bool(
                incoming.get("registration_enabled", current.registration_enabled)
            ),
        }
        self.accounts.set_setting(SETTING_KEY, json.dumps(data, ensure_ascii=False))
        return self.public()


class MailSender:
    def __init__(
        self,
        *,
        smtp_ssl_factory: Callable[..., Any] = smtplib.SMTP_SSL,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        timeout_seconds: float = 15.0,
    ) -> None:
        self.smtp_ssl_factory = smtp_ssl_factory
        self.smtp_factory = smtp_factory
        self.timeout_seconds = timeout_seconds

    def send(self, config: SmtpConfig, to: str, subject: str, body: str) -> None:
        if not config.ready:
            raise MailError("SMTP 尚未配置完整")
        recipient = _check_address(to.strip().lower(), "收件人")
        message = EmailMessage()
        message["From"] = formataddr(("THS Market", parseaddr(config.sender)[1] or config.sender))
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        try:
            if config.ssl:
                client = self.smtp_ssl_factory(
                    config.host,
                    config.port,
                    timeout=self.timeout_seconds,
                    context=ssl.create_default_context(),
                )
            else:
                client = self.smtp_factory(config.host, config.port, timeout=self.timeout_seconds)
            with client:
                if not config.ssl:
                    client.starttls(context=ssl.create_default_context())
                client.login(config.user, config.password)
                client.send_message(message)
        except smtplib.SMTPAuthenticationError:
            raise MailError("SMTP 登录失败，请检查用户名和密码（授权码）") from None
        except smtplib.SMTPRecipientsRefused:
            raise MailError("收件人被邮件服务器拒绝") from None
        except (smtplib.SMTPException, OSError):
            raise MailError("SMTP 发送失败，请检查 Host、端口和 SSL/TLS 设置") from None

    def send_code(self, config: SmtpConfig, to: str, purpose: str, code: str) -> None:
        action = "注册账号" if purpose == "register" else "重置密码"
        self.send(
            config,
            to,
            f"THS Market {action}验证码",
            f"你正在{action}。验证码：{code}\n\n10 分钟内有效，请勿告诉他人。如非本人操作请忽略本邮件。",
        )
