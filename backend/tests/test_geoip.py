import os

import maxminddb
from fastapi.testclient import TestClient

from app.connectors import geoip
from app.connectors.geoip.writer import write_mmdb
from app.models import DataSource
from app.security import encrypt_secret
from app.usecases.ap_site.learners import SiteIndex
from app.usecases.ap_site.scoring import SignalVote, score
from tests.conftest import setup_task


def test_writer_roundtrip(tmp_path):
    p = str(tmp_path / "t.mmdb")
    write_mmdb(p, [("10.1.0.0/16", {"site": "wide"}), ("10.1.2.0/24", {"site": "CPH-B03", "n": 7, "ok": True})])
    r = maxminddb.open_database(p)
    assert r.get("10.1.2.9") == {"site": "CPH-B03", "n": 7, "ok": True}
    assert r.get("10.1.9.9") == {"site": "wide"}
    assert r.get("10.2.0.1") is None
    rec, plen = r.get_with_prefix_len("10.1.2.9")
    assert plen == 24
    assert geoip.extract_key(rec, geoip.DEFAULT_FIELDS) == "CPH-B03"
    assert geoip.extract_key({"city": {"names": {"en": "Aarhus"}}}, geoip.DEFAULT_FIELDS) == "Aarhus"
    r.close()


def test_geoip_only_is_capped_and_not_auto():
    from app.connectors.catalyst_center.models import Site
    index = SiteIndex([Site(id="a", name="A", parent_id=None, hierarchy="Global/A", type="area"),
                       Site(id="b1", name="B1", parent_id="a", hierarchy="Global/A/B1", type="building"),
                       Site(id="f1", name="F1", parent_id="b1", hierarchy="Global/A/B1/F1", type="floor")])
    w = {"uplink": .4, "rf": .3, "name": .15, "tag": .1, "subnet": .05, "geoip": .15}
    avail = {k: True for k in w}
    sc = score({"geoip": SignalVote("geoip", {"b1": 1.0}, {}, "x")}, avail, w, index, require_floor=False)
    assert sc.confidence <= 60 and sc.auto_eligible is False
    sc2 = score({"geoip": SignalVote("geoip", {"b1": 1.0}), "name": SignalVote("name", {"b1": 1.0})},
                avail, w, index, require_floor=False)
    assert sc2.auto_eligible is True
    # Absent GeoIP never lowers coverage.
    no_geo = score({"name": SignalVote("name", {"b1": 1.0})}, {**avail, "geoip": False}, w, index, False)
    assert no_geo.coverage == 1.0
    assert next(e for e in no_geo.evidence if e["signal"] == "geoip")["detail"] == "not configured"


def _upload(db, mock_cc, tmp_path, name="geo"):
    os.environ["GEOIP_DIR"] = str(tmp_path)
    from app.config import get_settings
    get_settings.cache_clear()
    src = DataSource(name=name, kind="geoip", base_url="", username="", secret_enc=encrypt_secret(""))
    db.add(src)
    db.flush()
    data = TestClient(mock_cc.app).get("/mock/geoip.mmdb").content
    fd, tmp = geoip.upload_tempfile(src.id)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return src, geoip.store_upload(db, src, "mock.mmdb", tmp, "test")


def test_geoip_versions_and_signal(mock_cc, tmp_path):
    db, defn, cfg, ctx = setup_task(mock_cc, "ap_site_assignment")
    src, f1 = _upload(db, mock_cc, tmp_path)
    assert f1.active and f1.meta["sample_with_location"] > 0
    assert os.path.exists(geoip.file_path(src.id, f1.sha256))

    # A second version, then roll back to the first.
    p2 = str(tmp_path / "v2.mmdb")
    write_mmdb(p2, [("192.0.2.0/24", {"site": "lab"})])
    fd, tmp = geoip.upload_tempfile(src.id)
    with os.fdopen(fd, "wb") as fh:
        fh.write(open(p2, "rb").read())
    f2 = geoip.store_upload(db, src, "v2.mmdb", tmp, "test")
    assert f2.active and not f1.active
    geoip.activate_file(db, src.id, f1.id)
    db.flush()
    assert geoip.active_file(db, src.id).id == f1.id
    geoip.delete_file(db, src.id, f2.id)
    try:
        geoip.delete_file(db, src.id, f1.id)
        assert False, "active file must not be deletable"
    except ValueError:
        pass

    result = defn.analyze(ctx)
    assert result.stats["signals_available"]["geoip"] is True
    with_geo = [p for p in result.proposals
                if next(e for e in p.evidence if e["signal"] == "geoip")["buildings"]]
    assert with_geo, "GeoIP should vote for unassigned APs in covered buildings"
    truth = mock_cc.estate.truth
    sites = mock_cc.SITES
    ok = sum(next(e for e in p.evidence if e["signal"] == "geoip")["buildings"][0]["site_id"]
             == sites[truth[p.target_key]]["parentId"] for p in with_geo)
    assert ok / len(with_geo) >= 0.95
    db.close()
