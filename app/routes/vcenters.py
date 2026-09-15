"""vCenter 管理:清單 / 新增 / 編輯 / 刪除 / 啟停 / 連線測試(AJAX)/ 立即更新。"""
import asyncio
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import inventory
from app.audit import audit
from app.config import clamp_int
from app.database import get_db
from app.models import VCenter
from app.vsphere import test_connection
from app.webutil import render

router = APIRouter()


def _clean(name: str, host: str, port, username: str) -> tuple[str, str, int, str, str]:
    """表單欄位正規化;回 (name, host, port, username, error)。"""
    name, host, username = name.strip(), host.strip(), username.strip()
    port_i = clamp_int(port, 443, 1, 65535)
    if not name or not host or not username:
        return name, host, port_i, username, "顯示名稱、主機與帳號皆為必填"
    if len(name) > 100 or len(host) > 255 or len(username) > 200:
        return name, host, port_i, username, "欄位長度超過上限"
    return name, host, port_i, username, ""


@router.get("/vcenters")
def vc_list(request: Request, db: Session = Depends(get_db),
            error: str = "", saved: str = ""):
    vcs = db.query(VCenter).order_by(VCenter.name).all()
    snap = inventory.current()
    counts = {}
    for vc in vcs:
        st = snap.vcs.get(vc.id)
        d = st.data if st else None
        counts[vc.id] = {"hosts": len(d.hosts) if d else 0, "vms": len(d.vms) if d else 0,
                         "stale": bool(st and st.stale)}
    return render(request, "vcenters.html", "vcenters", vcs=vcs, counts=counts,
                  error=error, saved=saved)


@router.post("/vcenters")
def vc_add(request: Request, db: Session = Depends(get_db),
           name: str = Form(""), host: str = Form(""), port: str = Form("443"),
           username: str = Form(""), password: str = Form(""),
           verify_ssl: str = Form(""), note: str = Form("")):
    name, host, port_i, username, err = _clean(name, host, port, username)
    if not err and not password:
        err = "新增 vCenter 需填寫密碼"
    if not err and db.query(VCenter).filter(VCenter.name == name).first():
        err = f"顯示名稱「{name}」已存在"
    if err:
        return RedirectResponse(f"/vcenters?error={err}", status_code=303)
    vc = VCenter(name=name, host=host, port=port_i, username=username, password=password,
                 verify_ssl=bool(verify_ssl), note=note.strip()[:300], enabled=True)
    db.add(vc)
    db.commit()
    audit(request, "vcenter_add", f"新增 vCenter:{name}({host}:{port_i},{username})")
    from app import poller
    poller.request_poll()
    return RedirectResponse("/vcenters?saved=1", status_code=303)


@router.get("/vcenters/{vc_id}/edit")
def vc_edit_page(request: Request, vc_id: int, db: Session = Depends(get_db),
                 error: str = ""):
    vc = db.get(VCenter, vc_id)
    if vc is None:
        return RedirectResponse("/vcenters?error=找不到該 vCenter", status_code=303)
    return render(request, "vcenter_edit.html", "vcenters", vc=vc, error=error)


