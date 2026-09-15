"""模板共用:templates 實例、導覽列、render 快捷(注入登入者/角色/CSRF)、
用量色階、CSV 匯出。"""
import csv
import io
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app.auth import ROLE_LABELS, roles_for_session
from app.config import settings

templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


def usage_tier(pct) -> str:
    """用量百分比 → 五段色階 class(對應 base.html 的 .pbar.u* / .tier-u*)。

    門檻唯一定義點:u1 綠 <20、u2 青 20-40、u3 黃 40-60、u4 橘 60-80、
    u5 紅 ≥80。模板一律用 |usage_tier,勿重寫門檻鏈。"""
    try:
        p = float(pct or 0)
    except (TypeError, ValueError):
        p = 0.0
    return ("u5" if p >= 80 else "u4" if p >= 60 else
            "u3" if p >= 40 else "u2" if p >= 20 else "u1")


def fmt_gb(value) -> str:
    """GB 數值 → 千分位、最多 1 位小數(TB 級也維持 GB 單位,便於比較)。"""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{v:,.1f}" if v < 100 else f"{v:,.0f}"


def fmt_ghz(mhz) -> str:
    try:
        return f"{float(mhz or 0) / 1000:.2f}"
    except (TypeError, ValueError):
        return "—"


templates.env.filters["usage_tier"] = usage_tier


def _alert_href(rule: str, target_type: str, obj_key: str) -> str:
    from app.alerting import alert_href   # 延遲匯入避免循環(alerting → inventory → …)
    return alert_href(rule, target_type, obj_key)


templates.env.globals["alert_href"] = _alert_href
templates.env.filters["gb"] = fmt_gb
templates.env.filters["ghz"] = fmt_ghz

NAV = [
    ("dashboard", "/", "儀表板"),
    ("compute", "/compute", "運算"),
    ("storage", "/storage", "儲存"),
    ("network", "/network", "網路"),
    ("vms", "/vms", "VM 總覽"),
    ("alerts", "/alerts", "警示"),
    ("vcenters", "/vcenters", "vCenter 管理"),
    ("logs", "/logs", "稽核紀錄"),
    ("settings", "/settings", "設定"),
]


def session_user(request: Request) -> dict:
    try:
        return request.session.get("user") or {}
    except Exception:  # noqa: BLE001
        return {}


def _roles(request: Request) -> set[str]:
    """middleware 每請求已在執行緒內解析角色並放 request.state.roles;缺少時(非經 middleware
    的呼叫)才退回同步解析。"""
    roles = getattr(request.state, "roles", None)
    if roles is None:
        roles = roles_for_session(session_user(request))
    return set(roles)


def role_flags(request: Request) -> dict:
    """角色權限旗標(僅供模板顯示;伺服器端於 middleware 強制)。"""
    roles = _roles(request)
    return {"can_write": "full_admin" in roles}


def _csrf(request: Request) -> str:
    try:
        return request.session.get("csrf") or ""
    except Exception:  # noqa: BLE001
        return ""


def render(request: Request, name: str, active: str, **ctx):
    user = session_user(request)
    roles = _roles(request)
    label = "、".join(ROLE_LABELS.get(r, r) for r in ROLE_LABELS if r in roles)
    return templates.TemplateResponse(request, name, {
        "nav": NAV, "active": active,
        "user_name": user.get("name", ""), "user_id": user.get("id", ""),
        "must_change_pw": bool(user.get("must_change_pw")),
        "role_label": label or "唯讀",
        "csrf_token": _csrf(request),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
        "poll_interval": settings.poll_interval_seconds,
        **role_flags(request),
        **ctx})


def render_partial(request: Request, name: str, **ctx) -> HTMLResponse:
    """只渲染面板片段(自動更新時 fetch 替換 #panel 內容)。"""
    return templates.TemplateResponse(request, name, ctx)


def like_escape(q: str) -> str:
    """使用者輸入進 SQL LIKE 前跳脫萬用字元(搭配 .like(..., escape="\\")),
    讓 % / _ 只當字面比對。"""
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _csv_cell(v):
    """防 CSV 公式注入:儲存格以 = + - @ Tab CR 開頭時前綴 '。

    VM 名稱 / 備註等來自 vCenter(較低信任來源),避免對開啟 CSV 的
    工作站發動公式/DDE 攻擊。"""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", chr(9), chr(13)):
        return "'" + v
    return v


def csv_response(filename: str, header: list[str], rows) -> Response:
    """CSV 下載:UTF-8 BOM 讓 Excel 直接開啟不亂碼;中文檔名走 RFC 5987。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows([_csv_cell(c) for c in row] for row in rows)
    return Response(
        "\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f"attachment; filename=export.csv;"
                 f" filename*=UTF-8''{quote(filename)}"})
