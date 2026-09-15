"""應用設定:環境變數 VCOD_* > config.json > .env。

- config.json 由 Web 設定頁(/settings)維護,是設定的權威來源。
- 存檔即就地更新記憶體單例,輪詢器 / 登入等功能立即讀到新值。
- 分工:每座 vCenter 的連線設定(主機 / 帳密 / TLS)存 DB(vcenters 表,
  密碼 AES-GCM 加密);此處為全域營運設定(AD 登入、輪詢間隔、警示門檻等)。
- secret 欄位落地一律密文(secret_store),記憶體單例持有明文。
"""
from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "config.json"
INITIAL_ADMIN_PW_FILE = BASE_DIR / "data" / "initial_admin_password.txt"   # 首次啟動產生,改密碼後刪除

# 由設定頁管理、可持久化到 config.json 的欄位
UI_FIELDS = (
    # AD(NTLM)登入
    "ad_enabled",
    "ad_domain",
    "ad_servers",
    "ad_service_user",
    "ad_service_password",
    "ad_allowed_group",
    "ad_base_dn",
    "ad_use_ssl",
    "local_admin_password",
    "default_role",
    # 輪詢 / 保留
    "poll_interval_seconds",
    "log_retention_days",
    # 警示引擎(警示頁「規則」分頁維護)
    "alert_rules",
    "alert_debounce_rounds",
    "alert_exclude_patterns",
    "alert_notify_recovery",
    "alert_forward_vcenter_alarms",
    # 通知管道(Telegram + SMTP)
    "telegram_bot_token",
    "telegram_chat_id",
    "smtp_host",
    "smtp_port",
    "smtp_tls",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "smtp_to",
)

# 可持久化但不在設定頁顯示的內部欄位
_PERSIST_FIELDS = UI_FIELDS + ("session_secret", "web_port", "timezone",
                               "telegram_skip_tls_verify", "behind_tls", "local_admin_initial")

# 密碼類欄位:設定頁留空 = 不變更(避免把明碼 render 到 HTML)
SECRET_FIELDS = ("ad_service_password", "local_admin_password",
                 "smtp_password", "telegram_bot_token")

# config.json 中以密文保存的欄位(記憶體單例一律持有明文)
_ENC_FIELDS = SECRET_FIELDS + ("session_secret",)

POLL_MIN, POLL_MAX = 10, 3600
DEBOUNCE_MIN, DEBOUNCE_MAX = 1, 20


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VCOD_",
        env_file=".env",
        env_file_encoding="utf-8",
        json_file=CONFIG_FILE,
        json_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- AD(NTLM)驗證登入 ----
    ad_enabled: bool = False
    ad_domain: str = ""              # NetBIOS 名,例:CORP
    ad_servers: list[str] = []       # AD 伺服器 IP 清單
    ad_service_user: str = ""        # 查詢使用者用的 service account
    ad_service_password: str = ""
    ad_allowed_group: str = ""       # 允許登入的群組(留空 = 通過驗證即放行)
    ad_base_dn: str = ""             # 留空自動偵測
    ad_use_ssl: bool = False         # LDAPS(636)
    local_admin_password: str = ""   # 本機備援登入;空且 AD 未啟用時啟動自動產生初始密碼(見 main.lifespan)
    local_admin_initial: bool = False   # True = 目前密碼為系統產生的初始密碼,登入後強制變更
    default_role: str = "readonly"        # 未分權帳號的預設角色

    # ---- 輪詢 / 保留 ----
    poll_interval_seconds: int = 30  # vCenter 資料輪詢間隔(秒)
    log_retention_days: int = 365    # 稽核 / 警示歷史保留天數(0 = 不清理)

    # ---- 警示引擎(規則門檻見 alerting.RULES;此處只存使用者覆寫值)----
    alert_rules: dict[str, dict] = {}          # {rule_key: {"enabled": bool, "warning": num|None, "critical": num|None}}
    alert_debounce_rounds: int = 3             # 用量類規則連續 N 輪命中才觸發、N 輪未命中才解除
    alert_exclude_patterns: list[str] = ["vCLS-*"]   # 名稱樣式排除(fnmatch,不分大小寫)
    alert_notify_recovery: bool = True         # 恢復時也通知
    alert_forward_vcenter_alarms: bool = False # vCenter 內建告警也外送(預設只顯示)

    # ---- 通知管道 ----
    telegram_bot_token: str = ""     # BotFather 核發
    telegram_chat_id: str = ""       # 個人 / 群組 chat id(群組為負數)
    telegram_skip_tls_verify: bool = False   # 防火牆 SSL inspection 時的避開手段(僅 config.json)
    behind_tls: bool = False                 # 前端有 TLS(反向代理)時設 true:cookie Secure + HSTS(僅 config.json / 環境變數)
    smtp_host: str = ""              # 留空 = 停用郵件
    smtp_port: int = 25              # 465 走 SMTPS,其他埠依 smtp_tls
    smtp_tls: bool = False           # STARTTLS
    smtp_user: str = ""              # 留空 = 不驗證(內部 relay)
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""                # 逗號分隔收件人

    # ---- 服務埠 / 時區(不在設定頁,由 config.json / 環境變數 VCOD_* 管理)----
    web_port: int = 8082
    timezone: str = "Asia/Taipei"

    # ---- 內部 ----
    session_secret: str = ""

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # 優先序:初始化參數 > 環境變數 VCOD_* > config.json > .env
        #(環境變數覆寫檔案是慣例,也讓 VCOD_WEB_PORT=<測試埠> 在 config.json 已有 web_port 時仍生效)
        return (init_settings, env_settings, JsonConfigSettingsSource(settings_cls),
                dotenv_settings)


