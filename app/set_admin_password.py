"""建立 / 變更 / 停用本機 admin 密碼(備援入口;一般於設定頁變更,首次啟動由服務自動產生初始密碼)。

用法(專案根目錄):
  python -m app.set_admin_password            # 互動輸入兩次(不回顯),寫入 config.json 密文
  python -m app.set_admin_password --disable  # 清空 = 停用本機登入(須已啟用 AD,否則拒絕)

刻意不接受參數 / 管線輸入,避免密碼留在 shell 歷史或排程腳本。
"""
from __future__ import annotations

import argparse
import getpass
import sys

from app.auth import admin_password_problem, set_local_admin_password
from app.config import save_settings, settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="設定 VCOD 本機 admin 密碼")
    ap.add_argument("--disable", action="store_true", help="清空密碼,停用本機 admin 登入")
    args = ap.parse_args(argv)

    if args.disable:
        if not settings.ad_enabled:
            print("AD 未啟用,停用本機 admin 後將無任何登入方式;請先於設定頁啟用 AD。", file=sys.stderr)
            return 2
        save_settings({"local_admin_password": "", "local_admin_initial": False})
        print("已停用本機 admin 登入(config.json local_admin_password 已清空)。")
        return 0

    if not sys.stdin.isatty():
        print("需要互動式終端輸入密碼(不接受參數 / 管線)。", file=sys.stderr)
        return 2
    try:
        p1 = getpass.getpass("新密碼:")
        p2 = getpass.getpass("再輸入一次:")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        return 1
    if p1 != p2:
        print("兩次輸入不一致。", file=sys.stderr)
        return 1
    problem = admin_password_problem(p1)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    set_local_admin_password(p1)
    print("已更新本機 admin 密碼(config.json 以 AES-GCM 密文保存)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