@router.post("/vcenters/{vc_id}/edit")
def vc_edit(request: Request, vc_id: int, db: Session = Depends(get_db),
            name: str = Form(""), host: str = Form(""), port: str = Form("443"),
            username: str = Form(""), password: str = Form(""),
            verify_ssl: str = Form(""), enabled: str = Form(""), note: str = Form("")):
    vc = db.get(VCenter, vc_id)
    if vc is None:
        return RedirectResponse("/vcenters?error=找不到該 vCenter", status_code=303)
    name, host, port_i, username, err = _clean(name, host, port, username)
    if not err:
        dup = db.query(VCenter).filter(VCenter.name == name, VCenter.id != vc_id).first()
        if dup:
            err = f"顯示名稱「{name}」已被其他 vCenter 使用"
    if err:
        return RedirectResponse(f"/vcenters/{vc_id}/edit?error={err}", status_code=303)
    changes = []
    for field, new in (("name", name), ("host", host), ("port", port_i),
                       ("username", username), ("verify_ssl", bool(verify_ssl)),
                       ("enabled", bool(enabled)), ("note", note.strip()[:300])):
        if getattr(vc, field) != new:
            changes.append(field)
            setattr(vc, field, new)
    if password:                      # 留空 = 不變更
        vc.password = password
        changes.append("password")
    elif any(f in changes for f in ("host", "port", "username")):
        # 沿用已存密碼卻改連線目標 → 下輪輪詢會把密碼送到新主機;與 /vcenters/test 同一道防線
        db.rollback()
        audit(request, "vcenter_edit_blocked",
              f"編輯遭拒:{name} 變更 {', '.join(changes)} 但未重新輸入密碼")
        return RedirectResponse(f"/vcenters/{vc_id}/edit?error=變更主機 / 埠 / 帳號時須重新輸入密碼,"
                                "避免已儲存的密碼被送往其他目標", status_code=303)
    if not vc.enabled:
        vc.last_status = "disabled"
        vc.last_error = ""
    elif vc.last_status == "disabled":
        vc.last_status = "pending"
    db.commit()
    audit(request, "vcenter_edit", f"編輯 vCenter:{vc.name}(異動欄位:{', '.join(changes) or '無'})")
    from app import poller
    poller.drop_client(vc_id)
    poller.request_poll()
    return RedirectResponse("/vcenters?saved=1", status_code=303)


@router.post("/vcenters/{vc_id}/delete")
def vc_delete(request: Request, vc_id: int, db: Session = Depends(get_db)):
    vc = db.get(VCenter, vc_id)
    if vc:
        db.delete(vc)
        db.commit()
        audit(request, "vcenter_delete", f"刪除 vCenter:{vc.name}({vc.host})")
        from app import poller
        poller.drop_client(vc_id)
        poller.request_poll()
    return RedirectResponse("/vcenters?saved=1", status_code=303)


@router.post("/vcenters/poll")
async def vc_poll_now(request: Request):
    """立即更新:喚醒輪詢器跑一輪(不等待完成)。"""
    from app import poller
    poller.request_poll()
    audit(request, "poll_now", "手動觸發 vCenter 立即更新")
    return RedirectResponse("/vcenters?saved=poll", status_code=303)


@router.post("/vcenters/test")
async def vc_test(request: Request, db: Session = Depends(get_db)):
    """AJAX:以表單目前填的值測試連線。密碼留空且帶 id 時沿用已存密碼——
    此時目標主機必須與已存值相同,防止把已存密碼送往攻擊者架設的假 vCenter。"""
    try:
        data = json.loads(await request.body())
    except Exception:  # noqa: BLE001
        return {"ok": False, "message": "無效的請求格式"}
    host = str(data.get("host", "")).strip()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    port = clamp_int(data.get("port", 443), 443, 1, 65535)
    verify_ssl = bool(data.get("verify_ssl"))
    vc_id = data.get("id")
    if not host or not username:
        return {"ok": False, "message": "請填寫主機與帳號"}
    if not password:
        vc = (await asyncio.to_thread(db.get, VCenter, int(vc_id))
              if str(vc_id or "").isdigit() else None)
        if vc is None or not vc.password:
            return {"ok": False, "message": "請輸入密碼"}
        if (vc.host != host or (vc.port or 443) != port or vc.username != username
                or bool(vc.verify_ssl) != verify_ssl):
            audit(request, "vc_test_blocked",
                  f"連線測試遭拒:沿用已存密碼但目標 {username}@{host}:{port}"
                  f"(verify_ssl={verify_ssl})與已存不符")
            return {"ok": False, "message":
                    "使用已儲存的密碼時,主機 / 埠 / 帳號 / TLS 驗證須與已儲存值相同;"
                    "測試其他目標請一併輸入密碼"}
        password = vc.password
    ok, message = await asyncio.to_thread(test_connection, host, username, password,
                                          port, verify_ssl)
    audit(request, "vc_test", f"連線測試 {username}@{host}:{port}:{'成功' if ok else '失敗'}")
    return {"ok": ok, "message": message}
