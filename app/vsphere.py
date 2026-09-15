"""vCenter 資料收集(vSphere Web Services API / pyVmomi,唯讀)。

- 以 PropertyCollector 批次讀取(ContainerView + TraversalSpec),一次往返
  取回整類物件的指定屬性;舊版逐一存取 host.vm / vm.summary 每個屬性都是
  一次 SOAP 往返,數百台 VM 時一輪要數十秒。
- MoRef 之間的關聯(VM→主機 / VM→儲存區 / NIC→網路 / 主機→叢集)一律用
  各類物件的 `_moId` 建索引後在本機對應,不回頭向 vCenter 查名稱。
- 每座 vCenter 一個 VCenterClient,連線在輪詢間保持(session 存活);
  抓取失敗即 Disconnect,下一輪重新 SmartConnect。
- httpConnectionTimeout:預設無限等待,vCenter 黑洞會卡死整輪輪詢。
"""
from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pyVim.connect import Disconnect, SmartConnect
from pyVmomi import vim, vmodl

from app.config import to_local

logger = logging.getLogger("vcod.vsphere")

CONNECT_TIMEOUT = 30

_HOST_PROPS = [
    "name", "parent", "overallStatus", "triggeredAlarmState", "configIssue",
    "runtime.healthSystemRuntime.hardwareStatusInfo",
    "runtime.healthSystemRuntime.systemHealthInfo",
    "summary.hardware.cpuMhz", "summary.hardware.numCpuCores",
    "summary.hardware.numCpuThreads", "summary.hardware.memorySize",
    "summary.hardware.model", "summary.hardware.cpuModel",
    "summary.quickStats.overallCpuUsage", "summary.quickStats.overallMemoryUsage",
    "summary.quickStats.uptime",
    "summary.runtime.connectionState", "summary.runtime.powerState",
    "summary.runtime.inMaintenanceMode",
    "summary.config.product.fullName",
]
_VM_PROPS = [
    "name", "runtime.powerState", "runtime.host", "runtime.maxCpuUsage", "datastore",
    "triggeredAlarmState", "snapshot",
    "config.template", "config.hardware.numCPU", "config.hardware.memoryMB",
    "config.hardware.device", "config.guestFullName", "config.files.vmPathName",
    "summary.quickStats.overallCpuUsage", "summary.quickStats.guestMemoryUsage",
    "summary.quickStats.overallCpuReadiness", "summary.quickStats.balloonedMemory",
    "summary.quickStats.swappedMemory", "summary.quickStats.compressedMemory",
    "summary.quickStats.guestHeartbeatStatus",
    "summary.storage.committed", "summary.storage.uncommitted",
    "guest.ipAddress", "guest.net", "guest.hostName", "guest.toolsRunningStatus",
    "guest.toolsVersionStatus2", "guest.toolsVersion",
    "guest.disk", "guest.guestKernelCrashed",
]

# Tools 版本狀態(GuestInfo.toolsVersionStatus2)中屬「應更新」者;對應 vCenter 摘要頁的
# 「此虛擬機器可使用較新版本的 VMware Tools」等提示
TOOLS_OUTDATED = {"guestToolsNeedUpgrade", "guestToolsTooOld", "guestToolsSupportedOld",
                  "guestToolsBlacklisted"}
_DS_PROPS = [
    "name", "summary.capacity", "summary.freeSpace", "summary.uncommitted",
    "summary.type", "summary.accessible", "host", "triggeredAlarmState",
]

# Guest 檔案系統中不具容量意義的型別(唯讀媒體 / 記憶體檔案系統 / 網路掛載),不列入用量判定
_SKIP_FS_TYPES = {"tmpfs", "devtmpfs", "squashfs", "iso9660", "cdfs", "udf", "overlay",
                  "proc", "sysfs", "devfs", "autofs", "nfs", "nfs4", "cifs", "smbfs"}
_MIN_FS_BYTES = 1024 ** 3   # < 1 GB 的檔案系統(/boot/efi 之類)略過

# 網卡型別 → 顯示名稱(未列者以類別名去 Virtual 前綴)
_ADAPTER_LABELS = (
    (vim.vm.device.VirtualVmxnet3Vrdma, "VMXNET 3 (RDMA)"),
    (vim.vm.device.VirtualVmxnet3, "VMXNET 3"),
    (vim.vm.device.VirtualVmxnet2, "VMXNET 2"),
    (vim.vm.device.VirtualVmxnet, "VMXNET"),
    (vim.vm.device.VirtualE1000e, "E1000e"),
    (vim.vm.device.VirtualE1000, "E1000"),
    (vim.vm.device.VirtualPCNet32, "PCNet32"),
    (vim.vm.device.VirtualSriovEthernetCard, "SR-IOV passthrough"),
)


