"""SQLite(data/vcod.db)+ SQLAlchemy 2.x 同步 Session。

啟動時 create_all + 輕量遷移(新欄位冪等 ALTER 補進舊表),不用 Alembic。
"""
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

engine = create_engine(
    f"sqlite:///{DATA_DIR / 'vcod.db'}",
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from app import models

    models.Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # 輪詢器(執行緒)寫入連線狀態 + Web 讀取並行 → WAL 避免互鎖
        conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_auditlog_ts ON audit_logs (ts)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_alerthistory_at ON alert_history (at)")
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_alerthistory_key ON alert_history (key)")
    _migrate(engine)


def _migrate(engine) -> None:
    """輕量遷移:models 新增欄位時補進既有表(create_all 不會 ALTER 舊表)。"""
    migrations: list[tuple[str, str, str]] = [  # (表, 欄位, ALTER 子句)
    ]
    with engine.begin() as conn:
        for table, col, ddl in migrations:
            cols = {r[1] for r in
                    conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if cols and col not in cols:
                conn.exec_driver_sql(ddl)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
