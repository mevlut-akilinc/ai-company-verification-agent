from kyb.graph.load_neo4j import load_all


class FakeSink:
    def __init__(self):
        self.calls = []

    def run(self, query, params=None):
        self.calls.append((query, params or {}))

    def wipe(self):
        self.calls.append(("WIPE", {}))


def test_loader_queries(built):
    paths, manifest = built
    sink = FakeSink()
    counts = load_all(sink, paths.processed, batch=4, wipe=True)

    assert sink.calls[0][0] == "WIPE"
    assert "CREATE CONSTRAINT" in sink.calls[1][0]
    assert counts["Company"] == manifest["counts"]["companies"]
    assert counts["CONTROLS"] == manifest["counts"]["ownerships"]

    # GLEIF registration-authority entity id must never reach the graph (ER gold leak).
    for q, p in sink.calls:
        if "LEIEntity {lei: row.lei}" in q:
            assert all("ra_entity_id" not in row for row in p["rows"])

    # Batching respected and no None values sent.
    for q, p in sink.calls:
        for row in p.get("rows", []):
            assert len(p["rows"]) <= 4
            assert None not in row.values()

    owner_queries = {q.split("\n")[2].strip() for q, _ in sink.calls if "MERGE (o)-[r:CONTROLS" in q}
    assert any(":Company {company_number: row.owner_key}" in q for q in owner_queries)
    assert any(":ProtectedPSC" in q for q in owner_queries)  # super-secure PSC on 00000021
    assert any("DIRECT_PARENT" in q for q, _ in sink.calls)
