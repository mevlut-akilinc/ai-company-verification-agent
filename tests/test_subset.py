from kyb.data.readers import read_jsonl
from kyb.data.subset import ancestors, chain_depths


def _load(paths, name, sub="processed"):
    return list(read_jsonl(getattr(paths, sub) / name))


def test_chain_depths_and_cycles():
    owners = {"A": {"B"}, "B": {"C"}, "X": {"Y"}, "Y": {"X"}}
    depth, cycles = chain_depths(owners)
    assert depth["A"] == 2 and depth["B"] == 1 and depth.get("C", 0) == 0
    assert {"X", "Y"} <= cycles
    assert ancestors("A", owners) == {"B", "C"}
    assert ancestors("X", owners) == {"Y"}


def test_selection_contains_chain_and_cycle(built):
    paths, manifest = built
    numbers = {c["company_number"] for c in _load(paths, "companies.jsonl")}
    assert {"00000001", "00000002", "00000003"} <= numbers  # chain cluster
    assert {"00000010", "00000011"} <= numbers               # cycle cluster
    assert manifest["selection"]["max_depth_selected"] >= 2
    assert len(numbers) <= 15


def test_corporate_owner_resolution(built):
    paths, _ = built
    own = _load(paths, "ownerships.jsonl")
    edge_2_to_1 = [o for o in own if o["asset_company_number"] == "00000001" and o["owner_label"] == "Company"]
    assert edge_2_to_1 and edge_2_to_1[0]["owner_key"] == "00000002"
    assert edge_2_to_1[0]["resolved_by"] == "registration_number"
    assert (edge_2_to_1[0]["share_min"], edge_2_to_1[0]["share_max"]) == (75, 100)

    # The Dutch owner's '12345' must NOT be linked to UK company 00012345.
    owners_of_3 = [o for o in own if o["asset_company_number"] == "00000003"]
    assert not any(o["owner_key"] == "00012345" for o in owners_of_3)
    assert any(o["owner_label"] == "LegalEntity" for o in owners_of_3)


def test_ceased_kept_but_marked(built):
    paths, _ = built
    own = _load(paths, "ownerships.jsonl")
    ceased = [o for o in own if o["ceased_on"]]
    assert len(ceased) == 1 and ceased[0]["asset_company_number"] == "00000003"


def test_statements_and_privacy(built):
    paths, _ = built
    companies = {c["company_number"]: c for c in _load(paths, "companies.jsonl")}
    assert companies["00000002"]["psc_statements"] == ["psc-details-not-confirmed"]
    for p in _load(paths, "persons.jsonl"):
        assert "address" not in p  # data minimisation


def test_er_gold_and_lei(built):
    paths, _ = built
    gold = {g["company_number"]: g["lei"] for g in _load(paths, "er_gold_ch_gleif.jsonl", "eval")}
    assert gold.get("00000003") == "LEITOPCO000000000003"
    assert gold.get("00000021") == "LEISIMPLE00000000021"
    leis = {r["lei"] for r in _load(paths, "lei_records.jsonl")}
    assert "LEIPARENTNL000000001" in leis      # parent pulled in via RR
    assert "LEIDISTRACTOR0000001" in leis      # GB distractor
    assert "LEIFOREIGN0000000009" not in leis  # unrelated foreign record
    rels = _load(paths, "lei_relationships.jsonl")
    assert {r["relationship_type"] for r in rels} == {
        "IS_DIRECTLY_CONSOLIDATED_BY", "IS_ULTIMATELY_CONSOLIDATED_BY"}


def test_sanctions_filtered(built):
    paths, _ = built
    rows = _load(paths, "sanctions.jsonl")
    assert {r["schema"] for r in rows} == {"Person", "Company"}
    ayse = next(r for r in rows if r["id"] == "os-demo-1")
    assert "Ayşe Yılmazer" in ayse["aliases"]
