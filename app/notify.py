"""警示通知(Telegram + SMTP;移植自 WEB_BaselineGuard notify.py)。

- send():對所有已設定管道送出;任何管道失敗都不拋出,回傳結果字串由呼叫端記錄。
- 同步阻塞實作,呼叫端(poller)在執行緒內呼叫;設定頁測試按鈕走 send_test()。
- SMTP:埠 465 走 SMTPS(隱含 TLS),其他埠依 smtp_tls 勾 STARTTLS;
  smtp_user 留空 = 不做 SMTP AUTH(內部 relay 常見)。
- Telegram:單則上限 4096 字,超過截斷;防火牆 SSL inspection 造成憑證驗證失敗時,
  可在 config.json 設 telegram_skip_tls_verify=true(僅影響 Telegram 請求,訊息不含密碼)。
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from email.utils import formatdate

from app.config import settings

logger = logging.getLogger("vcod.notify")
TIMEOUT = 15.0


def channels_configured() -> bool:
    return bool((settings.telegram_bot_token and settings.telegram_chat_id)
                or (settings.smtp_host and settings.smtp_to))


def send(subject: str, text: str) -> str:
    """對所有已設定管道送出;回傳各管道結果彙總(不拋例外)。"""
    results = _send(subject, text)
    return ";".join(results) if results else "未設定任何通知管道"


def send_test() -> tuple[bool, str]:
    """設定頁的測試通知:對已設定的管道各發一則。"""
    if not channels_configured():
        return False, "未設定任何通知管道(Telegram / SMTP 收件人),請先儲存設定"
    results = _send("VCOD 通知測試",
                    "🔔 這是 VCOD 設定頁發出的測試通知 — 看到這則代表管道正常,"
                    "vCenter / 主機 / 儲存區 / VM 的警示將送達此處。")
    ok = not any("失敗" in r for r in results)
    return ok, ";".join(results)


def _tls_context() -> ssl.SSLContext:
    if settings.telegram_skip_tls_verify:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return ctx


def _send(subject: str, text: str) -> list[str]:
    results: list[str] = []

    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
                data=json.dumps({"chat_id": settings.telegram_chat_id,
                                 "text": f"{subject}\n{text}"[:4000]},
                                ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=TIMEOUT, context=_tls_context()) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            if not body.get("ok"):
                raise RuntimeError(body.get("description", "未知錯誤"))
            results.append("Telegram 已送出")
        except Exception as exc:  # noqa: BLE001
            results.append(f"Telegram 失敗:{exc}")

    if settings.smtp_host and settings.smtp_to:
        tos = [t.strip() for t in settings.smtp_to.replace("，", ",").split(",") if t.strip()]
        results.append(_smtp_send(subject, text, tos))

    return results


def _smtp_send(subject: str, text: str, tos: list[str]) -> str:
    try:
        sender = settings.smtp_from or settings.smtp_user or f"vcod@{settings.smtp_host}"
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender
        msg["To"] = ", ".join(tos)
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(text)
        if settings.smtp_port == 465:
            smtp = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT)
        else:
            smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=TIMEOUT)
        with smtp:
            if settings.smtp_tls and settings.smtp_port != 465:
                smtp.starttls()
            if settings.smtp_user:
                smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
        return f"郵件已送出({', '.join(tos)})"
    except Exception as exc:  # noqa: BLE001
        return f"郵件失敗:{exc}"
