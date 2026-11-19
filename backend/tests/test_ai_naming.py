"""AI-learned naming conventions: only validated mappings are used, everything else falls back to rules."""
import pytest

from app.connectors.catalyst_center.models import Site
from app.llm.base import LLMUnavailable
from app.usecases.ap_site.learners import SiteIndex
from app.usecases.ap_site.naming import (
    build_floor_template, build_site_naming, learn_floor_templates, learn_site_naming, safe_regex, validate,
)
from app.usecases.ap_site.scoring import SignalVote, score


class FakeLLM:
    name, label = "local", "Local AI"

    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls = answer, exc, 0

    def chat_json(self, system, user, schema, num_ctx=4096):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.answer


def _index():
    sites = [Site("g", "Global", None, "Global", "area"), Site("dk", "Denmark", "g", "Global/Denmark", "area")]
    for b, bname in (("b1", "Copenhagen HQ"), ("b2", "Aarhus Office")):
        sites.append(Site(b, bname, "dk", f"Global/Denmark/{bname}", "building"))
        for f in (1, 2):
            sites.append(Site(f"{b}f{f}", f"Floor {f}", b, f"Global/Denmark/{bname}/Floor {f}", "floor"))
    return SiteIndex(sites)


# kbh = Copenhagen, aar = Aarhus: the names do not contain the building names, so prefix matching on the
# building name cannot work; only the alias mapping does.
PAIRS = ([(f"kbh-s{f}-ap{n:02d}", f"b1f{f}") for f in (1, 2) for n in range(1, 8)]
         + [(f"aar-s{f}-ap{n:02d}", f"b2f{f}") for f in (1, 2) for n in range(1, 8)])
GOOD = {"pattern": r"^(?P<building>[a-z]+)-s(?P<floor>\d+)-ap\d+$",
        "building_aliases": [{"token": "kbh", "building": "Copenhagen HQ"}, {"token": "aar", "building": "Aarhus Office"}],
        "floor_aliases": [], "explanation": "city code, floor, number"}


class Ctx:
    def __init__(self, llm):
        from app.db import Base, SessionLocal, engine
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.db = SessionLocal()
        self.llm = llm
        self.source = type("S", (), {"id": 1})()
        self.lines = []

    def log(self, msg):
        self.lines.append(msg)


def test_valid_mapping_predicts_building_and_floor():
    idx = _index()
    n = validate(build_site_naming(GOOD, idx), PAIRS)
    assert n.accuracy == 1.0 and n.matched == len(PAIRS) and n.floor_accuracy == 1.0
    assert n.predict("KBH-S2-AP99.example.com") == ("b1", "b1f2")
    assert n.predict("osl-s1-ap01") is None, "unknown token is not guessed"


def test_learn_accepts_and_caches(monkeypatch):
    ctx = Ctx(FakeLLM(GOOD))
    assert learn_site_naming(ctx, _index(), PAIRS, "aps") is not None
    assert learn_site_naming(ctx, _index(), PAIRS, "aps") is not None
    assert ctx.llm.calls == 1, "unchanged inventory reuses the cached mapping"
    ctx.db.close()


def test_wrong_mapping_is_rejected():
    swapped = {**GOOD, "building_aliases": [{"token": "kbh", "building": "Aarhus Office"},
                                            {"token": "aar", "building": "Copenhagen HQ"}]}
    ctx = Ctx(FakeLLM(swapped))
    assert learn_site_naming(ctx, _index(), PAIRS, "aps") is None
    assert any("rejected by validation" in line for line in ctx.lines)
    ctx.db.close()


@pytest.mark.parametrize("answer,exc", [({"pattern": "(", "building_aliases": [], "floor_aliases": [],
                                          "explanation": ""}, None),
                                         (None, LLMUnavailable("down")),
                                         (None, ValueError("not json"))])
def test_bad_or_missing_answers_fall_back(answer, exc):
    ctx = Ctx(FakeLLM(answer, exc))
    assert learn_site_naming(ctx, _index(), PAIRS, "aps") is None
    ctx.db.close()


def test_no_ai_means_no_call():
    ctx = Ctx(None)
    assert learn_site_naming(ctx, _index(), PAIRS, "aps") is None
    ctx.db.close()


def test_unsafe_patterns_rejected():
    assert safe_regex(r"^(a+)+$", ("building",)) is None
    assert safe_regex(r"^x$", ("building",)) is None, "named group required"
    assert safe_regex(r"^(?P<building>[a-z]+)-", ("building",)) is not None


def test_ai_naming_alone_is_capped_and_never_auto():
    idx = _index()
    votes = {"ai_naming": SignalVote("ai_naming", {"b1": 1.0}, {"b1f1": 1.0}, "x")}
    sc = score(votes, {"ai_naming": True, "uplink": True}, {"ai_naming": 0.1, "uplink": 0.4}, idx)
    assert sc.confidence <= 60 and sc.auto_eligible is False
    assert any("AI-learned" in c for c in sc.concerns)


def test_floor_template_round_trip():
    names = ["cph-b2-f3-ap01-lobby", "cph-b2-f3-ap02-lobby", "cph-b2-f3-ap04-lobby", "printer-x"]
    ft = build_floor_template(r"^cph-b2-f3-ap(?P<num>\d+)-lobby$", "cph-b2-f3-ap{num:02d}-lobby", names)
    assert ft and ft.used == {1, 2, 4} and ft.render(3) == "cph-b2-f3-ap03-lobby"
    assert build_floor_template(r"^cph-b2-f3-ap(?P<num>\d+)-lobby$", "cph-ap{num:02d}", names) is None, \
        "template that does not rebuild the names is rejected"


def test_floor_templates_from_ai():
    names = ["dk_cph_3sal_ap7_n", "dk_cph_3sal_ap8_n", "dk_cph_3sal_ap10_n"]
    ans = {"floors": [{"floor": "F3", "pattern": r"^dk_cph_3sal_ap(?P<num>\d+)_n$", "template": "dk_cph_3sal_ap{num}_n"},
                      {"floor": "nope", "pattern": "x", "template": "y"}]}
    ctx = Ctx(FakeLLM(ans))
    out = learn_floor_templates(ctx, {"F3": names})
    assert list(out) == ["F3"] and out["F3"].render(9) == "dk_cph_3sal_ap9_n"
    ctx.db.close()