@dataclass
class VcData:
    """單座 vCenter 一輪抓取結果(純 dict / list,不含 pyVmomi 物件)。"""
    about: str = ""
    hosts: list[dict] = field(default_factory=list)
    vms: list[dict] = field(default_factory=list)
    datastores: list[dict] = field(default_factory=list)
    networks: list[dict] = field(default_factory=list)   # 所有 Port Group(含無 VM 者)
    alarms: list[dict] = field(default_factory=list)     # vCenter 已觸發的內建告警(主機 / VM / 儲存區)


def _ssl_context(verify: bool):
    if verify:
        return None  # pyVmomi 預設:驗證 CA 與主機名
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _moid(ref) -> str:
    try:
        return ref._moId if ref is not None else ""
    except AttributeError:
        return ""


def _collect(content, vim_type, props: list[str]) -> list[dict]:
    """ContainerView + PropertyCollector 批次取回 [{'_obj': MoRef, prop: val}]。

    缺失屬性(如無法存取的 VM 沒有 config)不在 dict 內,呼叫端用 .get。
    """
    view = content.viewManager.CreateContainerView(content.rootFolder, [vim_type], True)
    try:
        pc_ns = vmodl.query.PropertyCollector
        traversal = pc_ns.TraversalSpec(name="tv", path="view", skip=False,
                                        type=vim.view.ContainerView)
        obj_spec = pc_ns.ObjectSpec(obj=view, skip=True, selectSet=[traversal])
        prop_spec = pc_ns.PropertySpec(type=vim_type, pathSet=props, all=False)
        fspec = pc_ns.FilterSpec(objectSet=[obj_spec], propSet=[prop_spec])
        pc = content.propertyCollector
        result = pc.RetrievePropertiesEx([fspec], pc_ns.RetrieveOptions())
        out: list[dict] = []
        while result is not None:
            for o in result.objects:
                d: dict[str, Any] = {"_obj": o.obj}
                for p in o.propSet or []:
                    d[p.name] = p.val
                out.append(d)
            if result.token:
                result = pc.ContinueRetrievePropertiesEx(result.token)
            else:
                break
        return out
    finally:
        try:
            view.Destroy()
        except Exception:  # noqa: BLE001
            pass


def _collect_objs(content, refs: list, vim_type, props: list[str]) -> list[dict]:
    """對指定 MoRef 清單批次讀屬性(非 ManagedEntity 的物件如 Alarm 無法用 ContainerView)。"""
    if not refs:
        return []
    pc_ns = vmodl.query.PropertyCollector
    obj_specs = [pc_ns.ObjectSpec(obj=r, skip=False) for r in refs]
    prop_spec = pc_ns.PropertySpec(type=vim_type, pathSet=props, all=False)
    fspec = pc_ns.FilterSpec(objectSet=obj_specs, propSet=[prop_spec])
    pc = content.propertyCollector
    result = pc.RetrievePropertiesEx([fspec], pc_ns.RetrieveOptions())
    out: list[dict] = []
    while result is not None:
        for o in result.objects:
            d: dict[str, Any] = {"_obj": o.obj}
            for pr in o.propSet or []:
                d[pr.name] = pr.val
            out.append(d)
        if result.token:
            result = pc.ContinueRetrievePropertiesEx(result.token)
        else:
            break
    return out


def _alarm_rows(raw: dict, entity_type: str, entity_name: str,
                alarm_names: dict[str, str]) -> list[dict]:
    """triggeredAlarmState → 純 dict 列(只取黃 / 紅、未停用者)。"""
    rows = []
    for st in raw.get("triggeredAlarmState") or []:
        status = str(getattr(st, "overallStatus", "") or "")
        if status not in ("yellow", "red") or getattr(st, "disabled", False):
            continue
        t = getattr(st, "time", None)
        if isinstance(t, datetime):
            t = to_local(t)   # settings.timezone,勿用 astimezone()(系統時區)
        amid = _moid(getattr(st, "alarm", None))
        rows.append({
            "entity_type": entity_type, "entity_name": entity_name,
            "entity_moid": _moid(raw.get("_obj")),
            "alarm_moid": amid,
            "alarm": alarm_names.get(amid, "") or "(未知告警)",
            "status": status,
            "acknowledged": bool(getattr(st, "acknowledged", False)),
            "time": t,
        })
    return rows


