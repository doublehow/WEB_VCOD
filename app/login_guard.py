"""登入失敗節流:同帳號 / 同來源 IP 在視窗內失敗達門檻即鎖定(記憶體,重啟歸零)。

- 帳號鍵用正規化小寫帳號(CORP\\Admin 與 admin 同一鍵),IP 鍵用 request.client.host
  (反向代理後需 uvicorn --proxy-headers 才是真實來源)。
- 成功登入只清該帳號的失敗計數,IP 計數保留(混用有效帳號掃描也擋得住)。
"""
from __future__ import annotations

import threading
import time

WINDOW_S = 15 * 60      # 失敗計數視窗
LOCK_S = 15 * 60        # 鎖定時間
USER_MAX = 5            # 同帳號視窗內失敗次數
IP_MAX = 20             # 同 IP 視窗內失敗次數(多帳號掃描)

_lock = threading.Lock()
_fails: dict[str, list[float]] = {}
_locked_until: dict[str, float] = {}


def _keys(username: str, ip: str) -> list[tuple[str, int]]:
    keys = []
    if username:
        keys.append((f"u:{username}", USER_MAX))
    if ip:
        keys.append((f"i:{ip}", IP_MAX))
    return keys


def _prune(now: float) -> None:
    for k in [k for k, t in _locked_until.items() if t <= now]:
        del _locked_until[k]
    for k in list(_fails):
        _fails[k] = [t for t in _fails[k] if now - t < WINDOW_S]
        if not _fails[k]:
            del _fails[k]


def locked_for(username: str, ip: str) -> int:
    """回傳尚需等待的秒數;0 = 未鎖定。"""
    now = time.monotonic()
    with _lock:
        _prune(now)
        until = max((_locked_until.get(k, 0.0) for k, _ in _keys(username, ip)), default=0.0)
    return int(until - now) + 1 if until > now else 0


def record_failure(username: str, ip: str) -> None:
    now = time.monotonic()
    with _lock:
        _prune(now)
        for k, mx in _keys(username, ip):
            lst = _fails.setdefault(k, [])
            lst.append(now)
            if len(lst) >= mx:
                _locked_until[k] = now + LOCK_S
                lst.clear()


def reset(username: str, ip: str) -> None:  # noqa: ARG001 —— 介面對稱;IP 計數刻意保留
    with _lock:
        _fails.pop(f"u:{username}", None)
        _locked_until.pop(f"u:{username}", None)
