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

    def __init__(self, companies=(), leis=(), legal_entities=(), upstream=None, edges=()):
        # edges: {"asset", "label", "owner": {...}, "edge": {share_min, ...}} (store.controllers biçimi)
        self.edges = list(edges)
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

    def controllers(self, company_numbers):
        return [e for e in self.edges if e["asset"] in company_numbers]

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
    # 00000001 <- %75+ DEMO HOLDINGS LIMITED <- %50-75 Ayşe Yılmaz
    #          <- %25-50 yabancı tüzel kişi (le1 değil, çözülemeyen)
    edges=[
        {"asset": "00000001", "label": "Company",
         "owner": {"company_number": "00000002", "name": "DEMO HOLDINGS LIMITED"},
         "edge": {"share_min": 75.0, "share_max": 100.0}},
        {"asset": "00000002", "label": "Person",
         "owner": {"psc_key": "p1", "name": "Ayşe Yılmaz", "birth_year": 1980, "birth_month": 5},
         "edge": {"share_min": 50.0, "share_max": 75.0}},
        {"asset": "00000002", "label": "LegalEntity",
         "owner": {"entity_key": "le9", "name": "Offshore Ltd", "country_registered": "Jersey"},
         "edge": {"share_min": 25.0, "share_max": 50.0}},
    ],
)


@pytest.fixture(autouse=True)
def fake_graph(monkeypatch):
    from kyb.agents import nodes

    monkeypatch.setattr(nodes, "get_store", lambda: DEMO_STORE)
    return DEMO_STORE


# ----------------------------------------------------------------- küçük tarama indeksi
OS_BASLIK = ["id", "schema", "name", "aliases", "birth_date", "countries", "addresses",
             "identifiers", "sanctions", "phones", "emails", "program_ids", "dataset",
             "first_seen", "last_seen", "last_change"]

# OpenSanctions targets.simple.csv biçiminde birkaç kayıt. DEMO_STORE'daki adlarla
# (Ayşe Yılmaz, DEMO LTD, Offshore Ltd) eşleşen kayıt yok: varsayılan vaka temiz.
OS_KAYITLARI = [
    {"id": "Q7747", "schema": "Person", "name": "Vladimir Putin",
     "aliases": "Владимир Путин;PUTIN Vladimir Vladimirovich", "birth_date": "1952-10-07",
     "countries": "ru", "sanctions": "EU - Ukraine territorial integrity",
     "dataset": "EU Financial Sanctions Files (FSF)"},
    {"id": "NK-ros", "schema": "Company", "name": "Rosneft Oil Company", "aliases": "Rosneft",
     "countries": "ru", "sanctions": "OFAC SSI", "dataset": "US OFAC Consolidated List"},
    {"id": "Q-pep", "schema": "Person", "name": "John Smith", "birth_date": "1951-03-02",
     "countries": "gb", "dataset": "UK House of Commons;Wikidata Politically Exposed Persons"},
    {"id": "fcdo-1", "schema": "Person", "name": "Ivan Petrov", "birth_date": "1970",
     "countries": "ru", "dataset": "UK FCDO Sanctions List"},
    {"id": "crime-1", "schema": "Person", "name": "Carlos Mendez Ruiz", "countries": "co",
     "dataset": "Interpol Red Notices"},
    {"id": "only-cyr", "schema": "Person", "name": "Иван Иванов"},
    {"id": "ship-1", "schema": "Vessel", "name": "Vladimir Putin", "dataset": "EU Sanctions"},
]


def os_csv(path, kayitlar):
    import csv

    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OS_BASLIK)
        w.writeheader()
        for k in kayitlar:
            w.writerow({b: k.get(b, "") for b in OS_BASLIK})
    return path


def kur_tarama_indeksi(klasor, kayitlar=OS_KAYITLARI):
    from kyb.data.tarama_indeks import TaramaIndeksi, kur

    kaynak = os_csv(Path(klasor) / "os.csv", kayitlar)
    kur([kaynak], Path(klasor) / "ix.sqlite", ilerleme=False)
    return TaramaIndeksi(Path(klasor) / "ix.sqlite")


@pytest.fixture(scope="session")
def tarama_ix(tmp_path_factory):
    return kur_tarama_indeksi(tmp_path_factory.mktemp("tarama"))


@pytest.fixture(autouse=True)
def sahte_tarama(monkeypatch, tarama_ix):
    """Ajan gerçek data/processed indeksine değil, bu küçük indekse bakar."""
    from kyb.agents import nodes

    monkeypatch.setattr(nodes, "get_tarama_indeksi", lambda: tarama_ix)
    return tarama_ix