def _host_health_issues(raw: dict) -> list[str]:
    """主機 overallStatus 非 green 的具體原因(硬體狀態 / 感測器 / 組態問題),不含告警
    (告警另由 _alarm_rows 產生後於 _fetch 併入)。回傳 ['[red] 名稱', '[yellow] 名稱', …]。"""
    out: list[str] = []
    hw = raw.get("runtime.healthSystemRuntime.hardwareStatusInfo")
    for attr in ("cpuStatusInfo", "memoryStatusInfo", "storageStatusInfo"):
        for it in (getattr(hw, attr, None) or []) if hw else []:
            key = str(getattr(getattr(it, "status", None), "key", "") or "").lower()
            if key and key not in ("green", "unknown"):
                out.append(f"[{key}] {getattr(it, 'name', '') or attr}")
    sh = raw.get("runtime.healthSystemRuntime.systemHealthInfo")
    for sen in (getattr(sh, "numericSensorInfo", None) or []) if sh else []:
        key = str(getattr(getattr(sen, "healthState", None), "key", "") or "").lower()
        if key and key not in ("green", "unknown"):
            out.append(f"[{key}] {getattr(sen, 'name', '') or '感測器'}")
    for ev in raw.get("configIssue") or []:
        msg = getattr(ev, "fullFormattedMessage", "") or type(ev).__name__.rsplit(".", 1)[-1]
        out.append(f"[組態] {msg}")
    # 去重、保留順序
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _snapshot_stats(snap_info, now_utc: datetime) -> tuple[int, float]:
    """VirtualMachineSnapshotInfo → (快照數, 最舊快照天數)。"""
    if snap_info is None:
        return 0, 0.0
    count, oldest = 0, None
    stack = list(getattr(snap_info, "rootSnapshotList", None) or [])
    while stack:
        node = stack.pop()
        count += 1
        ct = getattr(node, "createTime", None)
        if isinstance(ct, datetime):
            if ct.tzinfo is None:
                ct = ct.replace(tzinfo=timezone.utc)
            if oldest is None or ct < oldest:
                oldest = ct
        stack.extend(getattr(node, "childSnapshotList", None) or [])
    days = round((now_utc - oldest).total_seconds() / 86400, 1) if oldest else 0.0
    return count, max(days, 0.0)


def _guest_disks(raw_list) -> list[dict]:
    """guest.disk(VMware Tools 回報的檔案系統)→ [{path, fs, capacity_gb, used_gb, free_gb, pct}]。"""
    out = []
    for g in raw_list or []:
        cap = int(getattr(g, "capacity", 0) or 0)
        free = int(getattr(g, "freeSpace", 0) or 0)
        fs = (getattr(g, "filesystemType", None) or "").lower()
        if cap < _MIN_FS_BYTES or fs in _SKIP_FS_TYPES:
            continue
        used = max(cap - free, 0)
        out.append({
            "path": getattr(g, "diskPath", "") or "",
            "fs": fs,
            "capacity_gb": round(cap / 1024 ** 3, 1),
            "used_gb": round(used / 1024 ** 3, 1),
            "free_gb": round(free / 1024 ** 3, 1),
            "pct": round(used / cap * 100, 1) if cap else 0.0,
        })
    out.sort(key=lambda d: -d["pct"])
    return out


def _adapter_label(device) -> str:
    for cls, label in _ADAPTER_LABELS:
        if isinstance(device, cls):
            return label
    return type(device).__name__.rsplit(".", 1)[-1].replace("Virtual", "")


def _nic_network(backing, net_names: dict, dvpg_by_key: dict, opaque_by_id: dict) -> str:
    """網卡 backing → Port Group 名稱(標準 / 分散式 / NSX Opaque 三種)。"""
    if backing is None:
        return ""
    eth = vim.vm.device.VirtualEthernetCard
    if isinstance(backing, eth.DistributedVirtualPortBackingInfo):
        key = getattr(getattr(backing, "port", None), "portgroupKey", "") or ""
        return dvpg_by_key.get(key, "") or (f"dvPortgroup {key}" if key else "")
    if isinstance(backing, eth.OpaqueNetworkBackingInfo):
        oid = getattr(backing, "opaqueNetworkId", "") or ""
        return opaque_by_id.get(oid, "") or oid
    if isinstance(backing, eth.NetworkBackingInfo):
        name = net_names.get(_moid(getattr(backing, "network", None)), "")
        return name or (getattr(backing, "deviceName", "") or "")
    return getattr(backing, "deviceName", "") or ""


