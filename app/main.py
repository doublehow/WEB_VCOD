"""VCOD — vCenter Overview Dashboard 進入點。

啟動:  .\\.venv\\Scripts\\python.exe run.py(Web UI 埠預設 8082,
       由 config.json "web_port" / 環境變數 VCOD_WEB_PORT 調整)
"""
import asyncio
import hmac
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app import alerting, inventory
from app.audit import audit
from app.auth import ROLE_LABELS, admin_password_problem, roles_for_session
from app.config import (INITIAL_ADMIN_PW_FILE, ensure_session_secret, local_now,
                        migrate_plaintext_secrets, save_settings, settings)
from app.database import init_db
from app.inventory import dashboard_stats, object_status
from app.routes import alerts, auth, logs_routes, settings_routes, vcenters, views
from app.webutil import render

# 免登入路徑
PUBLIC_PATHS = {"/login", "/favicon.ico"}

# 統一 log 格式:[2026-09-14 13:10:21,784: INFO/vcod.poller] 訊息
_LOG_FORMAT = "[%(asctime)s: %(levelname)s/%(name)s] %(message)s"


def _setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    fmt = logging.Formatter(_LOG_FORMAT)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            h.setFormatter(fmt)


_setup_logging()
migrate_plaintext_secrets()  # config.json 既有明文 secret 就地改寫密文(冪等)


