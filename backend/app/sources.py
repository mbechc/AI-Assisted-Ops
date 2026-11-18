import os
import tempfile
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.connectors.catalyst_center import CatalystCenter
from app.models import DataSource
from app.security import decrypt_secret


def build_cc_client(source: DataSource, recorder=None) -> CatalystCenter:
    verify: bool | str = bool(source.verify_tls)
    if source.verify_tls and source.ca_bundle:
        fd, path = tempfile.mkstemp(prefix="cc-ca-", suffix=".pem")
        with os.fdopen(fd, "w") as fh:
            fh.write(source.ca_bundle)
        verify = path
    return CatalystCenter(
        base_url=source.base_url,
        username=source.username,
        password=decrypt_secret(source.secret_enc),
        verify=verify,
        rate_limit_rps=source.rate_limit_rps or 5,
        capability_state=source.capabilities or {},
        recorder=recorder,
    )


def save_capabilities(db: Session, source: DataSource, client: CatalystCenter, status: str = "ok") -> None:
    state = client.capability_state()
    source.capabilities = state
    source.detected_version = state.get("version")
    source.last_check_at = datetime.now(timezone.utc)
    source.last_status = status
    db.add(source)