def _is_usable_ip(ip: str) -> bool:
    return bool(ip) and ":" not in ip and not ip.startswith("169.254.")


def _parse_vm(raw: dict, host_names: dict, cluster_of_host: dict, ds_names: dict,
              net_names: dict, dvpg_by_key: dict, opaque_by_id: dict,
              now_utc: datetime | None = None) -> dict:
    moid = _moid(raw["_obj"])
    name = raw.get("name") or moid
    power = str(raw.get("runtime.powerState") or "unknown")
    host_moid = _moid(raw.get("runtime.host"))
    now_utc = now_utc or datetime.now(timezone.utc)

    guest_by_mac: dict[str, list[str]] = {}
    for g in raw.get("guest.net") or []:
        mac = (getattr(g, "macAddress", "") or "").lower()
        if mac:
            guest_by_mac[mac] = [ip for ip in (getattr(g, "ipAddress", None) or []) if ip]

    nics: list[dict] = []
    vmdks: list[str] = []
    isos: list[dict] = []      # 光碟機掛載的 datastore ISO(ISO 所在儲存區也會算進 VM.datastore)
    provisioned_kb = 0
    for dev in raw.get("config.hardware.device") or []:
        if isinstance(dev, vim.vm.device.VirtualEthernetCard):
            mac = (getattr(dev, "macAddress", "") or "").lower()
            info = getattr(dev, "deviceInfo", None)
            conn = getattr(dev, "connectable", None)
            nics.append({
                "label": (getattr(info, "label", "") or "") if info else "",
                "adapter_type": _adapter_label(dev),
                "mac": mac,
                "network": _nic_network(getattr(dev, "backing", None), net_names,
                                        dvpg_by_key, opaque_by_id),
                "passthrough": isinstance(dev, vim.vm.device.VirtualSriovEthernetCard),
                "connected": bool(getattr(conn, "connected", False)) if conn else False,
                "ips": guest_by_mac.get(mac, []),
            })
        elif isinstance(dev, vim.vm.device.VirtualDisk):
            fn = getattr(getattr(dev, "backing", None), "fileName", "") or ""
            if fn:
                vmdks.append(fn)
            provisioned_kb += int(getattr(dev, "capacityInKB", 0) or 0)
        elif isinstance(dev, vim.vm.device.VirtualCdrom):
            backing = getattr(dev, "backing", None)
            if isinstance(backing, vim.vm.device.VirtualCdrom.IsoBackingInfo):
                fn = getattr(backing, "fileName", "") or ""
                if fn:
                    conn = getattr(dev, "connectable", None)
                    isos.append({"path": fn,
                                 "connected": bool(getattr(conn, "connected", False)) if conn else False})

    ips: list[str] = []
    for nic in nics:
        for ip in nic["ips"]:
            if _is_usable_ip(ip) and ip not in ips:
                ips.append(ip)
    primary_ip = raw.get("guest.ipAddress") or ""
    if not ips and _is_usable_ip(primary_ip):
        ips = [primary_ip]

    networks: list[str] = []
    for nic in nics:
        if nic["network"] and nic["network"] not in networks:
            networks.append(nic["network"])

    datastores = [ds_names.get(_moid(d), "") for d in (raw.get("datastore") or [])]
    datastores = [d for d in datastores if d]
    if not datastores:
        path = raw.get("config.files.vmPathName") or ""
        if path.startswith("[") and "]" in path:
            datastores = [path[1:path.index("]")]]

    committed = int(raw.get("summary.storage.committed") or 0)
    cpu_used = int(raw.get("summary.quickStats.overallCpuUsage") or 0)
    cpu_max = int(raw.get("runtime.maxCpuUsage") or 0)
    snap_count, snap_days = _snapshot_stats(raw.get("snapshot"), now_utc)
    guest_disks = _guest_disks(raw.get("guest.disk"))
    return {
        "moid": moid, "name": name, "power_state": power,
        "is_on": power == "poweredOn",
        # ---- 平台層警示用 ----
        # maxCpuUsage 以主機標稱時脈計,Turbo Boost 時實際 MHz 可超過 → 封頂 100%,
        # 另以 cpu_turbo 標記(vSphere Client 也會顯示 >100%,這是 VMware 的計法)
        "cpu_max_mhz": cpu_max,
        "cpu_pct": round(min(cpu_used / cpu_max, 1.0) * 100, 1) if cpu_max else 0.0,
        "cpu_turbo": bool(cpu_max and cpu_used > cpu_max),
        "ready_pct": round(float(raw.get("summary.quickStats.overallCpuReadiness") or 0), 1),
        "ballooned_mb": int(raw.get("summary.quickStats.balloonedMemory") or 0),
        "swapped_mb": int(raw.get("summary.quickStats.swappedMemory") or 0),
        "compressed_mb": int(raw.get("summary.quickStats.compressedMemory") or 0),
        "snapshot_count": snap_count, "snapshot_oldest_days": snap_days,
        # ---- 客體層(VMware Tools)----
        "heartbeat": str(raw.get("summary.quickStats.guestHeartbeatStatus") or "gray"),
        "kernel_crashed": bool(raw.get("guest.guestKernelCrashed") or False),
        "guest_disks": guest_disks,
        "guest_disk_max_pct": guest_disks[0]["pct"] if guest_disks else None,
        "guest_used_gb": round(sum(d["used_gb"] for d in guest_disks), 1) if guest_disks else None,
        "guest_capacity_gb": round(sum(d["capacity_gb"] for d in guest_disks), 1) if guest_disks else None,
        "is_template": bool(raw.get("config.template") or False),
        "host": host_names.get(host_moid, ""), "host_moid": host_moid,
        "cluster": cluster_of_host.get(host_moid, ""),
        "num_cpu": int(raw.get("config.hardware.numCPU") or 0),
        "memory_mb": int(raw.get("config.hardware.memoryMB") or 0),
        "cpu_usage_mhz": cpu_used,
        "mem_usage_mb": int(raw.get("summary.quickStats.guestMemoryUsage") or 0),
        "guest_os": raw.get("config.guestFullName") or "",
        "hostname": raw.get("guest.hostName") or "",
        "tools_running": (raw.get("guest.toolsRunningStatus") or "") == "guestToolsRunning",
        "tools_status": str(raw.get("guest.toolsVersionStatus2") or ""),   # guestToolsCurrent / NeedUpgrade / …
        "tools_version": str(raw.get("guest.toolsVersion") or ""),
        "tools_outdated": str(raw.get("guest.toolsVersionStatus2") or "") in TOOLS_OUTDATED,
        "primary_ip": primary_ip if _is_usable_ip(primary_ip) else (ips[0] if ips else ""),
        "ips": ips, "nics": nics, "networks": networks,
        "datastores": datastores, "vmdks": vmdks, "isos": isos,
        "disk_committed_gb": round(committed / 1024 ** 3, 1),
        "disk_provisioned_gb": round(provisioned_kb / 1024 ** 2, 1),
    }


