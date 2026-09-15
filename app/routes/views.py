"""四大檢視頁:運算 / 儲存 / 網路 / VM 總覽(+ CSV 匯出)。

每頁支援 `?partial=1` 只回面板片段,供前端自動更新時替換 #panel 內容
(搜尋框 / 主題 / 捲動位置留在頁面外殼,不被打斷)。
自動更新帶 `since=<上次快照 token>`:快照未變(輪詢尚未產生新資料、門檻未改)
直接回 204,不重新渲染也不傳輸;有變才回片段並以 X-Snapshot 標頭給新 token。
"""
from fastapi import APIRouter, Request, Response

from app import alerting, inventory
from app.inventory import (Snapshot, compute_view, group_by_vc, network_view, storage_view,
                           vm_rows)
from app.webutil import csv_response, render, render_partial

router = APIRouter()


def snapshot_token(snap: Snapshot) -> str:
    """快照識別:輪詢時間 + 警示規則指紋(規則改了卡片的 attention 也要重畫)。"""
    ts = snap.polled_at.isoformat(timespec="milliseconds") if snap.polled_at else "none"
    return f"{ts}|{alerting.rules_version()}"


def _page(request: Request, partial: bool, since: str, page: str, panel: str, **ctx):
    token = snapshot_token(inventory.current())
    if partial:
        if since and since == token:
            return Response(status_code=204, headers={"X-Snapshot": token})
        resp = render_partial(request, panel, **ctx)
        resp.headers["X-Snapshot"] = token
        return resp
    return render(request, page, page.replace(".html", ""), snapshot=token, **ctx)


@router.get("/compute")
async def compute(request: Request, partial: int = 0, since: str = ""):
    snap = inventory.current()
    return _page(request, bool(partial), since, "compute.html", "_panel_compute.html",
                 groups=group_by_vc(snap, compute_view(snap, alerting.active_index())),
                 polled_at=snap.polled_at)


@router.get("/storage")
async def storage(request: Request, partial: int = 0, since: str = ""):
    snap = inventory.current()
    return _page(request, bool(partial), since, "storage.html", "_panel_storage.html",
                 groups=group_by_vc(snap, storage_view(snap, alerting.active_index())),
                 polled_at=snap.polled_at)


@router.get("/network")
async def network(request: Request, partial: int = 0, since: str = ""):
    snap = inventory.current()
    return _page(request, bool(partial), since, "network.html", "_panel_network.html",
                 groups=group_by_vc(snap, network_view(snap)), polled_at=snap.polled_at)


@router.get("/vms")
async def vms(request: Request, partial: int = 0, since: str = ""):
    snap = inventory.current()
    return _page(request, bool(partial), since, "vms.html", "_panel_vms.html",
                 rows=vm_rows(snap, alerting.active_index()), polled_at=snap.polled_at)


@router.get("/vms/export.csv")
async def vms_export():
    rows = vm_rows(inventory.current())
    header = ["VM 名稱", "來源 vCenter", "電源狀態", "範本", "vCPU", "RAM (GB)",
              "IP 位址", "Network Group", "所在主機 (ESXi)", "叢集", "儲存區",
              "客體作業系統", "客體主機名稱", "Datastore 佔用 (GB)", "硬碟佈建量 (GB)",
              "Guest 已用 (GB)", "Guest 容量 (GB)", "Guest 最高用量 (%)", "快照數", "進行中警示"]
    power = {"poweredOn": "開機", "poweredOff": "關機", "suspended": "暫停"}
    data = [[r["name"], r["vc"], power.get(r["power_state"], r["power_state"]),
             "是" if r["is_template"] else "", r["num_cpu"], r["memory_gb"],
             "; ".join(r["ips"]), "; ".join(r["networks"]), r["host"], r["cluster"],
             "; ".join(r["datastores"]), r["guest_os"], r["hostname"],
             r["disk_committed_gb"], r["disk_provisioned_gb"],
             r["guest_used_gb"] if r["guest_used_gb"] is not None else "",
             r["guest_capacity_gb"] if r["guest_capacity_gb"] is not None else "",
             r["guest_disk_max_pct"] if r["guest_disk_max_pct"] is not None else "",
             r["snapshot_count"],
             "; ".join(f"{a['label']} {a['value']}" for a in r["alerts"])] for r in rows]
    return csv_response("VM總覽.csv", header, data)
