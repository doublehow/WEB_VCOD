"""稽核紀錄頁。"""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditLog
from app.webutil import like_escape, render

router = APIRouter()


@router.get("/logs")
def log_list(request: Request, db: Session = Depends(get_db),
             q: str = "", action: str = ""):
    query = db.query(AuditLog)
    q = q.strip()[:100]
    action = action.strip()[:50]
    if action:
        query = query.filter(AuditLog.action == action)
    if q:
        like = f"%{like_escape(q)}%"
        query = query.filter(AuditLog.detail.like(like, escape="\\")
                             | AuditLog.user.like(like, escape="\\"))
    logs = query.order_by(AuditLog.id.desc()).limit(300).all()
    actions = [a for (a,) in db.query(AuditLog.action).distinct().order_by(AuditLog.action).all()]
    return render(request, "logs.html", "logs", logs=logs, q=q, action=action,
                  actions=actions)