def _parse_host(raw: dict, cluster_names: dict) -> dict:
    moid = _moid(raw["_obj"])
    cpu_mhz = int(raw.get("summary.hardware.cpuMhz") or 0)
    cores = int(raw.get("summary.hardware.numCpuCores") or 0)
    cpu_total = cpu_mhz * cores
    mem_total_mb = int(raw.get("summary.hardware.memorySize") or 0) // (1024 * 1024)
    cpu_used = int(raw.get("summary.quickStats.overallCpuUsage") or 0)
    mem_used = int(raw.get("summary.quickStats.overallMemoryUsage") or 0)
    return {
        "moid": moid, "name": raw.get("name") or moid,
        "cluster": cluster_names.get(_moid(raw.get("parent")), ""),
        "connection_state": str(raw.get("summary.runtime.connectionState") or ""),
        "power_state": str(raw.get("summary.runtime.powerState") or ""),
        "maintenance": bool(raw.get("summary.runtime.inMaintenanceMode") or False),
        "overall_status": str(raw.get("overallStatus") or "gray"),   # gray/green/yellow/red(硬體 / 告警彙總)
        "health_issues": _host_health_issues(raw),                     # 非 green 的硬體 / 感測器 / 組態原因
        "version": raw.get("summary.config.product.fullName") or "",
        "model": raw.get("summary.hardware.model") or "",
        "cpu_model": raw.get("summary.hardware.cpuModel") or "",
        "num_cores": cores,
        "num_threads": int(raw.get("summary.hardware.numCpuThreads") or 0),
        "uptime_days": int(raw.get("summary.quickStats.uptime") or 0) // 86400,
        "cpu_usage_mhz": cpu_used, "cpu_total_mhz": cpu_total,
        "cpu_pct": round(cpu_used / cpu_total * 100, 1) if cpu_total else 0.0,
        "mem_usage_mb": mem_used, "mem_total_mb": mem_total_mb,
        "mem_pct": round(mem_used / mem_total_mb * 100, 1) if mem_total_mb else 0.0,
    }


