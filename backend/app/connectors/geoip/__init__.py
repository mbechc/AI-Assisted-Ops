"""Optional GeoIP source: a locally uploaded MaxMind-format (.mmdb) database.

The database maps IP networks to a location record. A location key (a string such as
"CPH-B03" or "Copenhagen") is extracted from the record and mapped to a Catalyst Center
building, either learned from devices that already have a site or set by an operator.
Nothing here is required: every site task works without a GeoIP source.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import shutil
import tempfile
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import DataSource, GeoipFile, GeoipMapping

DEFAULT_FIELDS = ["site", "building", "location", "location_name", "office", "name",
                  "city.names.en", "city.name", "city"]
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024


@dataclass
class GeoHit:
    key: str
    network: str
    record: dict


def geoip_fields(source: DataSource) -> list[str]:
    cfg = (source.capabilities or {}).get("geoip") or {}
    fields = cfg.get("fields") or DEFAULT_FIELDS
    return [f.strip() for f in fields if f and f.strip()]


def extract_key(record, fields: list[str]) -> str | None:
    if not isinstance(record, dict):
        return None
    for path in fields:
        cur = record
        for part in path.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
                cur = cur[int(part)]
            else:
                cur = None
            if cur is None:
                break
        if isinstance(cur, (str, int)) and str(cur).strip():
            return str(cur).strip()
    return None


def file_path(source_id: int, sha: str) -> str:
    return os.path.join(get_settings().geoip_dir, str(source_id), f"{sha}.mmdb")


def open_reader(path: str):
    import maxminddb
    return maxminddb.open_database(path)


class GeoipProvider:
    def __init__(self, source: DataSource, gf: GeoipFile, reader, mappings: dict[str, str]):
        self.source, self.file, self.reader = source, gf, reader
        self.fields = geoip_fields(source)
        self.mappings = mappings
        self._cache: dict[str, GeoHit | None] = {}

    def describe(self) -> str:
        return (f"{self.source.name}, file {self.file.filename} ({self.file.sha256[:12]}), "
                f"{len(self.mappings)} operator mapping(s)")

    def lookup(self, ip: str | None) -> GeoHit | None:
        if not ip:
            return None
        if ip in self._cache:
            return self._cache[ip]
        hit = None
        try:
            ipaddress.ip_address(ip)
            record, prefix = self.reader.get_with_prefix_len(ip)
            key = extract_key(record, self.fields)
            if key:
                net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
                hit = GeoHit(key, str(net), record)
        except (ValueError, TypeError):
            hit = None
        self._cache[ip] = hit
        return hit

    def location_key(self, ip: str | None) -> str | None:
        h = self.lookup(ip)
        return h.key if h else None

    def mapping_for(self, key: str) -> str | None:
        return self.mappings.get(key)

    def close(self) -> None:
        try:
            self.reader.close()
        except Exception:  # noqa: BLE001
            pass


def active_file(db: Session, source_id: int) -> GeoipFile | None:
    return db.scalars(select(GeoipFile).where(GeoipFile.source_id == source_id, GeoipFile.active.is_(True))).first()


def load_provider(db: Session, source_id: int | None = None) -> GeoipProvider | None:
    stmt = select(DataSource).where(DataSource.kind == "geoip", DataSource.enabled.is_(True)).order_by(DataSource.id)
    if source_id is not None:
        stmt = stmt.where(DataSource.id == source_id)
    for src in db.scalars(stmt).all():
        gf = active_file(db, src.id)
        if gf is None:
            continue
        path = file_path(src.id, gf.sha256)
        if not os.path.exists(path):
            continue
        try:
            reader = open_reader(path)
        except Exception:  # noqa: BLE001
            continue
        maps = {m.location_key: m.site_id for m in
                db.scalars(select(GeoipMapping).where(GeoipMapping.source_id == src.id)).all()}
        return GeoipProvider(src, gf, reader, maps)
    return None


def upload_tempfile(source_id: int) -> tuple[int, str]:
    base = os.path.join(get_settings().geoip_dir, str(source_id))
    os.makedirs(base, exist_ok=True)
    return tempfile.mkstemp(dir=base, suffix=".upload")


def store_upload(db: Session, source: DataSource, filename: str, tmp: str, actor: str,
                 activate: bool = True) -> GeoipFile:
    """Validate an uploaded temp file, move it to /data/geoip/<source>/<sha>.mmdb and record the version."""
    try:
        h = hashlib.sha256()
        size = 0
        with open(tmp, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        if size == 0:
            raise ValueError("empty file")
        meta = inspect_file(tmp, geoip_fields(source))
        sha = h.hexdigest()
        final = file_path(source.id, sha)
        shutil.move(tmp, final)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    gf = db.scalars(select(GeoipFile).where(GeoipFile.source_id == source.id, GeoipFile.sha256 == sha)).first()
    if gf is None:
        gf = GeoipFile(source_id=source.id, sha256=sha, filename=os.path.basename(filename or "upload.mmdb")[:255],
                       size=size, uploaded_by=actor, meta=meta, active=False)
        db.add(gf)
        db.flush()
    if activate:
        activate_file(db, source.id, gf.id)
    return gf


def inspect_file(path: str, fields: list[str]) -> dict:
    try:
        reader = open_reader(path)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"not a valid MaxMind database: {exc}") from exc
    try:
        m = reader.metadata()
        samples: list[str] = []
        keyed = 0
        total = 0
        for i, (net, rec) in enumerate(reader):
            if i >= 5000:
                break
            total += 1
            k = extract_key(rec, fields)
            if k:
                keyed += 1
                if len(samples) < 10 and k not in samples:
                    samples.append(k)
        return {"database_type": m.database_type, "ip_version": m.ip_version, "node_count": m.node_count,
                "build_epoch": m.build_epoch, "description": (m.description or {}).get("en"),
                "sample_networks": total, "sample_with_location": keyed, "sample_keys": samples}
    finally:
        reader.close()


def activate_file(db: Session, source_id: int, file_id: int) -> GeoipFile:
    target = db.get(GeoipFile, file_id)
    if target is None or target.source_id != source_id:
        raise ValueError("file not found")
    for f in db.scalars(select(GeoipFile).where(GeoipFile.source_id == source_id)).all():
        f.active = f.id == file_id
    db.flush()
    return target


def delete_file(db: Session, source_id: int, file_id: int) -> None:
    f = db.get(GeoipFile, file_id)
    if f is None or f.source_id != source_id:
        raise ValueError("file not found")
    if f.active:
        raise ValueError("the active file cannot be deleted; activate another version first")
    path = file_path(source_id, f.sha256)
    if os.path.exists(path):
        os.unlink(path)
    db.delete(f)
