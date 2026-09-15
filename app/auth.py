"""AD(NTLM)驗證 + 帳號分權(RBAC)。

- 驗證流程:Service Account 搜尋使用者 → 使用者帳密 bind 驗密 → 群組授權。
- LDAP 群組只控制「誰能登入」;登入後權限由 account_roles(帳號→角色)決定。
- 角色:full_admin(管理者)/ readonly(唯讀,擋所有寫入)。
  未在對照表中的帳號套用 settings.default_role(預設 readonly);
  本機 admin(session 帶 local=True)恆為 full_admin;AD 帳號即使名為 admin 也一律查表。
"""
from __future__ import annotations

import hashlib
import logging
import time

# ── MD4 相容性修補(Python 3.9+/OpenSSL 3.x 停用 MD4,NTLM 需要)──
try:
    hashlib.new("md4")
except ValueError:
    from Crypto.Hash import MD4 as _MD4_impl

    class _MD4Shim:
        name = "md4"
        digest_size = 16
        block_size = 64

        def __init__(self, d=b""):
            self._h = _MD4_impl.new(d)

        def update(self, d):
            self._h.update(d)
            return self

        def digest(self):
            return self._h.digest()

        def hexdigest(self):
            return self._h.hexdigest()

        def copy(self):
            c = _MD4Shim()
            c._h = self._h.copy()
            return c

    _orig_hashlib_new = hashlib.new

    def _patched_hashlib_new(name, *args, **kwargs):
        if name.lower() == "md4":
            return _MD4Shim(args[0] if args else b"")
        return _orig_hashlib_new(name, *args, **kwargs)

    hashlib.new = _patched_hashlib_new
# ── MD4 修補結束 ──

from ldap3 import ALL, FIRST, NTLM, SUBTREE, Connection, Server, ServerPool
from ldap3.utils.conv import escape_filter_chars

from app.config import save_settings, settings
from app.database import SessionLocal
from app.models import AccountRole

logger = logging.getLogger("vcod.auth")

ROLE_LABELS = {
    "full_admin": "管理者",
    "readonly": "唯讀",
}
_DEFAULT = "readonly"   # fail-safe:未知/未設 → 最小權限
_LDAP_TIMEOUT = 10      # ldap3 預設無限等待,DC 黑洞會卡住登入執行緒


def resolve_roles(username: str) -> list[str]:
    """登入帳號 → 角色清單(依 ROLE_LABELS 順序正規化)。不對 admin 特判:
    本機 admin 由 roles_for_session() 依 session 的 local 旗標放行。"""
    roles: set[str] = set()
    try:
        with SessionLocal() as db:
            for r in db.query(AccountRole).all():
                if (r.username.strip().lower() == username.strip().lower()
                        and r.role in ROLE_LABELS):
                    roles.add(r.role)
    except Exception:  # noqa: BLE001
        pass
    if not roles:
        role = settings.default_role
        roles = {role if role in ROLE_LABELS else _DEFAULT}
    return [r for r in ROLE_LABELS if r in roles]


_roles_cache: dict[str, tuple[float, list[str]]] = {}
_ROLES_TTL = 10.0


def resolve_roles_cached(username: str) -> list[str]:
    """resolve_roles 的短 TTL 快取版(middleware / 模板每請求呼叫)。

    分權表變更最晚 _ROLES_TTL 秒內對既有 session 生效,不必等對方登出。
    """
    now = time.monotonic()
    hit = _roles_cache.get(username)
    if hit and now - hit[0] < _ROLES_TTL:
        return hit[1]
    roles = resolve_roles(username)
    _roles_cache[username] = (now, roles)
    return roles


def invalidate_roles_cache() -> None:
    _roles_cache.clear()


def roles_for_session(user: dict | None) -> list[str]:
    """session user dict → 角色。本機 admin(登入時標 local=True)恆為管理者;
    其餘(含 AD 上名為 admin 的帳號)一律查分權表,避免 AD 帳號借名取得管理者。"""
    user = user or {}
    if user.get("local") is True and user.get("id") == "admin":
        return ["full_admin"]
    return resolve_roles_cached(str(user.get("id", "")))


ADMIN_PASSWORD_MIN_LEN = 12


def local_admin_must_change(password_used: str) -> bool:
    """本機 admin 以此密碼登入後是否須強制變更:系統產生的初始密碼、或不符現行規則(如舊版預設 admin)。"""
    from app.config import settings
    return bool(settings.local_admin_initial) or bool(admin_password_problem(password_used))


