"""設定頁:AD 登入 / 本機管理員 / 輪詢與保留 / 通知管道 / 帳號分權;含 AD 逐步測試與通知測試(AJAX)。"""
import asyncio
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.audit import audit
from app.auth import (ADMIN_PASSWORD_MIN_LEN, ROLE_LABELS, admin_password_problem,
                      set_local_admin_password)
from app.config import POLL_MAX, POLL_MIN, clamp_int, save_settings, settings
from app.database import get_db
from app.models import AccountRole
from app.webutil import render

router = APIRouter()


@router.get("/settings")
def settings_page(request: Request, db: Session = Depends(get_db), saved: str = "", error: str = ""):
    roles = db.query(AccountRole).order_by(AccountRole.username).all()
    return render(request, "settings.html", "settings", s=settings, saved=saved, error=error,
                  admin_pw_min=ADMIN_PASSWORD_MIN_LEN,
                  roles=roles, role_labels=ROLE_LABELS,
                  poll_min=POLL_MIN, poll_max=POLL_MAX)


@router.post("/settings")
async def settings_save(
    request: Request,
    ad_enabled: str = Form(""),
    ad_domain: str = Form(""),
    ad_servers: str = Form(""),
    ad_service_user: str = Form(""),
    ad_service_password: str = Form(""),
    ad_allowed_group: str = Form(""),
    ad_base_dn: str = Form(""),
    ad_use_ssl: str = Form(""),
    local_admin_password: str = Form(""),
    default_role: str = Form("readonly"),
    poll_interval_seconds: str = Form("30"),
    log_retention_days: str = Form("365"),
    telegram_bot_token: str = Form(""),
    telegram_chat_id: str = Form(""),
    smtp_host: str = Form(""),
    smtp_port: str = Form("25"),
    smtp_tls: str = Form(""),
    smtp_user: str = Form(""),
    smtp_password: str = Form(""),
    smtp_from: str = Form(""),
    smtp_to: str = Form(""),
):
    if local_admin_password:
        problem = admin_password_problem(local_admin_password)
        if problem:
            audit(request, "settings_rejected", f"本機 admin 密碼不符要求:{problem}")
            return RedirectResponse(f"/settings?error={problem},其餘欄位未儲存", status_code=303)
    updates = {
        "ad_enabled": bool(ad_enabled),
        "ad_domain": ad_domain.strip(),
        "ad_servers": [s.strip() for s in ad_servers.replace("，", ",").replace("\n", ",").split(",")
                       if s.strip()],
        "ad_service_user": ad_service_user.strip(),
        "ad_allowed_group": ad_allowed_group.strip(),
        "ad_base_dn": ad_base_dn.strip(),
        "ad_use_ssl": bool(ad_use_ssl),
        "default_role": default_role if default_role in ROLE_LABELS else "readonly",
        "poll_interval_seconds": clamp_int(poll_interval_seconds, 30, POLL_MIN, POLL_MAX),
        "log_retention_days": clamp_int(log_retention_days, 365, 0, 3650),
        "telegram_chat_id": telegram_chat_id.strip(),
        "smtp_host": smtp_host.strip(),
        "smtp_port": clamp_int(smtp_port, 25, 1, 65535),
        "smtp_tls": bool(smtp_tls),
        "smtp_user": smtp_user.strip(),
        "smtp_from": smtp_from.strip(),
        "smtp_to": smtp_to.strip(),
    }
    # 密碼 / Token 類欄位:留空 = 不變更
    for field, value in (("ad_service_password", ad_service_password),
                         ("smtp_password", smtp_password),
                         ("telegram_bot_token", telegram_bot_token.strip())):
        if value:
            updates[field] = value
    save_settings(updates)
    if local_admin_password:
        set_local_admin_password(local_admin_password)   # 密文落地、清初始旗標、刪初始密碼檔
        u = request.session.get("user") or {}
        if u.get("must_change_pw"):
            request.session["user"] = {**u, "must_change_pw": False}
    audit(request, "settings_save", "更新全域設定" + ("(含本機 admin 密碼)" if local_admin_password else ""))
    from app import poller
    poller.request_poll()
    return RedirectResponse("/settings?saved=1", status_code=303)


@router.post("/settings/notify-test")
async def notify_test(request: Request):
    """AJAX:依已儲存設定對 Telegram / SMTP 各發一則測試通知。"""
    from app import notify
    ok, message = await asyncio.to_thread(notify.send_test)
    audit(request, "notify_test", f"通知測試:{message}")
    return {"ok": ok, "message": message}


@router.post("/settings/ad-test")
async def ad_test(request: Request):
    """AJAX:以表單填入的 AD 設定逐步測試,不需先儲存。密碼欄留空時退回已儲存值。"""
    try:
        data = json.loads(await request.body())
    except Exception:  # noqa: BLE001
        return {"ok": False, "steps": [], "message": "無效的請求格式"}
    return await asyncio.to_thread(_ad_test_run, data)


