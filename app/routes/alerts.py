"""警示頁:進行中 / 歷史 / 規則 三分頁;規則存 config.json(alert_rules 等)。"""
import math

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import alerting, inventory
from app.alerting import LAYER_LABELS, RULES, TARGET_LABELS, rule_config
from app.audit import audit
from app.config import DEBOUNCE_MAX, DEBOUNCE_MIN, clamp_int, local_now, save_settings, settings
from app.database import get_db
from app.inventory import vcenter_alarms
from app.models import Alert, AlertHistory
from app.webutil import like_escape, render

router = APIRouter()


# 狀態類規則的固定等級(無門檻可調)
_FIXED_LEVELS = {
    "vc_down": "嚴重", "host_disconnected": "嚴重", "ds_inaccessible": "嚴重",
    "guest_kernel_crash": "嚴重", "vm_mem_pressure": "警告", "guest_tools": "警告",
    "guest_tools_outdated": "警告",
    "host_health": "黃 = 警告 / 紅 = 嚴重", "guest_heartbeat": "gray = 警告 / red = 嚴重",
}


def rules_table() -> list[dict]:
    """規則表(供警示頁與設定顯示):預設 + 覆寫後的有效值。"""
    rows = []
    for r in RULES:
        cfg = rule_config(r)
        rows.append({
            "fixed": _FIXED_LEVELS.get(r.key, "") if r.kind == "state" else "",
            "key": r.key, "label": r.label, "layer": r.layer, "layer_label": LAYER_LABELS[r.layer],
            "target": TARGET_LABELS[r.target], "kind": r.kind, "unit": r.unit,
            "lower_is_worse": r.lower_is_worse, "description": r.description,
            "enabled": cfg["enabled"],
            "warning": cfg["warning"], "critical": cfg["critical"],
            "has_warning": r.warning is not None, "has_critical": r.critical is not None,
            "default_warning": r.warning, "default_critical": r.critical,
        })
    return rows


@router.get("/alerts")
def alerts_page(request: Request, db: Session = Depends(get_db),
                tab: str = "active", q: str = "", level: str = "", saved: str = ""):
    if tab not in ("active", "history", "rules"):
        tab = "active"
    q = q.strip()[:100]
    level = level if level in ("warning", "critical") else ""
    active: list[Alert] = []
    history: list[AlertHistory] = []
    if tab == "active":
        query = db.query(Alert)
        if level:
            query = query.filter(Alert.level == level)
        if q:
            like = f"%{like_escape(q)}%"
            query = query.filter(Alert.target_name.like(like, escape="\\")
                                 | Alert.vc_name.like(like, escape="\\")
                                 | Alert.value.like(like, escape="\\"))
        active = query.all()
        active.sort(key=lambda a: (a.level != "critical", a.first_at))
    elif tab == "history":
        query = db.query(AlertHistory)
        if level:
            query = query.filter(AlertHistory.level == level)
        if q:
            like = f"%{like_escape(q)}%"
            query = query.filter(AlertHistory.target_name.like(like, escape="\\")
                                 | AlertHistory.vc_name.like(like, escape="\\")
                                 | AlertHistory.value.like(like, escape="\\"))
        history = query.order_by(AlertHistory.id.desc()).limit(300).all()
    snap = inventory.current()
    last = alerting.last_result()
    return render(request, "alerts.html", "alerts", tab=tab, q=q, level=level, saved=saved,
                  items=active, history=history, rules=rules_table(),
                  rule_labels={r.key: r.label for r in RULES},
                  vc_alarms=vcenter_alarms(snap), suppression=last.suppression,
                  now=local_now(), s=settings, debounce_min=DEBOUNCE_MIN, debounce_max=DEBOUNCE_MAX,
                  active_total=db.query(Alert).count())


@router.post("/alerts/rules")
async def rules_save(request: Request):
    """儲存規則覆寫 + 全域警示參數(表單欄位動態,直接讀 form)。"""
    form = await request.form()
    overrides: dict[str, dict] = {}
    for r in RULES:
        ov: dict = {}
        if not form.get(f"en_{r.key}"):
            ov["enabled"] = False
        for lvl in ("warning", "critical"):
            default = getattr(r, lvl)
            if default is None:
                continue
            raw = str(form.get(f"{lvl}_{r.key}", "")).strip()
            if raw == "":
                continue
            try:
                val = float(raw)
            except ValueError:
                continue
            if not math.isfinite(val) or val < 0 or (r.unit == "%" and val > 100):
                continue   # nan / inf / 負值 / 超過 100% 一律忽略:NaN 會讓比較恆為 False,規則靜默失效
            if val != default:
                ov[lvl] = val
        if ov:
            overrides[r.key] = ov
    patterns = [p.strip() for p in str(form.get("exclude_patterns", "")).replace("，", ",")
                .replace("\n", ",").split(",") if p.strip()][:100]
    save_settings({
        "alert_rules": overrides,
        "alert_debounce_rounds": clamp_int(form.get("debounce"), 3, DEBOUNCE_MIN, DEBOUNCE_MAX),
        "alert_exclude_patterns": patterns,
        "alert_notify_recovery": bool(form.get("notify_recovery")),
        "alert_forward_vcenter_alarms": bool(form.get("forward_vcenter_alarms")),
    })
    audit(request, "alert_rules_save",
          f"更新警示規則:覆寫 {len(overrides)} 條,去抖 {settings.alert_debounce_rounds} 輪,"
          f"排除 {len(patterns)} 個樣式")
    return RedirectResponse("/alerts?tab=rules&saved=1", status_code=303)
