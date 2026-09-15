"""Secret 靜態加密(at-rest):AES-256-GCM,金鑰存 data/secret.key。

- 密文格式:`enc:v1:<base64(nonce16 + tag16 + ciphertext)>`。
- decrypt() 對非密文格式原樣回傳(相容既有明文;啟動時由遷移統一改寫)。
- 金鑰檔首次使用自動生成(32 bytes);**遺失金鑰 = 密文不可復原**,
  只能重新輸入各 secret —— 請把 data/secret.key 隨 DB 一併備份。
- 防護範圍:config.json / DB 檔案外洩(備份、誤傳、誤 commit)時 secret
  不見光;無法防禦已取得主機完整權限的攻擊者(金鑰在同機)。
- 檔案權限:金鑰檔以 0600 建立、data/ 收成 0700(Windows 語意有限,
  權限操作一律 try/except 不擋啟動)。
- 解密失敗(金鑰不符 / 資料損毀)記 log 並回空字串,讓問題以「secret 未設定」
  的形式明確浮現,不讓亂碼默默流向 vCenter / LDAP。
"""
from __future__ import annotations

import base64
import logging
import os
import secrets as _secrets
import stat
from pathlib import Path

from Crypto.Cipher import AES
from sqlalchemy.types import String, TypeDecorator

logger = logging.getLogger("vcod.secret")

PREFIX = "enc:v1:"
_KEY_FILE = Path(__file__).resolve().parent.parent / "data" / "secret.key"
_key_cache: bytes | None = None


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError) as exc:
        logger.debug("無法設定 %s 權限為 %o:%s", path, mode, exc)


def _check_key_perm() -> None:
    """既有金鑰檔權限過寬(group/other 可存取)時記 warning 並嘗試收成 0600。"""
    if os.name != "posix":
        return
    try:
        mode = stat.S_IMODE(_KEY_FILE.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        logger.warning("secret 金鑰檔權限過寬(%o):%s —— 已嘗試收為 0600",
                       mode, _KEY_FILE)
        _chmod(_KEY_FILE, 0o600)


def _read_key() -> bytes:
    k = _KEY_FILE.read_bytes()
    if len(k) != 32:
        raise RuntimeError(f"{_KEY_FILE} 內容非 32 bytes,金鑰檔可能損毀")
    _check_key_perm()
    return k


def _key() -> bytes:
    global _key_cache
    if _key_cache is None:
        _KEY_FILE.parent.mkdir(exist_ok=True)
        _chmod(_KEY_FILE.parent, 0o700)
        if _KEY_FILE.exists():
            _key_cache = _read_key()
        else:
            key = _secrets.token_bytes(32)
            try:
                # O_EXCL + 0600:自建立起就不曾 world-readable
                fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                _key_cache = _read_key()
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(key)
                _chmod(_KEY_FILE, 0o600)
                _key_cache = key
                logger.info("已生成 secret 加密金鑰:%s", _KEY_FILE)
    return _key_cache


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value: str) -> str:
    """明文 → 密文;空值或已是密文原樣回傳。"""
    if not value or is_encrypted(value):
        return value
    cipher = AES.new(_key(), AES.MODE_GCM)
    ct, tag = cipher.encrypt_and_digest(value.encode("utf-8"))
    return PREFIX + base64.b64encode(cipher.nonce + tag + ct).decode()


def decrypt(value):
    """密文 → 明文;非密文格式原樣回傳(相容明文);失敗回空字串。"""
    if not is_encrypted(value):
        return value
    try:
        blob = base64.b64decode(value[len(PREFIX):])
        nonce, tag, ct = blob[:16], blob[16:32], blob[32:]
        cipher = AES.new(_key(), AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ct, tag).decode("utf-8")
    except Exception:  # noqa: BLE001
        logger.error("secret 解密失敗(金鑰不符或資料損毀),視為未設定")
        return ""


class EncryptedStr(TypeDecorator):
    """SQLAlchemy 欄位型別:寫入自動加密、讀出自動解密(明文相容)。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value) if value else value

    def process_result_value(self, value, dialect):
        return decrypt(value) if value else value
