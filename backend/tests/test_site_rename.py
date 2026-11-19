"""Site renames done by delete and re-create: rules pair the obvious ones, AI only confirms the grey zone."""
from app.usecases.site_hierarchy import match_recreated, norm_site_name


class FakeLLM:
    name, label = "cloud", "Cloud AI"

    def __init__(self, same_ids):
        self.same_ids, self.calls = same_ids, 0

    def chat_json(self, system, user, schema, num_ctx=4096):
        self.calls += 1
        return {"pairs": [{"id": i, "same": True, "reason": "abbreviation"} for i in self.same_ids]}


class Ctx:
    def __init__(self, llm=None):
        from app.db import Base, SessionLocal, engine
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        self.db, self.llm, self.lines = SessionLocal(), llm, []
        self.source = type("S", (), {"id": 1})()

    def log(self, msg):
        self.lines.append(msg)


def site(sid, name, parent="p", typ="building"):
    return {"id": sid, "name": name, "parent_id": parent, "type": typ, "hierarchy": f"Global/Area/{name}"}


P = {"rename_min_similarity": 0.8, "rename_ai_min_similarity": 0.4}


def test_normalising():
    assert norm_site_name("Bldg 2") == norm_site_name("Building 02") == norm_site_name("building-2")
    assert norm_site_name("Flr 3") == norm_site_name("Floor 03")


def test_rules_pair_obvious_renames_without_ai():
    ctx = Ctx()
    removed = [site("o1", "Bldg 2"), site("o2", "Copenhagen Harbour"), site("o3", "Floor 3", typ="floor")]
    added = [site("n1", "Building 02"), site("n2", "Copenhagen Harbor"), site("n3", "Floor 4", typ="floor")]
    m = {(x["before"]["id"], x["after"]["id"]): x for x in match_recreated(ctx, removed, added, P)}
    assert m[("o1", "n1")]["method"] == "exact" and m[("o1", "n1")]["confidence"] == 90
    assert m[("o2", "n2")]["method"] == "fuzzy"
    assert ("o3", "n3") not in m, "different floor numbers are never a rename"
    ctx.db.close()


def test_different_parent_or_type_not_paired():
    ctx = Ctx()
    assert not match_recreated(ctx, [site("o1", "Bldg 2", parent="a")], [site("n1", "Building 2", parent="b")], P)
    assert not match_recreated(ctx, [site("o1", "Bldg 2")], [site("n1", "Bldg 2", typ="floor")], P)
    ctx.db.close()


def test_grey_zone_needs_ai():
    removed, added = [site("o1", "CPH Head Office")], [site("n1", "Copenhagen Head Office")]
    ctx = Ctx()
    assert not match_recreated(ctx, removed, added, P)
    assert any("AI is off" in line for line in ctx.lines)
    ctx.db.close()

    ctx = Ctx(FakeLLM([0]))
    m = match_recreated(ctx, removed, added, P)
    assert len(m) == 1 and m[0]["method"] == "ai" and m[0]["confidence"] == 65 and m[0]["provider"] == "Cloud AI"
    match_recreated(ctx, removed, added, P)
    assert ctx.llm.calls == 1, "same question answered from cache"
    ctx.db.close()


def test_ai_rejection_keeps_them_separate():
    ctx = Ctx(FakeLLM([]))
    assert not match_recreated(ctx, [site("o1", "CPH Head Office")], [site("n1", "Copenhagen Head Office")], P)
    ctx.db.close()
