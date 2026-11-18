"""Operational CLI used by install.sh and support staff: `docker compose run --rm api manage <cmd>`."""
import argparse
import datetime as dt
import ipaddress
import os
import secrets
import sys
import time


def cmd_wait_db(args) -> int:
    from sqlalchemy import text
    from app.db import engine
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            with engine.connect() as c:
                c.execute(text("select 1"))
            return 0
        except Exception:  # noqa: BLE001
            time.sleep(2)
    print("database not reachable", file=sys.stderr)
    return 1


def cmd_create_admin(args) -> int:
    from sqlalchemy import select
    from app.audit import audit
    from app.db import session_scope
    from app.models import User
    from app.security import hash_password
    password = args.password or secrets.token_urlsafe(14)
    with session_scope() as db:
        user = db.scalars(select(User).where(User.username == args.username)).first()
        if user and not args.reset:
            print(f"user {args.username} already exists (use --reset to set a new password)")
            return 0
        if user is None:
            user = User(username=args.username, role="admin")
            db.add(user)
        user.password_hash, user.role, user.is_active = hash_password(password), "admin", True
        audit(db, "installer", "user.admin_bootstrap", "user", args.username)
    print(password if args.print_password else f"admin user {args.username} ready")
    return 0


def cmd_bootstrap(args) -> int:
    from app.db import session_scope
    from app.framework.runner import ensure_task_configs
    with session_scope() as db:
        ensure_task_configs(db)
    print("task configurations initialised")
    return 0


def cmd_gen_cert(args) -> int:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    os.makedirs(args.out, exist_ok=True)
    crt, key_path = os.path.join(args.out, "server.crt"), os.path.join(args.out, "server.key")
    if os.path.exists(crt) and not args.force:
        print("certificate exists, keeping it")
        return 0
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    names = [x509.DNSName(args.hostname), x509.DNSName("localhost")]
    for ip in args.ip or []:
        try:
            names.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, args.hostname),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AIOps")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    with open(key_path, "wb") as fh:
        fh.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                   serialization.NoEncryption()))
    os.chmod(key_path, 0o640)
    with open(crt, "wb") as fh:
        fh.write(cert.public_bytes(serialization.Encoding.PEM))
    print(f"self-signed certificate written for {args.hostname}")
    return 0


def cmd_add_mock_sources(args) -> int:
    from sqlalchemy import select
    from app.db import session_scope
    from app.models import DataSource
    from app.security import encrypt_secret
    with session_scope() as db:
        for name, url in (("Mock Catalyst Center 2.3.5", "http://mockcc-23:8080"),
                          ("Mock Catalyst Center 3.1.3", "http://mockcc-31:8080")):
            if db.scalars(select(DataSource).where(DataSource.name == name)).first():
                continue
            db.add(DataSource(name=name, kind="catalyst_center", base_url=url, username="admin",
                              secret_enc=encrypt_secret("mock"), verify_tls=False, rate_limit_rps=50,
                              enabled=False))
    print("mock sources added (disabled; enable one in the UI)")
    return 0


def cmd_selftest(args) -> int:
    import redis
    from sqlalchemy import text
    from app.config import get_settings
    from app.db import engine
    from app.framework import registry
    from app.llm.ollama import OllamaClient

    ok = True

    def check(name: str, fn) -> None:
        nonlocal ok
        try:
            detail = fn()
            print(f"[ OK ] {name}{': ' + detail if detail else ''}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"[FAIL] {name}: {exc}")

    def db_check():
        with engine.connect() as c:
            v = c.execute(text("select version_num from alembic_version")).scalar()
        return f"schema {v}"

    def llm_check():
        st = OllamaClient().status()
        if not st["reachable"]:
            raise RuntimeError(st.get("error"))
        if not st["model_present"]:
            if args.allow_no_model:
                return f"WARNING model {st['model']} not pulled; rule-based confidence only"
            raise RuntimeError(f"model {st['model']} not pulled (present: {st['models']})")
        if args.llm_probe:
            OllamaClient().chat_json("Reply with JSON.", 'Return {"ok": true}',
                                     {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
        return st["model"]

    check("database", db_check)
    check("redis", lambda: str(redis.Redis.from_url(get_settings().redis_url).ping()))
    check("local LLM", llm_check)
    check("task registry", lambda: ", ".join(registry.load_all()))

    def workers_check():
        from app.workers import WORKERS, snapshot
        tasks = registry.load_all().values()
        missing = sorted({t.worker for t in tasks} - set(WORKERS))
        if missing:
            raise RuntimeError(f"tasks reference unknown workers: {missing}")
        dead = [w["name"] for w in snapshot() if not w["alive"]]
        if dead:
            raise RuntimeError(f"no heartbeat from: {', '.join(dead)}")
        return ", ".join(WORKERS)

    check("specialist workers", workers_check)
    return 0 if ok else 1


def cmd_record_fixtures(args) -> int:
    from app.tools.record_fixtures import record
    return record(args.source_id, args.out, args.max_objects)


def main() -> int:
    p = argparse.ArgumentParser(prog="manage")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("wait-db"); s.add_argument("--timeout", type=int, default=120); s.set_defaults(fn=cmd_wait_db)
    s = sub.add_parser("create-admin")
    s.add_argument("--username", default="admin"); s.add_argument("--password")
    s.add_argument("--reset", action="store_true"); s.add_argument("--print-password", action="store_true")
    s.set_defaults(fn=cmd_create_admin)
    s = sub.add_parser("bootstrap"); s.set_defaults(fn=cmd_bootstrap)
    s = sub.add_parser("gen-cert")
    s.add_argument("--hostname", required=True); s.add_argument("--ip", action="append")
    s.add_argument("--out", default="/certs"); s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_gen_cert)
    s = sub.add_parser("add-mock-sources"); s.set_defaults(fn=cmd_add_mock_sources)
    s = sub.add_parser("selftest"); s.add_argument("--llm-probe", action="store_true")
    s.add_argument("--allow-no-model", action="store_true"); s.set_defaults(fn=cmd_selftest)
    s = sub.add_parser("record-fixtures")
    s.add_argument("--source-id", type=int, required=True); s.add_argument("--out", default="/data/fixtures")
    s.add_argument("--max-objects", type=int, default=25)
    s.set_defaults(fn=cmd_record_fixtures)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
