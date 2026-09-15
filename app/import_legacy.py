"""從舊版(NiceGUI 版 vCenter Dashboard)的 config.json 匯入 vCenter 連線與 AD 設定。

用法:.\\.venv\\Scripts\\python.exe -m app.import_legacy <舊版 config.json 路徑>

- 舊格式:{"vcenter": [{"ip","user","password"}, ...], "ad": {...}} 或純 list。
- vCenter 密碼入庫即以 AES-GCM 加密;顯示名稱取主機名第一段,重名自動加序號;
  同主機 + 帳號已存在者略過。舊版一律不驗證憑證,匯入時沿用(verify_ssl=False),
  可於管理頁逐座改回驗證。
- AD 設定寫入 config.json(密碼加密),但 **不自動啟用**(ad_enabled 維持原值),
  請於設定頁確認後啟用。
- 匯入後請自行刪除或妥善保管舊版 config.json(內含明文帳密)。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(path: str) -> int:
    src = Path(path)
    if not src.is_file():
        print(f"找不到檔案:{src}", file=sys.stderr)
        return 2
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"讀取失敗:{exc}", file=sys.stderr)
        return 2
    if isinstance(raw, list):
        raw = {"vcenter": raw, "ad": {}}
    vcs = raw.get("vcenter") or []
    ad = raw.get("ad") or {}

    from app.config import save_settings
    from app.database import SessionLocal, init_db
    from app.models import VCenter

    init_db()
    added = skipped = 0
    with SessionLocal() as db:
        existing = {(v.host.lower(), v.username.lower()) for v in db.query(VCenter).all()}
        names = {v.name for v in db.query(VCenter).all()}
        for item in vcs:
            host = str(item.get("ip") or item.get("host") or "").strip()
            user = str(item.get("user") or item.get("username") or "").strip()
            pwd = str(item.get("password") or "")
            if not host or not user or not pwd:
                print(f"略過(欄位不完整):{item.get('ip')}")
                skipped += 1
                continue
            if (host.lower(), user.lower()) in existing:
                print(f"略過(已存在):{user}@{host}")
                skipped += 1
                continue
            base = host.split(".")[0] or host
            name, n = base, 2
            while name in names:
                name, n = f"{base}-{n}", n + 1
            db.add(VCenter(name=name, host=host, port=443, username=user, password=pwd,
                           verify_ssl=False, enabled=True, note="自舊版 config.json 匯入"))
            names.add(name)
            existing.add((host.lower(), user.lower()))
            added += 1
            print(f"匯入:{name} ← {user}@{host}(TLS 驗證:略過)")
        db.commit()

    if ad:
        updates = {}
        mapping = {"domain": "ad_domain", "servers": "ad_servers", "service_user": "ad_service_user",
                   "service_password": "ad_service_password", "allowed_group": "ad_allowed_group",
                   "base_dn": "ad_base_dn"}
        for old, new in mapping.items():
            val = ad.get(old)
            if val:
                updates[new] = val if new != "ad_servers" else [str(s).strip() for s in val if str(s).strip()]
        if updates:
            save_settings(updates)
            print(f"AD 設定已寫入 config.json:{', '.join(updates)}(ad_enabled 未變更,請於設定頁啟用)")

    print(f"完成:新增 {added} 座 vCenter,略過 {skipped} 座。")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    sys.exit(main(sys.argv[1]))
