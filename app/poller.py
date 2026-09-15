"""背景輪詢器:依 settings.poll_interval_seconds 抓取所有啟用的 vCenter。

- 各 vCenter 在執行緒內並行抓取(pyVmomi 為同步阻塞 I/O),整輪等待全部完成
  後**一次性**發布新快照(inventory.publish),讀取端不會看到半更新狀態。
- 單座失敗:保留該座上一輪資料並標 stale,連線狀態 / 錯誤寫回 DB
  (vcenters.last_*)供管理頁與儀表板呈現;狀態轉變時記 INFO,穩定時不吵。
- 設定變更(新增 / 編輯 / 刪除 vCenter、按「立即更新」)呼叫 request_poll()
  喚醒迴圈立刻跑一輪,不必等下一個間隔。
- 快照發布後於執行緒內執行 alerting.run_round()(評估 / 去抖 / 落地 / 通知),
  再處理 vCenter 內建告警外送;稽核與警示歷史依 log_retention_days 每日清理一次。
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from app import alerting, inventory
from app.config import POLL_MAX, POLL_MIN, local_now, settings
from app.database import SessionLocal
from app.inventory import Snapshot, VcState
from app.models import AuditLog, VCenter
from app.vsphere import VCenterClient

logger = logging.getLogger("vcod.poller")

_clients: dict[int, VCenterClient] = {}
_wake = asyncio.Event()
_loop: asyncio.AbstractEventLoop | None = None   # poller_loop 所在 loop,供跨執行緒喚醒
_last_cleanup: float = 0.0
_vc_alarm_keys: set[str] = set()   # 上一輪 vCenter 內建告警 key(判斷新出現者)


def request_poll() -> None:
    """喚醒輪詢迴圈立刻執行一輪(設定變更 / 手動更新)。可自任何執行緒呼叫:
    同步路由在 threadpool 內執行,asyncio.Event 非執行緒安全,跨執行緒走 call_soon_threadsafe。"""
    loop = _loop
    if loop is None or loop.is_closed():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is loop:
        _wake.set()
    else:
        loop.call_soon_threadsafe(_wake.set)


def drop_client(vc_id: int) -> None:
    """vCenter 被刪除 / 連線設定變更:關閉舊連線,下輪重建。"""
    c = _clients.pop(vc_id, None)
    if c is not None:
        c.disconnect()


def _client_for(vc: VCenter) -> VCenterClient:
    c = _clients.get(vc.id)
    if (c is None or c.host != vc.host or c.port != (vc.port or 443)
            or c.username != vc.username or c.password != vc.password
            or c.verify_ssl != bool(vc.verify_ssl)):
        drop_client(vc.id)
        c = VCenterClient(vc.host, vc.username, vc.password,
                          port=vc.port or 443, verify_ssl=bool(vc.verify_ssl))
        _clients[vc.id] = c
    return c


def _poll_one(vc: VCenter, prev: VcState | None) -> VcState:
    """(執行緒內)抓取單座 vCenter;失敗沿用 prev 的資料並標 stale。"""
    state = VcState(id=vc.id, name=vc.name, host=vc.host, enabled=True)
    t0 = time.monotonic()
    client = _client_for(vc)
    try:
        data = client.fetch()
        state.data = data
        state.about = data.about
        state.status = "ok"
        state.ok_at = local_now()
    except Exception as exc:  # noqa: BLE001
        client.disconnect()
        msg = str(exc) or type(exc).__name__
        if "CERTIFICATE_VERIFY_FAILED" in msg:
            msg = "TLS 憑證驗證失敗(自簽憑證請於編輯頁取消「驗證 TLS 憑證」)"
        elif type(exc).__name__ == "InvalidLogin":
            msg = "帳號或密碼錯誤"
        state.status = "failed"
        state.error = f"{type(exc).__name__}: {msg}"[:500]
        if prev is not None and prev.data is not None:
            state.data = prev.data
            state.about = prev.about
            state.ok_at = prev.ok_at
            state.stale = True
    state.polled_at = local_now()
    state.duration_ms = int((time.monotonic() - t0) * 1000)
    return state


def _write_status(states: list[VcState]) -> None:
    """輪詢結果寫回 vcenters.last_*(獨立 session,失敗只記 log)。"""
    try:
        with SessionLocal() as db:
            for st in states:
                vc = db.get(VCenter, st.id)
                if vc is None:
                    continue
                vc.last_status = st.status
                vc.last_error = st.error if st.status == "failed" else ""
                vc.last_polled_at = st.polled_at
                vc.last_duration_ms = st.duration_ms
                if st.status == "ok":
                    vc.last_ok_at = st.ok_at
                    vc.version = (st.about or "")[:120]
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("寫回 vCenter 輪詢狀態失敗")


async def run_round() -> Snapshot:
    """抓取所有啟用的 vCenter 並發布快照;回傳新快照。"""
    t0 = time.monotonic()
    prev = inventory.current()
    with SessionLocal() as db:
        vcs = db.query(VCenter).order_by(VCenter.name).all()
    active_ids = {vc.id for vc in vcs}
    for stale_id in [i for i in _clients if i not in active_ids]:
        drop_client(stale_id)

    snap = Snapshot()
    tasks = []
    for vc in vcs:
        if not vc.enabled:
            drop_client(vc.id)
            snap.vcs[vc.id] = VcState(id=vc.id, name=vc.name, host=vc.host,
                                      enabled=False, status="disabled")
            continue
        tasks.append(asyncio.to_thread(_poll_one, vc, prev.vcs.get(vc.id)))
    states: list[VcState] = list(await asyncio.gather(*tasks)) if tasks else []
    for st in states:
        snap.vcs[st.id] = st
        old = prev.vcs.get(st.id)
        if old is None or old.status != st.status:
            if st.status == "ok":
                logger.info("[%s] 已連線:%s", st.name, st.about)
            else:
                logger.warning("[%s] 連線失敗:%s", st.name, st.error)
    snap.polled_at = local_now()
    snap.round_ms = int((time.monotonic() - t0) * 1000)
    inventory.publish(snap)
    await asyncio.to_thread(_write_status, states)
    await asyncio.to_thread(_alerts_round, snap)
    logger.debug("輪詢完成:%d 座 vCenter,%d ms", len(states), snap.round_ms)
    return snap


def _alerts_round(snap: Snapshot) -> None:
    """(執行緒內)警示評估 + vCenter 內建告警外送;任何錯誤不得影響輪詢。"""
    global _vc_alarm_keys
    try:
        res = alerting.run_round(snap)
        if res.transitions:
            logger.info("警示轉態 %d 筆(進行中 %d):%s", len(res.transitions), res.active,
                        "、".join(f"{t['event']}:{t['target_name']}/{t['rule']}" for t in res.transitions[:8]))
    except Exception:  # noqa: BLE001
        logger.exception("警示評估失敗")
    try:
        _vc_alarm_keys = alerting.forward_vcenter_alarms(snap, _vc_alarm_keys)
    except Exception:  # noqa: BLE001
        logger.exception("vCenter 內建告警處理失敗")


def _cleanup_audit() -> None:
    days = settings.log_retention_days
    if days <= 0:
        return
    try:
        with SessionLocal() as db:
            cutoff = local_now() - timedelta(days=days)
            n = db.query(AuditLog).filter(AuditLog.ts < cutoff).delete()
            db.commit()
        if n:
            logger.info("已清理 %d 筆逾 %d 天的稽核紀錄", n, days)
        m = alerting.cleanup_history(days)
        if m:
            logger.info("已清理 %d 筆逾 %d 天的警示歷史", m, days)
    except Exception:  # noqa: BLE001
        logger.exception("稽核 / 警示歷史清理失敗")


async def poller_loop() -> None:
    global _last_cleanup, _loop
    _loop = asyncio.get_running_loop()
    logger.info("輪詢器啟動(間隔 %d 秒)", settings.poll_interval_seconds)
    while True:
        try:
            await run_round()
        except Exception:  # noqa: BLE001 —— 單輪失敗不能讓迴圈死掉
            logger.exception("輪詢迴圈發生未預期錯誤")
        if time.monotonic() - _last_cleanup > 86400:
            _last_cleanup = time.monotonic()
            await asyncio.to_thread(_cleanup_audit)
        interval = max(POLL_MIN, min(POLL_MAX, settings.poll_interval_seconds))
        _wake.clear()
        try:
            await asyncio.wait_for(_wake.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def shutdown() -> None:
    for vc_id in list(_clients):
        drop_client(vc_id)