def set_local_admin_password(pw: str) -> None:
    """寫入新密碼(密文)、清初始旗標、刪初始密碼檔;設定頁與 CLI 共用。呼叫端先過 admin_password_problem。"""
    from app.config import INITIAL_ADMIN_PW_FILE, save_settings
    save_settings({"local_admin_password": pw, "local_admin_initial": False})
    try:
        INITIAL_ADMIN_PW_FILE.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("無法刪除初始密碼檔 %s:%s,請手動刪除", INITIAL_ADMIN_PW_FILE, exc)


def admin_password_problem(pw: str) -> str:
    """本機 admin 新密碼檢查(設定頁與 set_admin_password 共用);回錯誤訊息,合格回空字串。"""
    if len(pw) < ADMIN_PASSWORD_MIN_LEN:
        return f"本機 admin 密碼至少 {ADMIN_PASSWORD_MIN_LEN} 字元"
    if pw.strip().lower() == "admin":
        return "本機 admin 密碼不得為 admin"
    return ""


def group_match(member_of, allowed_group: str) -> bool:
    """群組授權比對:取每個群組 DN 的第一個 RDN(CN=值)不分大小寫精確相等。

    不用子字串比對——「Admins」不可放行「Admins-Test」。"""
    want = (allowed_group or "").strip().lower()
    if not want:
        return False
    for gdn in member_of or []:
        rdn = str(gdn).split(",", 1)[0].strip()
        if rdn.lower().startswith("cn=") and rdn[3:].strip().lower() == want:
            return True
    return False


def normalize_username(username: str) -> str:
    """DOMAIN\\user / user@domain / DOMAIN/user → 純 sAMAccountName。"""
    if "\\" in username:
        username = username.split("\\", 1)[1]
    elif "/" in username:
        username = username.split("/", 1)[1]
    elif "@" in username:
        username = username.split("@", 1)[0]
    return username.strip()


def authenticate_ad(username: str, password: str) -> tuple[bool, object]:
    """回傳 (True, {'id','name'}) 或 (False, 錯誤訊息字串)。"""
    username = normalize_username(username)
    # 空密碼防護:LDAP 對空密碼可能以匿名 bind 回報成功
    if not username or not password:
        return False, "帳號與密碼不可為空"

    domain = settings.ad_domain
    server_ips = settings.ad_servers
    svc_user = settings.ad_service_user
    svc_pass = settings.ad_service_password
    allowed_group = settings.ad_allowed_group
    base_dn = settings.ad_base_dn

    if not server_ips:
        return False, "尚未設定 AD 伺服器"
    if not svc_user or not svc_pass:
        return False, "尚未設定 AD Service Account"

    servers = [Server(ip, get_info=ALL, use_ssl=settings.ad_use_ssl,
                      connect_timeout=_LDAP_TIMEOUT) for ip in server_ips]
    server_pool = ServerPool(servers, pool_strategy=FIRST)
    full_svc = f"{domain}\\{svc_user}" if "\\" not in svc_user else svc_user

    try:
        # Step 1:Service Account 連線搜尋使用者
        conn = Connection(server_pool, user=full_svc, password=svc_pass,
                          authentication=NTLM, auto_bind=True,
                          receive_timeout=_LDAP_TIMEOUT)
        if not base_dn:
            try:
                if conn.server and conn.server.info:
                    base_dn = conn.server.info.other.get(
                        "defaultNamingContext", [None])[0]
            except Exception:  # noqa: BLE001
                base_dn = None
            if not base_dn:
                return False, "無法自動偵測 Base DN,請於設定頁手動填寫"
            save_settings({"ad_base_dn": base_dn})

        conn.search(
            base_dn,
            f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(username)}))",
            attributes=["distinguishedName", "memberOf", "displayName"],
            search_scope=SUBTREE,
        )
        if not conn.entries:
            return False, "找不到該使用者帳號"

        entry = conn.entries[0]
        display_name = (entry.displayName.value if "displayName" in entry
                        else None) or username
        member_of = entry.memberOf.value if "memberOf" in entry else []
        conn.unbind()

        # Step 2:使用者帳密驗密
        user_conn = Connection(server_pool, user=f"{domain}\\{username}",
                               password=password, authentication=NTLM,
                               receive_timeout=_LDAP_TIMEOUT)
        if not user_conn.bind():
            return False, "密碼錯誤"
        user_conn.unbind()

        # Step 3:群組授權(未設允許群組 = 通過驗證即放行)
        if isinstance(member_of, str):
            member_of = [member_of]
        user_info = {"id": username, "name": display_name}
        if not allowed_group or group_match(member_of, allowed_group):
            return True, user_info
        return False, f"驗證通過,但您不在授權群組({allowed_group})內"

    except Exception as exc:  # noqa: BLE001
        return False, f"AD 連線或驗證錯誤:{exc}"