def _parse_datastore(raw: dict) -> dict:
    moid = _moid(raw["_obj"])
    cap = int(raw.get("summary.capacity") or 0)
    free = int(raw.get("summary.freeSpace") or 0)
    uncommitted = int(raw.get("summary.uncommitted") or 0)
    used = max(cap - free, 0)
    return {
        "moid": moid, "name": raw.get("name") or moid,
        "type": raw.get("summary.type") or "",
        "accessible": bool(raw.get("summary.accessible") if raw.get("summary.accessible") is not None else True),
        "capacity_gb": round(cap / 1024 ** 3, 1),
        "free_gb": round(free / 1024 ** 3, 1),
        "used_gb": round(used / 1024 ** 3, 1),
        "provisioned_gb": round((used + uncommitted) / 1024 ** 3, 1),
        "used_pct": round(used / cap * 100, 1) if cap else 0.0,
        "host_count": len(raw.get("host") or []),
    }


class VCenterClient:
    """單座 vCenter 的長連線 + 一輪抓取。所有方法皆為同步阻塞,請在執行緒內呼叫。"""

    def __init__(self, host: str, username: str, password: str,
                 port: int = 443, verify_ssl: bool = True):
        self.host = host
        self.port = port or 443
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self._si = None
        self._alarm_names: dict[str, str] = {}   # alarm moid → 名稱(定義極少變動,跨輪快取)

    # ---- 連線 ----
    def connect(self) -> None:
        self.disconnect()
        self._si = SmartConnect(host=self.host, port=self.port, user=self.username,
                                pwd=self.password, sslContext=_ssl_context(self.verify_ssl),
                                httpConnectionTimeout=CONNECT_TIMEOUT)

    def disconnect(self) -> None:
        if self._si is not None:
            try:
                Disconnect(self._si)
            except Exception:  # noqa: BLE001
                pass
            self._si = None

    @property
    def connected(self) -> bool:
        return self._si is not None

    # ---- 抓取 ----
    def fetch(self) -> VcData:
        """抓取一輪;連線遺失 / session 過期時重連一次再試。"""
        if self._si is None:
            self.connect()
        try:
            return self._fetch(self._si.RetrieveContent())
        except vim.fault.InvalidLogin:
            raise   # 帳密錯誤:重試無意義,直接回報
        except Exception as exc:  # noqa: BLE001 —— 含 NotAuthenticated(session 過期 / vCenter 重啟)與網路中斷,重連重試一次
            logger.info("[%s] 抓取失敗(%s),重新連線後重試", self.host, type(exc).__name__)
            self.connect()
            return self._fetch(self._si.RetrieveContent())

    def _alarm_name_map(self, content, raws: list[dict]) -> dict[str, str]:
        """把本輪出現、尚未快取的 alarm MoRef 一次查名稱(通常只在首輪或新告警定義時發生)。"""
        unknown = {}
        for r in raws:
            for st in r.get("triggeredAlarmState") or []:
                ref = getattr(st, "alarm", None)
                mid = _moid(ref)
                if mid and mid not in self._alarm_names:
                    unknown[mid] = ref
        if unknown:
            try:
                for a in _collect_objs(content, list(unknown.values()), vim.alarm.Alarm, ["info.name"]):
                    self._alarm_names[_moid(a["_obj"])] = str(a.get("info.name") or "")
            except Exception as exc:  # noqa: BLE001 —— 名稱查不到不影響主流程
                logger.debug("[%s] 讀取告警定義名稱失敗:%s", self.host, exc)
        return self._alarm_names

    def _fetch(self, content) -> VcData:
        t0 = time.monotonic()
        data = VcData(about=getattr(content.about, "fullName", "") or "")
        now_utc = datetime.now(timezone.utc)

        clusters = {_moid(c["_obj"]): c.get("name", "")
                    for c in _collect(content, vim.ComputeResource, ["name"])}
        ds_raw = _collect(content, vim.Datastore, _DS_PROPS)
        ds_names = {_moid(d["_obj"]): d.get("name", "") for d in ds_raw}
        net_names = {_moid(n["_obj"]): n.get("name", "")
                     for n in _collect(content, vim.Network, ["name"])}
        dvpg_by_key = {p.get("key", ""): p.get("name", "")
                       for p in _collect(content, vim.dvs.DistributedVirtualPortgroup,
                                         ["name", "key"])}
        opaque_by_id = {}
        try:
            opaque_by_id = {o.get("summary.opaqueNetworkId", ""): o.get("name", "")
                            for o in _collect(content, vim.OpaqueNetwork,
                                              ["name", "summary.opaqueNetworkId"])}
        except Exception:  # noqa: BLE001 —— 舊版 vCenter 無此型別
            pass

        hosts_raw = _collect(content, vim.HostSystem, _HOST_PROPS)
        data.hosts = [_parse_host(h, clusters) for h in hosts_raw]
        host_names = {h["moid"]: h["name"] for h in data.hosts}
        cluster_of_host = {h["moid"]: h["cluster"] for h in data.hosts}

        vms_raw = _collect(content, vim.VirtualMachine, _VM_PROPS)
        data.vms = [_parse_vm(v, host_names, cluster_of_host, ds_names,
                              net_names, dvpg_by_key, opaque_by_id, now_utc) for v in vms_raw]
        data.datastores = [_parse_datastore(d) for d in ds_raw]
        data.networks = [{"moid": k, "name": v} for k, v in net_names.items() if v]

        # vCenter 內建告警(已觸發、黃 / 紅):主機 / VM / 儲存區三類
        names = self._alarm_name_map(content, hosts_raw + vms_raw + ds_raw)
        for raw, etype in ((hosts_raw, "主機"), (vms_raw, "VM"), (ds_raw, "儲存區")):
            for r in raw:
                data.alarms.extend(_alarm_rows(r, etype, r.get("name") or _moid(r["_obj"]), names))
        data.alarms.sort(key=lambda a: (a["status"] != "red", a["entity_type"], a["entity_name"]))
        # 主機健康原因:已觸發告警排前面,再接硬體 / 感測器 / 組態
        alarm_by_host: dict[str, list[str]] = {}
        for a in data.alarms:
            if a["entity_type"] == "主機":
                alarm_by_host.setdefault(a["entity_moid"], []).append(f"[{a['status']}] {a['alarm']}")
        for h in data.hosts:
            h["health_issues"] = alarm_by_host.get(h["moid"], []) + h["health_issues"]

        logger.debug("[%s] 抓取完成:%d 主機 / %d VM / %d 儲存區,%.1fs",
                     self.host, len(data.hosts), len(data.vms), len(data.datastores),
                     time.monotonic() - t0)
        return data


