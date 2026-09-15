"""稽核紀錄 helper。

在各異動路由呼叫 audit(request, action, detail) 寫一筆 AuditLog;
用獨立 session、整段防護 —— 稽核寫入失敗絕不影響主流程。
在 event loop 內(middleware / async 路由)呼叫時,SQLite 寫入丟 executor 不阻塞 loop;
在執行緒 / 同步路由內則直接寫。
"""
import asyncio
import logging

from fastapi import Request

from app.database import SessionLocal
from app.models import AuditLog

logger = logging.getLogger("vcod.audit")


def client_ip(request: Request | None) -> str:
    """來源 IP(uvicorn 以 --proxy-headers 啟動時已還原 XFF)。"""
    try:
        return request.client.host if request and request.client else ""
    except Exception:  # noqa: BLE001
        return ""


def audit(request: Request | None, action: str, detail: str, user: str = "") -> None:
    """寫入稽核紀錄;失敗不影響主流程。"""
    try:
        if not user and request is not None:
            u = request.session.get("user") or {}
            user = u.get("name") or u.get("id") or ""
    except Exception:  # noqa: BLE001 —— 尚未掛 SessionMiddleware
        pass
    ip = client_ip(request)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        loop.run_in_executor(None, _write, user, ip, action, detail)
    else:
        _write(user, ip, action, detail)


def _write(user: str, ip: str, action: str, detail: str) -> None:
    try:
        with SessionLocal() as db:
            db.add(AuditLog(user=user, ip=ip, action=action, detail=detail[:2000]))
            db.commit()
    except Exception:  # noqa: BLE001 —— 稽核失敗只記 log,不影響主流程
        logger.exception("稽核寫入失敗:%s", action)