def _ad_test_run(data: dict) -> dict:
    """(同步,於 thread 內執行)AD 連線逐步測試主體。"""
    from ldap3 import ALL, NTLM, SUBTREE, Connection, Server
    from ldap3.core.exceptions import LDAPBindError, LDAPException
    from app.auth import group_match  # noqa: F401 —— 亦觸發 MD4 修補

    server_ips_raw = str(data.get("ad_servers", "")).strip()
    domain = str(data.get("ad_domain", "")).strip()
    svc_account = str(data.get("ad_service_user", "")).strip()
    svc_password = str(data.get("ad_service_password", "")).strip() or settings.ad_service_password
    allowed_group = str(data.get("ad_allowed_group", "")).strip()
    base_dn = str(data.get("ad_base_dn", "")).strip()
    use_ssl = bool(data.get("ad_use_ssl"))
    test_user = str(data.get("test_username", "")).strip()
    test_pass = str(data.get("test_password", ""))

    steps: list[dict] = []
    if not server_ips_raw or not domain:
        return {"ok": False, "steps": steps, "message": "AD Server IP 或 Domain 未填寫"}
    servers = [s.strip() for s in server_ips_raw.replace("，", ",").replace("\n", ",").split(",")
               if s.strip()]

    # 防憑證外送:密碼欄留空 = 沿用已存 service 密碼,此時目標必須是已儲存的 AD 伺服器
    if not str(data.get("ad_service_password", "")).strip() and settings.ad_service_password:
        rogue = [ip for ip in servers if ip not in set(settings.ad_servers)]
        if rogue:
            audit(None, "ad_test_blocked", f"AD 測試遭拒:沿用已存密碼但目標 {rogue} 不在已儲存清單")
            return {"ok": False, "steps": steps, "message":
                    "使用已儲存的 Service 密碼時,Server 僅限已儲存的 AD 伺服器"
                    f"({', '.join(settings.ad_servers) or '未設定'});測試其他伺服器請一併輸入密碼"}

    steps.append({"label": "設定解析", "ok": True, "detail": f"Server: {servers},Domain: {domain}"})

    svc_bind_ok = False
    for ip in servers:
        try:
            ldap_server = Server(ip, get_info=ALL, connect_timeout=5, use_ssl=use_ssl)
            if svc_account and svc_password:
                try:
                    conn = Connection(ldap_server, user=f"{domain}\\{svc_account}",
                                      password=svc_password, authentication=NTLM,
                                      auto_bind=True, receive_timeout=8)
                    conn.unbind()
                    steps.append({"label": f"Service Account Bind ({ip})", "ok": True,
                                  "detail": f"{domain}\\{svc_account} bind 成功"})
                    svc_bind_ok = True
                    break
                except LDAPBindError as exc:
                    steps.append({"label": f"Service Account Bind ({ip})", "ok": False,
                                  "detail": f"bind 失敗:{exc}"})
                except LDAPException as exc:
                    steps.append({"label": f"Service Account Bind ({ip})", "ok": False,
                                  "detail": f"LDAP 例外:{exc}"})
            else:
                try:
                    conn = Connection(ldap_server, receive_timeout=5)
                    conn.open()
                    conn.unbind()
                    steps.append({"label": f"Server 連通 ({ip})", "ok": True,
                                  "detail": "Server 可達(未使用 Service Account)"})
                    svc_bind_ok = True
                    break
                except Exception as exc:  # noqa: BLE001
                    steps.append({"label": f"Server 連通 ({ip})", "ok": False, "detail": f"連線失敗:{exc}"})
        except Exception as exc:  # noqa: BLE001
            steps.append({"label": f"Server 連接 ({ip})", "ok": False, "detail": str(exc)})

    if test_user and test_pass:
        for ip in servers:
            try:
                ldap_server = Server(ip, get_info=ALL, connect_timeout=5, use_ssl=use_ssl)
                try:
                    conn = Connection(ldap_server, user=f"{domain}\\{test_user}",
                                      password=test_pass, authentication=NTLM,
                                      auto_bind=True, receive_timeout=8)
                    conn.unbind()
                    steps.append({"label": f"使用者認證 ({ip})", "ok": True,
                                  "detail": f"{test_user} 認證成功"})
                    if allowed_group and base_dn and svc_account and svc_password:
                        conn2 = Connection(ldap_server, user=f"{domain}\\{svc_account}",
                                           password=svc_password, authentication=NTLM,
                                           auto_bind=True, receive_timeout=8)
                        from ldap3.utils.conv import escape_filter_chars
                        conn2.search(search_base=base_dn,
                                     search_filter=f"(sAMAccountName={escape_filter_chars(test_user)})",
                                     search_scope=SUBTREE, attributes=["memberOf"])
                        if not conn2.entries:
                            steps.append({"label": "群組驗證", "ok": False,
                                          "detail": f"在 Base DN 找不到使用者 {test_user}"})
                        else:
                            try:
                                member_of = list(conn2.entries[0].memberOf) or []
                            except Exception:  # noqa: BLE001
                                member_of = []
                            match = group_match(member_of, allowed_group)
                            steps.append({"label": "群組驗證", "ok": match,
                                          "detail": (f"共 {len(member_of)} 個群組,"
                                                     f"{'包含' if match else '不包含'} '{allowed_group}'")})
                        conn2.unbind()
                    break
                except LDAPBindError as exc:
                    steps.append({"label": f"使用者認證 ({ip})", "ok": False,
                                  "detail": f"帳號或密碼錯誤:{exc}"})
                    break
                except LDAPException as exc:
                    steps.append({"label": f"使用者認證 ({ip})", "ok": False, "detail": f"LDAP 例外:{exc}"})
            except Exception as exc:  # noqa: BLE001
                steps.append({"label": f"使用者認證 ({ip})", "ok": False, "detail": str(exc)})
    else:
        steps.append({"label": "使用者認證", "ok": None, "detail": "未提供測試帳號密碼,跳過"})

    return {"ok": svc_bind_ok, "steps": steps,
            "message": "測試完成" if svc_bind_ok else "部分測試失敗,請查看詳情"}