def test_connection(host: str, username: str, password: str,
                    port: int = 443, verify_ssl: bool = True) -> tuple[bool, str]:
    """連線測試:登入 → 讀版本與主機/VM 數 → 登出。回 (成功, 訊息)。"""
    si = None
    try:
        si = SmartConnect(host=host, port=port or 443, user=username, pwd=password,
                          sslContext=_ssl_context(verify_ssl),
                          httpConnectionTimeout=CONNECT_TIMEOUT)
        content = si.RetrieveContent()
        n_hosts = len(_collect(content, vim.HostSystem, ["name"]))
        n_vms = len(_collect(content, vim.VirtualMachine, ["name"]))
        return True, (f"連線成功:{content.about.fullName};"
                      f"{n_hosts} 台主機、{n_vms} 台 VM")
    except vim.fault.InvalidLogin:
        return False, "帳號或密碼錯誤(需具備 vCenter 唯讀以上權限)"
    except ssl.SSLError as exc:
        return False, (f"TLS 憑證驗證失敗:{getattr(exc, 'reason', None) or exc};"
                       "若為自簽憑證請取消勾選「驗證 TLS 憑證」")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) or type(exc).__name__
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            return False, "TLS 憑證驗證失敗;若為自簽憑證請取消勾選「驗證 TLS 憑證」"
        return False, f"連線失敗:{type(exc).__name__}: {msg[:200]}"
    finally:
        if si is not None:
            try:
                Disconnect(si)
            except Exception:  # noqa: BLE001
                pass
