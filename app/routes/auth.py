"""登入 / 登出 / 帳號分權(RBAC 管理)。"""
import asyncio
import secrets

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import login_guard
from app.audit import audit, client_ip
from app.auth import (ROLE_LABELS, authenticate_ad, invalidate_roles_cache,
                      local_admin_must_change, normalize_username, resolve_roles)
from app.config import settings
from app.database import get_db
from app.models import AccountRole
from app.webutil import templates

router = APIRouter()


def _login_ok(request: Request, user: dict, next_url: str = "/") -> RedirectResponse:
    request.session.clear()           # 防 session fixation
    request.session["user"] = user
    request.session["csrf"] = secrets.token_urlsafe(32)
    # RedirectResponse 會把非 ASCII 百分比編碼;勿直接塞 headers["location"](latin-1 會炸)
    return RedirectResponse(next_url, status_code=303)


@router.get("/login")
async def login_page(request: Request, error: str = ""):
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "ad_enabled": settings.ad_enabled,
                                "csp_nonce": getattr(request.state, "csp_nonce", "")})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...),
                       password: str = Form(...)):
    uname = username.strip()
    guard_user = normalize_username(uname).lower()
    ip = client_ip(request)
    wait = login_guard.locked_for(guard_user, ip)
    if wait:
        audit(request, "login_locked", f"登入遭節流拒絕:{uname}(剩餘 {wait} 秒)", user=guard_user)
        return RedirectResponse(f"/login?error=登入失敗次數過多,請 {-(-wait // 60)} 分鐘後再試",
                                status_code=303)

    # 本機管理帳號(緊急備援,不經 AD;session 標 local=True,roles_for_session 據此放行)。
    # 空值防護:secret.key 遺失時解密回空字串,不擋空值會 fail-open。
    # compare_digest 的 str 版只接受 ASCII,含中文密碼會 TypeError → 一律以 bytes 比對。
    if (uname == "admin" and settings.local_admin_password
            and secrets.compare_digest(password.encode("utf-8"),
                                       settings.local_admin_password.encode("utf-8"))):
        login_guard.reset(guard_user, ip)
        must_change = local_admin_must_change(password)
        audit(request, "login", "本機管理員登入(管理者)" + ("(初始 / 不合規密碼,強制變更)" if must_change else ""),
              user="admin")
        return _login_ok(request, {"id": "admin", "name": "本機管理員",
                                   "roles": ["full_admin"], "local": True,
                                   "must_change_pw": must_change},
                         next_url=("/settings?error=請先變更本機 admin 密碼(初始或不符規則)"
                                   if must_change else "/"))

    if settings.ad_enabled:
        # ldap3 為同步網路 I/O,丟執行緒避免 AD 無回應時卡死整個 event loop
        ok, result = await asyncio.to_thread(authenticate_ad, uname, password)
        if ok:
            login_guard.reset(guard_user, ip)
            roles = await asyncio.to_thread(resolve_roles, result.get("id", uname))
            result["roles"] = roles
            labels = "、".join(ROLE_LABELS.get(r, r) for r in roles)
            audit(request, "login", f"AD 登入:{result['id']}({labels})", user=result["id"])
            return _login_ok(request, result)
        login_guard.record_failure(guard_user, ip)
        # 回給使用者的訊息統一,避免枚舉帳號是否存在;詳細原因只進稽核紀錄
        audit(request, "login_failed", f"登入失敗:{uname}(原因:{result})",
              user=normalize_username(uname))
        return RedirectResponse("/login?error=帳號或密碼錯誤", status_code=303)

    login_guard.record_failure(guard_user, ip)
    audit(request, "login_failed", f"登入失敗:{uname}", user=uname)
    return RedirectResponse(
        "/login?error=帳號或密碼錯誤(AD 未啟用,請用本機 admin 帳號或至設定頁啟用 AD)",
        status_code=303)


@router.post("/logout")
async def logout(request: Request):
    """登出(POST + CSRF):避免 GET 被跨站觸發強制登出;唯讀角色一樣可登出。"""
    u = request.session.get("user") or {}
    who = u.get("name") or u.get("id") or ""
    request.session.clear()
    if who:
        audit(None, "logout", f"登出:{who}", user=who)
    return RedirectResponse("/login", status_code=303)


@router.get("/logout")
async def logout_get(request: Request):
    """舊書籤相容:GET 不清 session,導回首頁 / 登入頁。"""
    return RedirectResponse("/" if request.session.get("user") else "/login", status_code=303)


# ---------- 帳號分權(管理 UI 在設定頁;寫入由 middleware 強制僅 full_admin)----------
@router.post("/roles")
def roles_set(request: Request, db: Session = Depends(get_db),
              username: str = Form(...), role: str = Form("readonly")):
    uname = normalize_username(username)
    if uname and role in ROLE_LABELS:
        exists = db.query(AccountRole).filter(
            AccountRole.username == uname, AccountRole.role == role).first()
        if exists is None:
            db.add(AccountRole(username=uname, role=role))
            db.commit()
            invalidate_roles_cache()
            audit(request, "role_set", f"帳號分權:{uname} 新增角色 {ROLE_LABELS[role]}")
    return RedirectResponse("/settings#roles", status_code=303)


@router.post("/roles/{role_id}/delete")
def roles_delete(request: Request, role_id: int, db: Session = Depends(get_db)):
    r = db.get(AccountRole, role_id)
    if r:
        db.delete(r)
        db.commit()
        invalidate_roles_cache()
        audit(request, "role_delete", f"移除分權:{r.username}({ROLE_LABELS.get(r.role, r.role)})")
    return RedirectResponse("/settings#roles", status_code=303)
