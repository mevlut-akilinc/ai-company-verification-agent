import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from make_fixtures import write_fixtures  # noqa: E402

from kyb.config import Paths  # noqa: E402
from kyb.data.subset import SubsetParams, build_subset  # noqa: E402


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    data = tmp_path_factory.mktemp("data")
    paths = Paths(data=data).ensure()
    write_fixtures(paths.raw)
    params = SubsetParams(target=15, min_chain_depth=2, distractor_leis=5, cycle_budget_ratio=0.2)
    manifest = build_subset(paths, params)
    return paths, manifest


# ----------------------------------------------------------------- sahte graf (Neo4j gerekmez)
class FakeStore:
    """GraphStore arayüzü; fulltext yerine basit kelime eşleşmesi."""

    def __init__(self, companies=(), leis=(), legal_entities=(), upstream=None):
        self.companies = {c["company_number"]: c for c in companies}
        self.leis = list(leis)
        self.legal = list(legal_entities)
        self.upstream = upstream or {}  # company_number -> [entity_key]

    def company(self, company_number):
        return self.companies.get(company_number)

    def search(self, label, query, limit=10):
        havuz = {"Company": self.companies.values(), "LEIEntity": self.leis,
                 "LegalEntity": self.legal}.get(label, [])
        kelimeler = {k.replace("\\", "").upper() for k in query.split()}
        return [n for n in havuz if kelimeler & set((n.get("name") or "").upper().split())][:limit]

    def upstream_legal_entities(self, company_number, max_depth=10):
        keys = self.upstream.get(company_number, [])
        return [le for le in self.legal if le["entity_key"] in keys]


DEMO_STORE = FakeStore(
    companies=[{"company_number": "00000001", "name": "DEMO LTD"},
               {"company_number": "00000002", "name": "DEMO HOLDINGS LIMITED"}],
    leis=[{"lei": "LEI0000000000000DEMO", "name": "DEMO LIMITED", "country": "GB",
           "ra_id": "RA000585"}],
    legal_entities=[{"entity_key": "le1", "name": "Demo Holdings Ltd",
                     "country_registered": "England"}],
    upstream={"00000001": ["le1"]},
)


@pytest.fixture(autouse=True)
def fake_graph(monkeypatch):
    from kyb.agents import nodes

    monkeypatch.setattr(nodes, "get_store", lambda: DEMO_STORE)
    return DEMO_STORE