settings = Settings()

from app import secret_store  # noqa: E402 —— 置後避免循環匯入疑慮

for _f in _ENC_FIELDS:
    setattr(settings, _f, secret_store.decrypt(getattr(settings, _f)))


def clamp_int(value, default: int, lo: int, hi: int) -> int:
    """表單字串 → 落在 [lo, hi] 的整數;非數值回 default。"""
    try:
        return max(lo, min(hi, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def local_now() -> datetime:
    """settings.timezone 的當下時間(naive,DB 統一格式)。"""
    try:
        return datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 —— timezone 設錯時退回系統時區
        return datetime.now()


def to_local(dt: datetime) -> datetime:
    """aware datetime → settings.timezone 的 naive 本地時間(與 local_now 同一時區);
    naive 輸入視為已是本地時間原樣回傳。勿在他處用 astimezone()(那是系統時區)。"""
    if dt.tzinfo is None:
        return dt
    try:
        return dt.astimezone(ZoneInfo(settings.timezone)).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 —— timezone 設錯時退回系統時區
        return dt.astimezone().replace(tzinfo=None)


def _read_config_file() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


_save_lock = threading.Lock()


def _write_config_file(data: dict) -> None:
    CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")


def save_settings(updates: dict) -> None:
    """更新記憶體單例並持久化到 config.json(只接受可持久化欄位)。

    讀-改-寫全程持鎖;secret 欄位落地前加密。
    """
    with _save_lock:
        data = _read_config_file()
        for key, value in updates.items():
            if key not in _PERSIST_FIELDS:
                continue
            setattr(settings, key, value)
            data[key] = (secret_store.encrypt(value)
                         if key in _ENC_FIELDS and value else value)
        _write_config_file(data)


def migrate_plaintext_secrets() -> None:
    """啟動時把 config.json 既有明文 secret 就地改寫為密文(冪等)。"""
    with _save_lock:
        data = _read_config_file()
        changed = [f for f in _ENC_FIELDS
                   if data.get(f) and not secret_store.is_encrypted(data[f])]
        for f in changed:
            data[f] = secret_store.encrypt(data[f])
        if changed:
            _write_config_file(data)
    if changed:
        import logging
        logging.getLogger("vcod.secret").info(
            "config.json 明文 secret 已改寫為密文:%s", ", ".join(changed))


def ensure_session_secret() -> str:
    """回傳 session 簽章密鑰;不存在時自動生成並持久化。"""
    if not settings.session_secret:
        save_settings({"session_secret": secrets.token_hex(32)})
    return settings.session_secret
