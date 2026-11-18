from sqlalchemy.orm import Session

from app.models import AuditLog


def audit(db: Session, actor: str, action: str, object_type: str | None = None,
          object_id: str | int | None = None, **details) -> None:
    db.add(AuditLog(
        actor=actor,
        action=action,
        object_type=object_type,
        object_id=str(object_id) if object_id is not None else None,
        details=details,
    ))
