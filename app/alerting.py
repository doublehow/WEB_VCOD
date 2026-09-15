"""警示引擎:規則定義、每輪評估、去抖狀態機、抑制 / 排除、落地與通知。

流程(poller 每輪 publish 快照後在執行緒內呼叫 run_round):
1. 依 RULES 對快照中每個物件評估 → 本輪候選 {alert_key: (level, value)}。
2. 狀態機:用量類連續 N 輪命中才 firing、N 輪未命中才 resolved;
   狀態類(二元事實)N 固定 1。firing 中等級改變 → changed。
3. 抑制:主機失聯 / 維護 → 其 VM 不評估;vCenter 本輪失敗(stale)→ 該座凍結
   (不計命中也不計未命中,既有警示維持);vCenter 停用 → 其進行中警示靜默解除
   (落地歷史、不通知)。
4. 排除:名稱樣式(fnmatch,不分大小寫)、範本 VM、維護模式主機。
5. 轉態寫入 alerts / alert_history,整輪合併成一則通知(嚴重 → 警告 → 恢復)。

物件 key 與 inventory 一致:`<vc_id>::<moid>`;警示 key = `<rule>|<物件 key>`。
所有函式皆同步;`active_index()` 回傳供模板用的「物件 key → 進行中警示」索引,
以整個 dict 替換發布(與快照相同的一致性做法)。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime

from app.config import DEBOUNCE_MAX, DEBOUNCE_MIN, local_now, settings
from app.database import SessionLocal
from app.inventory import Snapshot
from app.models import ALERT_LEVEL_LABELS, Alert, AlertHistory

logger = logging.getLogger("vcod.alerting")

TARGET_LABELS = {"vcenter": "vCenter", "host": "主機", "datastore": "儲存區", "vm": "VM"}
LAYER_LABELS = {"platform": "虛擬平台層", "guest": "客體 OS 層"}


# ---------------------------------------------------------------- 規則定義

@dataclass(frozen=True)
class Rule:
    key: str
    label: str
    layer: str            # platform / guest
    target: str           # vcenter / host / datastore / vm
    kind: str             # usage(門檻數值,去抖 N 輪)/ state(二元,立即)
    unit: str = ""        # % / GB / 天
    warning: float | None = None    # 預設門檻;None = 此等級不適用
    critical: float | None = None
    lower_is_worse: bool = False    # True:數值 ≤ 門檻才命中(如剩餘 GB)
    description: str = ""


RULES: list[Rule] = [
    # ---- 虛擬平台層 ----
    Rule("vc_down", "vCenter 連線失敗", "platform", "vcenter", "state",
         description="輪詢無法登入或讀取 vCenter;該座其他物件凍結評估"),
    Rule("host_disconnected", "主機失聯", "platform", "host", "state",
         description="connectionState 非 connected(disconnected / notResponding);其 VM 暫停評估"),
    Rule("host_cpu", "主機 CPU 使用率", "platform", "host", "usage", "%", 80, 95),
    Rule("host_mem", "主機記憶體使用率", "platform", "host", "usage", "%", 80, 95),
    Rule("host_health", "主機硬體 / 整體健康", "platform", "host", "state",
         description="vCenter overallStatus 黃 = 警告、紅 = 嚴重(硬體感測器、告警彙總)"),
    Rule("ds_usage", "儲存區用量", "platform", "datastore", "usage", "%", 80, 90),
    Rule("ds_inaccessible", "儲存區無法存取", "platform", "datastore", "state"),
    Rule("ds_free", "儲存區剩餘空間", "platform", "datastore", "usage", "GB", 200, 50,
         lower_is_worse=True, description="剩餘 GB 低於門檻"),
    Rule("vm_cpu", "VM CPU 使用率", "platform", "vm", "usage", "%", 90, None,
         description="overallCpuUsage ÷ maxCpuUsage;僅開機中的 VM"),
    Rule("vm_ready", "VM CPU Ready", "platform", "vm", "usage", "%", 10, 20,
         description="等待實體 CPU 排程的比例(vSphere 7.0+);高 = 主機 CPU 爭用"),
    Rule("vm_mem_pressure", "VM 記憶體回收(balloon / swap)", "platform", "vm", "state",
         description="ballooned / swapped / compressed 任一 > 0,代表主機記憶體不足"),
    Rule("vm_snapshot_age", "VM 快照過久", "platform", "vm", "usage", "天", 7, 30,
         description="最舊快照存在天數"),
    # ---- 客體 OS 層(VMware Tools)----
    Rule("guest_fs", "Guest 檔案系統用量", "guest", "vm", "usage", "%", 85, 95,
         description="Tools 回報的各掛載點,取最高者;排除 tmpfs / 光碟 / 網路掛載與 < 1 GB"),
    Rule("guest_heartbeat", "Guest 心跳異常", "guest", "vm", "state",
         description="VM 開機且 Tools 執行中,但心跳 gray = 警告、red = 嚴重(OS 無回應)"),
    Rule("guest_tools", "VMware Tools 未執行", "guest", "vm", "state",
         description="VM 開機中但 Tools 未執行;客體層其餘規則因此失明"),
    Rule("guest_tools_outdated", "VMware Tools 版本過舊", "guest", "vm", "state",
         description="toolsVersionStatus2 為 NeedUpgrade / TooOld / SupportedOld / Blacklisted;"
                     "對應 vCenter 摘要頁「可使用較新版本的 VMware Tools」"),
    Rule("guest_kernel_crash", "Guest Kernel Crash", "guest", "vm", "state"),
]
RULE_MAP = {r.key: r for r in RULES}


def alert_href(rule: str, target_type: str, obj_key: str) -> str:
    """警示 → 深連結:依規則面向決定頁面(VM 的容量類規則到儲存頁、運算類到運算頁),
    帶 ?focus=<物件 key> 讓該頁只顯示此物件(VM 會展開所屬主機 / 儲存區卡片)。"""
    from urllib.parse import quote
    if target_type == "vcenter":
        return "/vcenters"
    if target_type == "host":
        page = "/compute"
    elif target_type == "datastore":
        page = "/storage"
    else:   # vm:容量類 → 儲存頁,其餘 → 運算頁
        page = "/storage" if rule in ("guest_fs", "vm_snapshot_age") else "/compute"
    return f"{page}?focus={quote(obj_key, safe='')}"


def rule_config(rule: Rule) -> dict:
    """預設 + 使用者覆寫 → {enabled, warning, critical}。"""
    ov = settings.alert_rules.get(rule.key) or {}
    def _num(v, default):
        if v is None or v == "":
            return default
        try:
            return float(v)
        except (TypeError, ValueError):
            return default
    return {
        "enabled": bool(ov.get("enabled", True)),
        "warning": _num(ov.get("warning"), rule.warning) if rule.warning is not None else None,
        "critical": _num(ov.get("critical"), rule.critical) if rule.critical is not None else None,
    }


def rules_version() -> str:
    """規則設定的指紋(納入快照 token,規則改了畫面才會重畫)。"""
    blob = json.dumps({"r": settings.alert_rules, "d": settings.alert_debounce_rounds,
                       "x": settings.alert_exclude_patterns}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]


def _debounce() -> int:
    return max(DEBOUNCE_MIN, min(DEBOUNCE_MAX, int(settings.alert_debounce_rounds or 3)))


def _excluded(name: str) -> bool:
    n = (name or "").lower()
    return any(fnmatch.fnmatchcase(n, (p or "").lower()) for p in settings.alert_exclude_patterns if p)


# ---------------------------------------------------------------- 評估

def _level_for(value: float, cfg: dict, lower_is_worse: bool) -> str | None:
    """依門檻判等級;critical 優先。"""
    def hit(th):
        return th is not None and (value <= th if lower_is_worse else value >= th)
    if hit(cfg["critical"]):
        return "critical"
    if hit(cfg["warning"]):
        return "warning"
    return None


def _fmt(v: float, unit: str) -> str:
    return f"{v:,.0f} {unit}" if unit == "GB" else f"{v:g} {unit}" if unit else f"{v:g}"


def _eval_usage(rule: Rule, value: float | None, cfg: dict, extra: str = "") -> tuple[str, str] | None:
    if value is None:
        return None
    lvl = _level_for(value, cfg, rule.lower_is_worse)
    if lvl is None:
        return None
    th = cfg[lvl]
    sign = "≤" if rule.lower_is_worse else "≥"
    text = f"{_fmt(value, rule.unit)}({sign} {_fmt(th, rule.unit)})"
    return lvl, (f"{extra} {text}" if extra else text)


def evaluate(snap: Snapshot) -> tuple[dict[str, dict], dict]:
    """快照 → (候選 {alert_key: 候選 dict}, 抑制統計)。

    候選 dict:rule / level / value / target_type / target_name / vc_name / vc_id / obj_key。
    凍結中的 vCenter(stale)不產生候選,也由呼叫端跳過其狀態遞增。
    """
    cands: dict[str, dict] = {}
    supp = {"frozen_vcs": [], "disabled_vcs": [], "hosts_suppressed_vms": 0, "excluded": 0}
    cfgs = {r.key: rule_config(r) for r in RULES}

    def add(rule: Rule, obj_key: str, level: str, value: str, name: str, vc: str, vc_id: int):
        cands[f"{rule.key}|{obj_key}"] = {
            "rule": rule.key, "layer": rule.layer, "level": level, "value": value,
            "target_type": rule.target, "target_name": name, "vc_name": vc, "vc_id": vc_id,
            "obj_key": obj_key, "kind": rule.kind,
        }

    for vc in snap.vcs.values():
        if not vc.enabled:
            supp["disabled_vcs"].append(vc.id)
            continue
        vkey = f"{vc.id}::vcenter"
        if vc.status == "failed":
            if cfgs["vc_down"]["enabled"]:
                add(RULE_MAP["vc_down"], vkey, "critical", (vc.error or "連線失敗")[:200], vc.name, vc.name, vc.id)
            if vc.stale or vc.data is None:
                supp["frozen_vcs"].append(vc.id)
                continue       # 凍結:資料是舊的,不據此新增或解除其他警示
        if vc.data is None:
            continue

        # ---- 主機 ----
        suppressed_hosts: set[str] = set()   # 失聯 / 維護 → 其 VM 不評估
        for h in vc.data.hosts:
            hkey = f"{vc.id}::{h['moid']}"
            if h["maintenance"]:
                suppressed_hosts.add(h["moid"])
                continue
            if _excluded(h["name"]):
                supp["excluded"] += 1
                continue
            if h["connection_state"] and h["connection_state"] != "connected":
                suppressed_hosts.add(h["moid"])
                if cfgs["host_disconnected"]["enabled"]:
                    add(RULE_MAP["host_disconnected"], hkey, "critical",
                        f"連線狀態 {h['connection_state']}", h["name"], vc.name, vc.id)
                continue
            for rk, val in (("host_cpu", h["cpu_pct"]), ("host_mem", h["mem_pct"])):
                if cfgs[rk]["enabled"]:
                    r = _eval_usage(RULE_MAP[rk], val, cfgs[rk])
                    if r:
                        add(RULE_MAP[rk], hkey, r[0], r[1], h["name"], vc.name, vc.id)
            if cfgs["host_health"]["enabled"] and h.get("overall_status") in ("yellow", "red"):
                lvl = "critical" if h["overall_status"] == "red" else "warning"
                issues = h.get("health_issues") or []
                detail = ";".join(issues[:3]) + (f" …另 {len(issues) - 3} 項" if len(issues) > 3 else "")
                add(RULE_MAP["host_health"], hkey, lvl,
                    f"{h['overall_status']}" + (f":{detail}" if detail else "(vCenter 未列出具體原因)"),
                    h["name"], vc.name, vc.id)

        # ---- 儲存區 ----
        for d in vc.data.datastores:
            dkey = f"{vc.id}::{d['moid']}"
            if _excluded(d["name"]):
                supp["excluded"] += 1
                continue
            if not d["accessible"]:
                if cfgs["ds_inaccessible"]["enabled"]:
                    add(RULE_MAP["ds_inaccessible"], dkey, "critical", "accessible = false",
                        d["name"], vc.name, vc.id)
                continue
            if cfgs["ds_usage"]["enabled"]:
                r = _eval_usage(RULE_MAP["ds_usage"], d["used_pct"], cfgs["ds_usage"])
                if r:
                    add(RULE_MAP["ds_usage"], dkey, r[0], r[1], d["name"], vc.name, vc.id)
            if cfgs["ds_free"]["enabled"] and d["capacity_gb"]:
                r = _eval_usage(RULE_MAP["ds_free"], d["free_gb"], cfgs["ds_free"])
                if r:
                    add(RULE_MAP["ds_free"], dkey, r[0], r[1], d["name"], vc.name, vc.id)

        # ---- VM ----
        for v in vc.data.vms:
            if v["is_template"]:
                continue
            if v["host_moid"] in suppressed_hosts:
                supp["hosts_suppressed_vms"] += 1
                continue
            if _excluded(v["name"]):
                supp["excluded"] += 1
                continue
            mkey = f"{vc.id}::{v['moid']}"
            on = v["is_on"]
            # 平台層
            if on and cfgs["vm_cpu"]["enabled"]:
                r = _eval_usage(RULE_MAP["vm_cpu"], v.get("cpu_pct"), cfgs["vm_cpu"])
                if r:
                    add(RULE_MAP["vm_cpu"], mkey, r[0], r[1], v["name"], vc.name, vc.id)
            if on and cfgs["vm_ready"]["enabled"]:
                r = _eval_usage(RULE_MAP["vm_ready"], v.get("ready_pct"), cfgs["vm_ready"])
                if r:
                    add(RULE_MAP["vm_ready"], mkey, r[0], r[1], v["name"], vc.name, vc.id)
            if on and cfgs["vm_mem_pressure"]["enabled"]:
                parts = [f"{lbl} {mb} MB" for lbl, mb in (("balloon", v.get("ballooned_mb", 0)),
                                                          ("swap", v.get("swapped_mb", 0)),
                                                          ("compressed", v.get("compressed_mb", 0))) if mb]
                if parts:
                    add(RULE_MAP["vm_mem_pressure"], mkey, "warning", " / ".join(parts), v["name"], vc.name, vc.id)
            if cfgs["vm_snapshot_age"]["enabled"] and v.get("snapshot_count"):
                r = _eval_usage(RULE_MAP["vm_snapshot_age"], v.get("snapshot_oldest_days"),
                                cfgs["vm_snapshot_age"], extra=f"{v['snapshot_count']} 個快照,最舊")
                if r:
                    add(RULE_MAP["vm_snapshot_age"], mkey, r[0], r[1], v["name"], vc.name, vc.id)
            # 客體層(僅開機中)
            if not on:
                continue
            if cfgs["guest_kernel_crash"]["enabled"] and v.get("kernel_crashed"):
                add(RULE_MAP["guest_kernel_crash"], mkey, "critical", "guestKernelCrashed = true",
                    v["name"], vc.name, vc.id)
            if not v.get("tools_running"):
                if cfgs["guest_tools"]["enabled"]:
                    add(RULE_MAP["guest_tools"], mkey, "warning", "toolsRunningStatus ≠ running",
                        v["name"], vc.name, vc.id)
                continue   # Tools 未執行時心跳 / 檔案系統皆無資料,不重複發
            if cfgs["guest_tools_outdated"]["enabled"] and v.get("tools_outdated"):
                ver = f"(版本 {v['tools_version']})" if v.get("tools_version") else ""
                add(RULE_MAP["guest_tools_outdated"], mkey, "warning",
                    f"{v.get('tools_status', '')}{ver}", v["name"], vc.name, vc.id)
            if cfgs["guest_heartbeat"]["enabled"] and v.get("heartbeat") in ("gray", "red"):
                lvl = "critical" if v["heartbeat"] == "red" else "warning"
                # 舊版 Tools 常不回心跳:gray 多半是「果」,把「因」一併寫進數值
                note = ",Tools 版本過舊可能為原因" if (v["heartbeat"] == "gray" and v.get("tools_outdated")) else ""
                add(RULE_MAP["guest_heartbeat"], mkey, lvl, f"heartbeat = {v['heartbeat']}{note}",
                    v["name"], vc.name, vc.id)
            if cfgs["guest_fs"]["enabled"] and v.get("guest_disks"):
                top = v["guest_disks"][0]
                r = _eval_usage(RULE_MAP["guest_fs"], top["pct"], cfgs["guest_fs"], extra=top["path"])
                if r:
                    add(RULE_MAP["guest_fs"], mkey, r[0], r[1], v["name"], vc.name, vc.id)
    return cands, supp


# ---------------------------------------------------------------- 狀態機

@dataclass
class _State:
    hits: int = 0
    misses: int = 0
    firing: bool = False
    level: str = ""
    value: str = ""
    meta: dict = field(default_factory=dict)
    first_at: datetime | None = None


@dataclass
class RoundResult:
    transitions: list[dict] = field(default_factory=list)   # {event, level, ...}
    active: int = 0
    suppression: dict = field(default_factory=dict)
    notified: str = ""


_lock = threading.Lock()
_states: dict[str, _State] = {}
_active_index: dict[str, list[dict]] = {}      # obj_key → [進行中警示 dict](模板用)
_last_result: RoundResult = RoundResult()
_loaded = False


def load_state() -> None:
    """啟動時自 alerts 表讀回進行中警示(視為已去抖、已通知),重啟不重發。"""
    global _loaded
    with _lock:
        if _loaded:
            return
        try:
            with SessionLocal() as db:
                for a in db.query(Alert).all():
                    _states[a.key] = _State(
                        hits=_debounce(), firing=True, level=a.level, value=a.value,
                        first_at=a.first_at,
                        meta={"rule": a.rule, "layer": a.layer, "target_type": a.target_type,
                              "target_name": a.target_name, "vc_name": a.vc_name,
                              "obj_key": a.key.split("|", 1)[1] if "|" in a.key else ""})
            _loaded = True
            _rebuild_index()
            logger.info("已載入 %d 筆進行中警示", len(_states))
        except Exception:  # noqa: BLE001
            logger.exception("載入進行中警示失敗")


def _rebuild_index() -> None:
    global _active_index
    idx: dict[str, list[dict]] = {}
    for key, st in _states.items():
        if not st.firing:
            continue
        rule = RULE_MAP.get(st.meta.get("rule", ""))
        idx.setdefault(st.meta.get("obj_key", ""), []).append({
            "key": key, "rule": st.meta.get("rule", ""), "label": rule.label if rule else st.meta.get("rule", ""),
            "layer": st.meta.get("layer", ""), "level": st.level, "value": st.value,
            "first_at": st.first_at,
            "target_type": st.meta.get("target_type", ""), "target_name": st.meta.get("target_name", ""),
            "vc_name": st.meta.get("vc_name", ""),
        })
    for lst in idx.values():
        lst.sort(key=lambda a: (a["level"] != "critical", a["label"]))
    _active_index = idx


def active_index() -> dict[str, list[dict]]:
    return _active_index


def last_result() -> RoundResult:
    return _last_result


def _duration_text(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} 秒"
    if s < 3600:
        return f"{s // 60} 分"
    if s < 86400:
        return f"{s // 3600} 小時 {(s % 3600) // 60} 分"
    return f"{s // 86400} 天 {(s % 86400) // 3600} 小時"


def run_round(snap: Snapshot) -> RoundResult:
    """評估一輪、推進狀態機、落地並通知;回傳本輪結果。(同步,於執行緒內呼叫)"""
    global _last_result
    load_state()
    cands, supp = evaluate(snap)
    frozen = set(supp["frozen_vcs"])
    disabled = set(supp["disabled_vcs"])
    n = _debounce()
    now = local_now()
    transitions: list[dict] = []

    with _lock:
        # 命中者:遞增 hits、清 misses
        for key, c in cands.items():
            st = _states.get(key)
            if st is None:
                st = _states[key] = _State()
            st.misses = 0
            st.hits += 1
            st.meta = {k: c[k] for k in ("rule", "layer", "target_type", "target_name", "vc_name", "obj_key")}
            need = 1 if c["kind"] == "state" else n
            if not st.firing and st.hits >= need:
                st.firing, st.level, st.value, st.first_at = True, c["level"], c["value"], now
                transitions.append({"event": "firing", "key": key, "level": c["level"], "value": c["value"], **st.meta})
            elif st.firing:
                if st.level != c["level"]:
                    transitions.append({"event": "changed", "key": key, "level": c["level"], "value": c["value"],
                                        "prev_level": st.level, **st.meta})
                    st.level = c["level"]
                st.value = c["value"]
        # 未命中者:凍結座跳過;其餘遞增 misses,達 N(狀態類 1)即解除
        for key in list(_states):
            if key in cands:
                continue
            st = _states[key]
            obj_key = st.meta.get("obj_key", "")
            try:
                vc_id = int(obj_key.split("::", 1)[0])
            except (ValueError, IndexError):
                vc_id = -1
            if vc_id in frozen:
                continue
            if vc_id in disabled:
                # 停用座:進行中警示靜默解除(記歷史、不通知),不當作「恢復」發出
                if st.firing:
                    dur = (now - st.first_at).total_seconds() if st.first_at else 0.0
                    transitions.append({"event": "resolved", "key": key, "level": st.level,
                                        "value": "vCenter 已停用", "duration_s": dur, "silent": True,
                                        **st.meta})
                del _states[key]
                continue
            rule = RULE_MAP.get(st.meta.get("rule", ""))
            need = 1 if (rule is None or rule.kind == "state") else n
            st.hits = 0
            st.misses += 1
            if st.firing and st.misses >= need:
                dur = (now - st.first_at).total_seconds() if st.first_at else 0.0
                transitions.append({"event": "resolved", "key": key, "level": st.level, "value": st.value,
                                    "duration_s": dur, **st.meta})
                del _states[key]
            elif not st.firing and st.misses >= need:
                del _states[key]
        _rebuild_index()
        active = sum(1 for s in _states.values() if s.firing)

    result = RoundResult(transitions=transitions, active=active, suppression=supp)
    if transitions:
        loud = [t for t in transitions if not t.get("silent")]
        result.notified = _notify(loud, now) if loud else "靜默(vCenter 停用)"
        _persist(transitions, now, result.notified)
    _last_result = result
    return result


# ---------------------------------------------------------------- 落地 / 通知

def _persist(transitions: list[dict], now: datetime, notified: str) -> None:
    try:
        with SessionLocal() as db:
            for t in transitions:
                common = dict(key=t["key"], rule=t["rule"], layer=t["layer"], level=t["level"],
                              target_type=t["target_type"], target_name=t["target_name"],
                              vc_name=t["vc_name"], value=t["value"])
                if t["event"] == "firing":
                    a = db.query(Alert).filter(Alert.key == t["key"]).first()
                    if a is None:
                        db.add(Alert(**common, first_at=now, last_at=now))
                    else:   # 記憶體狀態曾遺失(載入失敗等):沿用既有列,不重複插入
                        for k, v in common.items():
                            setattr(a, k, v)
                        a.last_at = now
                elif t["event"] == "changed":
                    a = db.query(Alert).filter(Alert.key == t["key"]).first()
                    if a:
                        a.level, a.value, a.last_at = t["level"], t["value"], now
                else:
                    db.query(Alert).filter(Alert.key == t["key"]).delete()
                db.add(AlertHistory(**common, event=t["event"], at=now,
                                    duration_s=float(t.get("duration_s", 0.0)),
                                    notified=("靜默(vCenter 停用)" if t.get("silent") else notified)[:300]))
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("警示落地失敗")


def _line(t: dict) -> str:
    tl = TARGET_LABELS.get(t["target_type"], t["target_type"])
    rule = RULE_MAP.get(t["rule"])
    label = rule.label if rule else t["rule"]
    vc = f"({t['vc_name']})" if t["target_type"] != "vcenter" else ""
    return f"{tl} {t['target_name']}{vc}:{label} {t['value']}"


def format_message(transitions: list[dict], now: datetime) -> tuple[str, str]:
    """整輪轉態 → (主旨, 內文);嚴重 → 警告 → 等級變更 → 恢復,超過 30 行截斷。"""
    crit = [t for t in transitions if t["event"] == "firing" and t["level"] == "critical"]
    warn = [t for t in transitions if t["event"] == "firing" and t["level"] == "warning"]
    chg = [t for t in transitions if t["event"] == "changed"]
    res = [t for t in transitions if t["event"] == "resolved"]
    parts = []
    if crit:
        parts.append(f"🔴 {len(crit)} 嚴重")
    if warn:
        parts.append(f"⚠️ {len(warn)} 警告")
    if chg:
        parts.append(f"↕ {len(chg)} 等級變更")
    if res:
        parts.append(f"✅ {len(res)} 恢復")
    subject = "VCOD 警示:" + " / ".join(parts)
    lines: list[str] = []
    for t in crit:
        lines.append(f"🔴 [嚴重] {_line(t)}")
    for t in warn:
        lines.append(f"⚠️ [警告] {_line(t)}")
    for t in chg:
        arrow = f"{ALERT_LEVEL_LABELS.get(t.get('prev_level', ''), '?')} → {ALERT_LEVEL_LABELS.get(t['level'], t['level'])}"
        lines.append(f"↕ [{arrow}] {_line(t)}")
    for t in res:
        lines.append(f"✅ [恢復] {_line(t)}(持續 {_duration_text(t.get('duration_s', 0))})")
    if len(lines) > 30:
        extra = len(lines) - 30
        lines = lines[:30] + [f"… 另有 {extra} 筆,詳見儀表板「警示」頁"]
    body = f"{now.strftime('%Y-%m-%d %H:%M:%S')}\n" + "\n".join(lines)
    return subject, body


def _notify(transitions: list[dict], now: datetime) -> str:
    from app import notify
    sendable = [t for t in transitions
                if t["event"] != "resolved" or settings.alert_notify_recovery]
    if not sendable:
        return "恢復通知已關閉"
    subject, body = format_message(sendable, now)
    if not notify.channels_configured():
        logger.info("警示轉態 %d 筆(未設定通知管道):%s", len(sendable), subject)
        return "未設定通知管道"
    result = notify.send(subject, body)
    logger.info("警示通知「%s」→ %s", subject, result)
    return result


def forward_vcenter_alarms(snap: Snapshot, prev_keys: set[str]) -> set[str]:
    """vCenter 內建告警外送(選用):新出現的黃 / 紅告警發一則;回傳本輪 key 集合。"""
    keys: set[str] = set()
    new: list[dict] = []
    for vc in snap.vcs.values():
        if not vc.data or vc.stale:
            keys.update(k for k in prev_keys if k.startswith(f"{vc.id}|"))
            continue
        for a in vc.data.alarms:
            k = f"{vc.id}|{a['entity_type']}|{a['entity_name']}|{a['alarm_moid']}"
            keys.add(k)
            if k not in prev_keys:
                new.append({**a, "vc_name": vc.name})
    if new and settings.alert_forward_vcenter_alarms:
        from app import notify
        if notify.channels_configured():
            lines = [f"{'🔴' if a['status'] == 'red' else '⚠️'} {a['entity_type']} {a['entity_name']}"
                     f"({a['vc_name']}):{a['alarm']}" for a in new[:30]]
            if len(new) > 30:
                lines.append(f"… 另有 {len(new) - 30} 筆")
            res = notify.send(f"VCOD:vCenter 內建告警 {len(new)} 筆",
                              f"{local_now().strftime('%Y-%m-%d %H:%M:%S')}\n" + "\n".join(lines))
            logger.info("vCenter 內建告警外送 %d 筆 → %s", len(new), res)
    return keys


def cleanup_history(days: int) -> int:
    if days <= 0:
        return 0
    from datetime import timedelta
    with SessionLocal() as db:
        n = db.query(AlertHistory).filter(AlertHistory.at < local_now() - timedelta(days=days)).delete()
        db.commit()
    return n
