"""庫存快照(記憶體):合併各 vCenter 抓取結果,提供各頁面所需的檢視。

- 快照以整個物件替換(而非就地修改),讀取端拿到的永遠是一致的版本。
- 某座 vCenter 這輪失敗時保留其上一輪資料並標 stale,畫面不會忽然少一半。
- 所有跨 vCenter 的實體以 `vc_id::moid` 為鍵(儲存區 / Port Group 名稱在
  不同 vCenter 常重複,舊版以名稱合併會把兩座的 datastore1 疊成一張卡)。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime

from app.vsphere import VcData

UNKNOWN_NET = "(未指定網路)"


@dataclass
class VcState:
    """單座 vCenter 在快照中的狀態。"""
    id: int
    name: str
    host: str
    enabled: bool = True
    status: str = "pending"          # ok / failed / disabled / pending
    error: str = ""
    stale: bool = False              # 這輪失敗、沿用上一輪資料
    about: str = ""
    polled_at: datetime | None = None
    ok_at: datetime | None = None
    duration_ms: int = 0
    data: VcData | None = None


@dataclass
class Snapshot:
    vcs: dict[int, VcState] = field(default_factory=dict)
    polled_at: datetime | None = None
    round_ms: int = 0

    # ---- 扁平清單(附 vc 標註) ----
    def _tag(self, items: list[dict], vc: VcState) -> list[dict]:
        out = []
        for it in items:
            d = dict(it)
            d["vc"] = vc.name
            d["vc_id"] = vc.id
            d["stale"] = vc.stale
            d["key"] = f"{vc.id}::{it.get('moid', '')}"
            out.append(d)
        return out

    def hosts(self) -> list[dict]:
        out: list[dict] = []
        for vc in self.vcs.values():
            if vc.data:
                out.extend(self._tag(vc.data.hosts, vc))
        return out

    def vms(self) -> list[dict]:
        out: list[dict] = []
        for vc in self.vcs.values():
            if vc.data:
                out.extend(self._tag(vc.data.vms, vc))
        return out

    def datastores(self) -> list[dict]:
        out: list[dict] = []
        for vc in self.vcs.values():
            if vc.data:
                out.extend(self._tag(vc.data.datastores, vc))
        return out

    def networks(self) -> list[dict]:
        out: list[dict] = []
        for vc in self.vcs.values():
            if vc.data:
                out.extend(self._tag(vc.data.networks, vc))
        return out


_lock = threading.Lock()
_snapshot = Snapshot()


def current() -> Snapshot:
    return _snapshot


def publish(snap: Snapshot) -> None:
    global _snapshot
    with _lock:
        _snapshot = snap


# ---------------------------------------------------------------- 檢視

def _vm_sort_key(vm: dict):
    return (0 if vm["is_on"] else 1, vm["name"].lower())


# VM 列小籤依頁面情境過濾:運算頁只看運算 / 客體健康類,儲存頁只看容量類;
# VM 總覽、警示頁、儀表板顯示全部。卡片本身(主機 / 儲存區)的規則不受影響。
VM_RULES_COMPUTE = {"vm_cpu", "vm_ready", "vm_mem_pressure", "guest_heartbeat",
                    "guest_tools", "guest_tools_outdated", "guest_kernel_crash"}
VM_RULES_STORAGE = {"guest_fs", "vm_snapshot_age"}


def _vm_alerts(alerts: dict[str, list[dict]], vm: dict, rules: set[str]) -> list[dict]:
    return [a for a in alerts.get(vm["key"], []) if a["rule"] in rules]


# 卡片外框呼吸燈只對「使用率」類規則(達警告門檻即紅框);其他警示(硬體健康、失聯、
# 無法存取、VM 層)只顯示小籤 —— 2026-09-15 使用者決定
BREATHE_RULES = {"host_cpu", "host_mem", "ds_usage"}


def _attach_alerts(card: dict, alerts: dict[str, list[dict]]) -> None:
    """把進行中警示掛到卡片:alerts 全部列出(小籤);alert_level 只看使用率類(呼吸燈)。"""
    lst = alerts.get(card["key"], [])
    card["alerts"] = lst
    usage = [a for a in lst if a["rule"] in BREATHE_RULES]
    card["alert_level"] = ("critical" if any(a["level"] == "critical" for a in usage)
                           else "warning" if usage else None)


def compute_view(snap: Snapshot, alerts: dict[str, list[dict]] | None = None) -> list[dict]:
    """主機卡片:每台主機 + 其 VM(開機優先、CPU 用量高者在前)。警示狀態取自引擎索引。"""
    alerts = alerts or {}
    vms_by_host: dict[str, list[dict]] = {}
    for vm in snap.vms():
        vms_by_host.setdefault(f"{vm['vc_id']}::{vm['host_moid']}", []).append(vm)
    cards = []
    for h in sorted(snap.hosts(), key=lambda x: (x["vc"].lower(), x["name"].lower())):
        vms = sorted(vms_by_host.get(h["key"], []),
                     key=lambda v: (0 if v["is_on"] else 1, -v["cpu_usage_mhz"], v["name"].lower()))
        card = dict(h)
        card["vms"] = [dict(v, alerts=_vm_alerts(alerts, v, VM_RULES_COMPUTE)) for v in vms]
        card["vm_on"] = sum(1 for v in vms if v["is_on"])
        _attach_alerts(card, alerts)
        card["search"] = " ".join([h["name"], h["vc"], h["cluster"]] + [v["name"] for v in vms]).lower()
        cards.append(card)
    return cards


def storage_view(snap: Snapshot, alerts: dict[str, list[dict]] | None = None) -> list[dict]:
    """儲存區卡片:容量 + 使用該儲存區的 VM(Datastore 佔用高者在前)。警示狀態取自引擎索引。"""
    alerts = alerts or {}
    vms_by_ds: dict[tuple[int, str], list[dict]] = {}
    for vm in snap.vms():
        for ds in vm["datastores"]:
            vms_by_ds.setdefault((vm["vc_id"], ds), []).append(vm)
    cards = []
    for d in sorted(snap.datastores(), key=lambda x: (x["vc"].lower(), x["name"].lower())):
        vms = sorted(vms_by_ds.get((d["vc_id"], d["name"]), []),
                     key=lambda v: (-v["disk_committed_gb"], v["name"].lower()))
        card = dict(d)
        card["vms"] = [dict(v, alerts=_vm_alerts(alerts, v, VM_RULES_STORAGE)) for v in vms]
        _attach_alerts(card, alerts)
        card["search"] = " ".join([d["name"], d["vc"], d["type"]] + [v["name"] for v in vms]).lower()
        cards.append(card)
    return cards


def _ip_sort(ip: str) -> tuple:
    parts = ip.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return (0, *(int(p) for p in parts))
    return (1, ip)


def network_view(snap: Snapshot) -> list[dict]:
    """Port Group 卡片:每個網路 + 連接其上的 VM(開機優先、IP 排序)。

    含 vCenter 上存在但目前沒有 VM 的 Port Group;NIC 未指定網路的 VM 歸入
    UNKNOWN_NET 群組。
    """
    groups: dict[tuple[int, str], dict] = {}
    for n in snap.networks():
        groups[(n["vc_id"], n["name"])] = {
            "vc": n["vc"], "vc_id": n["vc_id"], "name": n["name"],
            "key": f"{n['vc_id']}::{n['name']}", "members": []}
    for vm in snap.vms():
        nets = vm["networks"] or ([UNKNOWN_NET] if vm["nics"] else [])
        for net in nets:
            g = groups.setdefault((vm["vc_id"], net), {
                "vc": vm["vc"], "vc_id": vm["vc_id"], "name": net,
                "key": f"{vm['vc_id']}::{net}", "members": []})
            nic = next((x for x in vm["nics"] if x["network"] == net), None)
            ips = [ip for ip in (nic["ips"] if nic else []) if ":" not in ip and not ip.startswith("169.254.")]
            ip = ips[0] if ips else (nic["ips"][0] if nic and nic["ips"] else "")
            g["members"].append({"vm": vm, "nic": nic, "ip": ip})
    cards = []
    for g in groups.values():
        g["members"].sort(key=lambda m: (0 if m["vm"]["is_on"] else 1, _ip_sort(m["ip"] or "999"),
                                         m["vm"]["name"].lower()))
        g["on_count"] = sum(1 for m in g["members"] if m["vm"]["is_on"])
        g["off_count"] = len(g["members"]) - g["on_count"]
        g["search"] = " ".join([g["name"], g["vc"]] + [m["vm"]["name"] for m in g["members"]]
                               + [m["ip"] for m in g["members"] if m["ip"]]).lower()
        cards.append(g)
    cards.sort(key=lambda g: (g["name"] == UNKNOWN_NET, not g["members"], g["vc"].lower(), g["name"].lower()))
    return cards


def group_by_vc(snap: Snapshot, cards: list[dict]) -> list[dict]:
    """把卡片依 vCenter 分組(運算 / 儲存 / 網路頁的區段標頭)。

    所有啟用的 vCenter 都會出現(即使本輪失敗、尚無資料),讓使用者看得到
    「這座在、但連不上」;停用的不列。依名稱排序,卡片維持各檢視原本的排序。
    """
    by_id: dict[int, list[dict]] = {}
    for c in cards:
        by_id.setdefault(c["vc_id"], []).append(c)
    groups = []
    for vc in sorted(snap.vcs.values(), key=lambda v: v.name.lower()):
        if not vc.enabled:
            continue
        groups.append({
            "vc_id": vc.id, "vc": vc.name, "host": vc.host, "status": vc.status,
            "stale": vc.stale, "error": vc.error, "about": vc.about,
            "polled_at": vc.polled_at, "cards": by_id.get(vc.id, []),
        })
    return groups


def vm_rows(snap: Snapshot, alerts: dict[str, list[dict]] | None = None) -> list[dict]:
    """VM 總覽表列(含 CSV 匯出用的扁平欄位與進行中警示)。"""
    alerts = alerts or {}
    rows = []
    for vm in sorted(snap.vms(), key=lambda v: (v["vc"].lower(), v["name"].lower())):
        r = dict(vm)
        r["memory_gb"] = round(vm["memory_mb"] / 1024, 1)
        r["alerts"] = alerts.get(vm["key"], [])
        r["alert_level"] = ("critical" if any(a["level"] == "critical" for a in r["alerts"])
                            else "warning" if r["alerts"] else None)
        r["search"] = " ".join(
            [vm["name"], vm["vc"], vm["host"], vm["cluster"], vm["guest_os"], vm["hostname"]]
            + vm["ips"] + vm["networks"] + vm["datastores"]).lower()
        rows.append(r)
    return rows


def object_status(snap: Snapshot) -> dict[str, dict]:
    """物件 key → 目前狀態 badge {label, css}(警示清單「狀態」欄用)。

    VM:ON / OFF / 暫停;主機:連線 / 維護 / 失聯;儲存區:可存取 / 無法存取;
    vCenter:已連線 / 失敗;快照裡找不到的物件(已刪除 / 座已停用)回「—」。
    """
    st: dict[str, dict] = {}
    for vc in snap.vcs.values():
        if vc.enabled:
            st[f"{vc.id}::vcenter"] = ({"label": "已連線", "css": "allow"} if vc.status == "ok" and not vc.stale
                                       else {"label": "失敗", "css": "deny"})
        if not vc.data:
            continue
        for h in vc.data.hosts:
            if h["connection_state"] and h["connection_state"] != "connected":
                v = {"label": "失聯", "css": "deny"}
            elif h["maintenance"]:
                v = {"label": "維護", "css": "warn"}
            else:
                v = {"label": "連線", "css": "allow"}
            st[f"{vc.id}::{h['moid']}"] = v
        for d in vc.data.datastores:
            st[f"{vc.id}::{d['moid']}"] = ({"label": "可存取", "css": "allow"} if d["accessible"]
                                           else {"label": "無法存取", "css": "deny"})
        for m in vc.data.vms:
            if m["is_on"]:
                v = {"label": "ON", "css": "allow"}
            elif m["power_state"] == "suspended":
                v = {"label": "暫停", "css": "warn"}
            else:
                v = {"label": "OFF", "css": "muted"}
            st[f"{vc.id}::{m['moid']}"] = v
    return st


def vcenter_alarms(snap: Snapshot) -> list[dict]:
    """vCenter 內建已觸發告警(全部座合併,紅在前)。"""
    out: list[dict] = []
    for vc in snap.vcs.values():
        if vc.data:
            out.extend(dict(a, vc=vc.name, stale=vc.stale,
                            obj_key=f"{vc.id}::{a.get('entity_moid', '')}") for a in vc.data.alarms)
    out.sort(key=lambda a: (a["status"] != "red", a["vc"].lower(), a["entity_type"], a["entity_name"].lower()))
    return out


def dashboard_stats(snap: Snapshot) -> dict:
    hosts = snap.hosts()
    vms = snap.vms()
    dss = snap.datastores()
    vcs = list(snap.vcs.values())
    enabled = [v for v in vcs if v.enabled]

    on = sum(1 for v in vms if v["power_state"] == "poweredOn")
    suspended = sum(1 for v in vms if v["power_state"] == "suspended")
    off = len(vms) - on - suspended
    templates = sum(1 for v in vms if v["is_template"])

    cpu_total = sum(h["cpu_total_mhz"] for h in hosts)
    cpu_used = sum(h["cpu_usage_mhz"] for h in hosts)
    mem_total = sum(h["mem_total_mb"] for h in hosts)
    mem_used = sum(h["mem_usage_mb"] for h in hosts)
    ds_cap = sum(d["capacity_gb"] for d in dss)
    ds_used = sum(d["used_gb"] for d in dss)


    per_vc = []
    for vc in sorted(vcs, key=lambda x: x.name.lower()):
        d = vc.data
        per_vc.append({
            "id": vc.id, "name": vc.name, "host": vc.host, "enabled": vc.enabled,
            "status": vc.status, "stale": vc.stale, "error": vc.error, "about": vc.about,
            "polled_at": vc.polled_at, "ok_at": vc.ok_at, "duration_ms": vc.duration_ms,
            "hosts": len(d.hosts) if d else 0,
            "vms": len(d.vms) if d else 0,
            "vms_on": sum(1 for v in d.vms if v["is_on"]) if d else 0,
            "datastores": len(d.datastores) if d else 0,
        })

    top_cpu = sorted(hosts, key=lambda h: -h["cpu_pct"])[:8]
    top_mem = sorted(hosts, key=lambda h: -h["mem_pct"])[:8]
    top_ds = sorted(dss, key=lambda d: -d["used_pct"])[:8]

    return {
        "vc_total": len(enabled),
        "vc_ok": sum(1 for v in enabled if v.status == "ok" and not v.stale),
        "host_total": len(hosts),
        "host_connected": sum(1 for h in hosts if h["connection_state"] == "connected"),
        "vm_total": len(vms), "vm_on": on, "vm_off": off, "vm_suspended": suspended,
        "vm_templates": templates,
        "ds_total": len(dss),
        "cpu_pct": round(cpu_used / cpu_total * 100, 1) if cpu_total else 0.0,
        "cpu_used_ghz": round(cpu_used / 1000, 1), "cpu_total_ghz": round(cpu_total / 1000, 1),
        "mem_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0.0,
        "mem_used_gb": round(mem_used / 1024, 1), "mem_total_gb": round(mem_total / 1024, 1),
        "ds_pct": round(ds_used / ds_cap * 100, 1) if ds_cap else 0.0,
        "ds_used_gb": round(ds_used, 1), "ds_cap_gb": round(ds_cap, 1),
        "per_vc": per_vc, "vc_alarms": vcenter_alarms(snap),
        "top_cpu": top_cpu, "top_mem": top_mem, "top_ds": top_ds,
        "polled_at": snap.polled_at, "round_ms": snap.round_ms,
    }