def _bootstrap_local_admin(log: logging.Logger) -> None:
    """本機 admin 密碼開機檢查:
    - 空且 AD 未啟用(否則無任何登入方式)→ 產生隨機初始密碼,密文存 config.json、明文寫
      data/initial_admin_password.txt(僅擁有者可讀),並標 local_admin_initial;首次登入強制變更後自動刪檔。
    - 仍為舊版預設 admin 或不符規則 → 不拒絕啟動(否則無法進 UI 改),改由登入後強制變更。"""
    if not settings.local_admin_password and not settings.ad_enabled:
        pw = secrets.token_urlsafe(12)
        save_settings({"local_admin_password": pw, "local_admin_initial": True})
        try:
            INITIAL_ADMIN_PW_FILE.parent.mkdir(exist_ok=True)
            fd = os.open(INITIAL_ADMIN_PW_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(pw + "\n")
            log.critical("本機 admin 尚未設定密碼,已產生初始密碼並寫入 %s;"
                         "請以 admin 登入後立即變更(變更後該檔自動刪除)", INITIAL_ADMIN_PW_FILE)
        except OSError as exc:
            log.critical("初始密碼檔寫入失敗(%s),初始密碼:%s —— 請以 admin 登入後立即變更", exc, pw)
    elif settings.local_admin_password and admin_password_problem(settings.local_admin_password):
        log.critical("本機 admin 密碼不符現行規則(如舊版預設 admin),登入後將強制變更")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log = logging.getLogger("vcod.main")
    _bootstrap_local_admin(log)
    alerting.load_state()   # 進行中警示讀回,重啟不重發
    from app import poller
    task = asyncio.create_task(poller.poller_loop())
    yield
    task.cancel()
    poller.shutdown()


app = FastAPI(title="VCOD — vCenter Overview Dashboard", lifespan=lifespan)
app.include_router(auth.router)
app.include_router(views.router)
app.include_router(alerts.router)
app.include_router(vcenters.router)
app.include_router(logs_routes.router)
app.include_router(settings_routes.router)


_STATIC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}
if settings.behind_tls:   # 前端 TLS 終結時才送 HSTS(純 HTTP 送了會讓瀏覽器拒連)
    _STATIC_HEADERS["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def _csp(nonce: str) -> str:
    """全站零 CDN。script 只允許本站檔案與帶本請求 nonce 的內嵌 <script>(模板一律
    <script nonce="{{ csp_nonce }}">,不用 onclick= 等內嵌事件屬性與 javascript: URL);
    樣式因大量 style= 屬性仍允許 inline。"""
    return (f"default-src 'self'; script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'self'")


def _secured(resp: Response, nonce: str) -> Response:
    resp.headers.setdefault("Content-Security-Policy", _csp(nonce))
    for k, v in _STATIC_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


def _wants_json(request: Request) -> bool:
    return "application/json" in (request.headers.get("accept") or "")


def _deny(request: Request, msg: str, status: int = 403) -> Response:
    nonce = request.state.csp_nonce
    if _wants_json(request):
        return _secured(JSONResponse({"ok": False, "error": msg}, status_code=status), nonce)
    return _secured(HTMLResponse(
        f'<div style="font-family:sans-serif;padding:40px;max-width:600px">'
        f'<h2>{status} 權限不足</h2><p>{msg}</p>'
        f'<p><a href="/">← 回首頁</a></p></div>',
        status_code=status), nonce)


@app.middleware("http")
async def require_login(request: Request, call_next):
    """未登入導向 /login;唯讀角色擋非 GET;非 GET 需 CSRF token;統一加安全標頭。

    - 角色不信任 cookie 內的值:每請求以登入帳號重查分權表(10 秒 TTL 快取),
      撤權最晚 10 秒內生效。
    - CSRF:登入後發 session-bound token,表單以 _csrf 欄位、AJAX 以
      X-CSRF-Token 標頭帶回,非 GET 以 compare_digest 驗證。
    - 每請求產生 CSP nonce(request.state.csp_nonce)供模板 <script nonce>;角色解析結果
      放 request.state.roles,路由 / 模板不再各自查表。
    - session user 帶 must_change_pw 時只放行 /settings 與 /logout(強制改本機 admin 密碼)。
    """
    nonce = secrets.token_urlsafe(16)
    request.state.csp_nonce = nonce
    path = request.url.path
    if path in PUBLIC_PATHS:
        return _secured(await call_next(request), nonce)
    user = request.session.get("user")
    if not user:
        if _wants_json(request):
            return _secured(JSONResponse({"ok": False, "error": "未登入"}, status_code=401), nonce)
        return _secured(RedirectResponse("/login", status_code=303), nonce)
    if not request.session.get("csrf"):
        request.session["csrf"] = secrets.token_urlsafe(32)
    # 本機 admin 以初始 / 不合規密碼登入 → 只能到設定頁改密碼或登出
    if user.get("must_change_pw") and path not in ("/settings", "/logout"):
        if request.method in ("GET", "HEAD") and not _wants_json(request):
            return _secured(RedirectResponse("/settings?error=請先變更本機 admin 密碼,再使用其他功能",
                                             status_code=303), nonce)
        return _deny(request, "本機 admin 須先變更密碼(設定頁),才能執行其他操作。")
    # 角色每請求解析一次(分權表查詢在執行緒內,10 秒 TTL 快取),存 request.state 供模板讀取
    roles = set(await asyncio.to_thread(roles_for_session, user))
    request.state.roles = sorted(roles)
    if request.method not in ("GET", "HEAD"):
        if "full_admin" not in roles and path != "/logout":
            label = "、".join(ROLE_LABELS.get(r, r) for r in roles) or "唯讀"
            audit(request, "access_denied", f"{request.method} {path}(角色 {label})")
            return _deny(request, f"此帳號為{label}權限,不可執行變更操作。")
        # 先 body() 讓 Starlette 快取原始 body 供下游路由重放
        await request.body()
        form = await request.form()
        token = str(form.get("_csrf") or request.headers.get("X-CSRF-Token") or "")
        if not hmac.compare_digest(token, request.session["csrf"]):
            audit(request, "csrf_rejected", f"{request.method} {path}")
            return _deny(request, "表單驗證失敗(CSRF token 不符或已過期),請重新整理頁面後再試。")
    return _secured(await call_next(request), nonce)


# SessionMiddleware 後加 → 最外層 → 先執行,讓 require_login 內能讀 request.session。
# session_cookie 取專屬名稱:cookie 只認主機不分埠,同機其他 starlette 系統若都用
# 預設名 "session" 會互相覆蓋。max_age 8 小時:簽章 cookie 無伺服器端狀態,
# 靠較短絕對有效期收斂外流風險。
app.add_middleware(SessionMiddleware, secret_key=ensure_session_secret(),
                   same_site="lax", session_cookie="vcod_session",
                   https_only=settings.behind_tls, max_age=8 * 3600)
# gzip 最外層:面板片段(VM 總覽可達數 MB)壓縮後約 4–7%,每個分頁每輪自動更新都受惠
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/api/status")
async def status_api():
    """頂欄指示器 15 秒輪詢:vCenter 連線數與最後更新時間。"""
    snap = inventory.current()
    enabled = [v for v in snap.vcs.values() if v.enabled]
    ok = [v for v in enabled if v.status == "ok" and not v.stale]
    return {
        "vc_total": len(enabled), "vc_ok": len(ok),
        "polled_at": snap.polled_at.isoformat() if snap.polled_at else None,
        "round_ms": snap.round_ms,
        "poll_interval": settings.poll_interval_seconds,
    }


def _donut(parts: list[tuple[str, int, str]]) -> dict | None:
    """(標籤, 數量, CSS 變數名) → conic-gradient 甜甜圈;總數 0 回 None。"""
    total = sum(c for _, c, _ in parts)
    if total == 0:
        return None
    stops, legend, acc = [], [], 0
    for label, count, var in parts:
        if count == 0:
            continue
        start = acc / total * 100
        acc += count
        end = acc / total * 100
        stops.append(f"var({var}) {start:.2f}% {end:.2f}%")
        legend.append({"label": label, "count": count,
                       "pct": round(count / total * 100), "var": var})
    return {"gradient": ", ".join(stops), "legend": legend, "total": total}


@app.get("/")
async def dashboard(request: Request):
    snap = inventory.current()
    s = dashboard_stats(snap)
    # 進行中警示(引擎狀態,已去抖 / 排除 / 抑制):嚴重在前、久的在前
    active = [dict(a, obj_key=k) for k, lst in alerting.active_index().items() for a in lst]
    active.sort(key=lambda a: (a["level"] != "critical", a["first_at"] or local_now()))
    n_crit = sum(1 for a in active if a["level"] == "critical")
    donut_power = _donut([
        ("開機", s["vm_on"], "--allow"),
        ("關機", s["vm_off"], "--text3"),
        ("暫停", s["vm_suspended"], "--warn"),
    ])
    donut_vc = _donut([
        ("已連線", s["vc_ok"], "--allow"),
        ("失敗 / 過期", s["vc_total"] - s["vc_ok"], "--deny"),
    ])
    return render(request, "dashboard.html", "dashboard", s=s,
                  donut_power=donut_power, donut_vc=donut_vc,
                  alerts_active=active[:50], active_total=len(active), n_crit=n_crit,
                  n_warn=len(active) - n_crit, suppression=alerting.last_result().suppression,
                  obj_status=object_status(snap), now=local_now())
