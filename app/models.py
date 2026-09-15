"""ORM 模型:VCenter(連線設定 + 最近輪詢狀態)/ AccountRole / AuditLog /
Alert(進行中警示)/ AlertHistory(警示轉態紀錄)。

vCenter 的庫存資料(主機 / VM / 儲存區 / 網路)**不入庫**,由 inventory.py
於記憶體維護即時快照;DB 只存連線設定、分權、稽核與警示狀態。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import local_now
from app.secret_store import EncryptedStr


class Base(DeclarativeBase):
    pass


# 輪詢狀態 → 語意 badge 色(對應 base.html 的 .badge-*)
POLL_STATUS_LABELS = {"ok": "已連線", "failed": "連線失敗",
                      "disabled": "停用", "pending": "尚未輪詢"}
POLL_STATUS_CSS = {"ok": "allow", "failed": "deny",
                   "disabled": "muted", "pending": "warn"}


class VCenter(Base):
    __tablename__ = "vcenters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)   # 顯示名(唯一)
    host: Mapped[str] = mapped_column(String(255))                # IP / FQDN
    port: Mapped[int] = mapped_column(Integer, default=443)
    username: Mapped[str] = mapped_column(String(200), default="")
    password: Mapped[str] = mapped_column(EncryptedStr(600), default="")
    verify_ssl: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(300), default="")

    # 最近一次輪詢結果(由 poller 執行緒寫入)
    last_status: Mapped[str] = mapped_column(String(20), default="pending")
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[str] = mapped_column(String(120), default="")   # about.fullName

    @property
    def status_label(self) -> str:
        return POLL_STATUS_LABELS.get(self.last_status, self.last_status)

    @property
    def status_css(self) -> str:
        return POLL_STATUS_CSS.get(self.last_status, "muted")


class AccountRole(Base):
    __tablename__ = "account_roles"
    __table_args__ = (
        UniqueConstraint("username", "role", name="uq_account_roles_username_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(20), default="readonly")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=local_now)
    user: Mapped[str] = mapped_column(String(100), default="")
    ip: Mapped[str] = mapped_column(String(45), default="")
    action: Mapped[str] = mapped_column(String(50), default="")
    detail: Mapped[str] = mapped_column(Text, default="")


# 警示嚴重度 → 語意 badge 色 / 顯示名
ALERT_LEVEL_LABELS = {"warning": "警告", "critical": "嚴重"}
ALERT_LEVEL_CSS = {"warning": "warn", "critical": "deny"}
ALERT_EVENT_LABELS = {"firing": "觸發", "resolved": "恢復", "changed": "等級變更"}


class Alert(Base):
    """進行中的警示(每個「規則 × 物件」一筆;解除即刪並寫入 AlertHistory)。

    重啟時由 alerting.load_state() 讀回,避免重啟後把既有警示再通知一次。
    """
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(300), unique=True)   # "<rule>|<vc_id>::<moid>"
    rule: Mapped[str] = mapped_column(String(40))
    layer: Mapped[str] = mapped_column(String(10), default="platform")   # platform / guest
    level: Mapped[str] = mapped_column(String(10), default="warning")
    target_type: Mapped[str] = mapped_column(String(20), default="")     # vcenter / host / datastore / vm
    target_name: Mapped[str] = mapped_column(String(255), default="")
    vc_name: Mapped[str] = mapped_column(String(100), default="")
    value: Mapped[str] = mapped_column(String(300), default="")          # 觸發當下的數值說明
    first_at: Mapped[datetime] = mapped_column(DateTime, default=local_now)
    last_at: Mapped[datetime] = mapped_column(DateTime, default=local_now)

    @property
    def level_label(self) -> str:
        return ALERT_LEVEL_LABELS.get(self.level, self.level)

    @property
    def level_css(self) -> str:
        return ALERT_LEVEL_CSS.get(self.level, "muted")


class AlertHistory(Base):
    """警示轉態紀錄:觸發 / 恢復 / 等級變更各一筆;依 log_retention_days 清理。"""
    __tablename__ = "alert_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=local_now)
    event: Mapped[str] = mapped_column(String(10))       # firing / resolved / changed
    key: Mapped[str] = mapped_column(String(300))
    rule: Mapped[str] = mapped_column(String(40))
    layer: Mapped[str] = mapped_column(String(10), default="platform")
    level: Mapped[str] = mapped_column(String(10), default="warning")
    target_type: Mapped[str] = mapped_column(String(20), default="")
    target_name: Mapped[str] = mapped_column(String(255), default="")
    vc_name: Mapped[str] = mapped_column(String(100), default="")
    value: Mapped[str] = mapped_column(String(300), default="")
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)   # resolved 時的持續秒數
    notified: Mapped[str] = mapped_column(String(300), default="")  # 通知結果摘要

    @property
    def level_label(self) -> str:
        return ALERT_LEVEL_LABELS.get(self.level, self.level)

    @property
    def level_css(self) -> str:
        return ALERT_LEVEL_CSS.get(self.level, "muted")

    @property
    def event_label(self) -> str:
        return ALERT_EVENT_LABELS.get(self.event, self.event)
