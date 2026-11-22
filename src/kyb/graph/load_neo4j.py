"""Load the processed subset into Neo4j (idempotent: every write is a MERGE).

Graph model
-----------
(:Company {company_number})                         Companies House company
(:Person {psc_key})                                 individual PSC filing (not deduplicated yet)
(:LegalEntity {entity_key})                         corporate PSC not resolvable to a subset company
(:ProtectedPSC {psc_key})                           super-secure PSC (identity withheld by law)
(:LEIEntity {lei})                                  GLEIF Level 1 record

(owner)-[:CONTROLS {share_min, share_max, vote_min, vote_max, ...}]->(:Company)
(:LEIEntity)-[:DIRECT_PARENT | :ULTIMATE_PARENT]->(:LEIEntity)

Deliberately NOT loaded:
* Company <-> LEI links: they are the entity-resolution gold standard
  (data/eval/er_gold_ch_gleif.jsonl) and GLEIF's registration-authority entity
  ID is dropped for the same reason. Loading them would leak the answers.
* Sanctions: screening runs on its own index, not via graph traversal.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Protocol

from kyb.config import Neo4jSettings
from kyb.data.readers import read_jsonl

CONSTRAINTS = [
    "CREATE CONSTRAINT company_number IF NOT EXISTS FOR (n:Company) REQUIRE n.company_number IS UNIQUE",
    "CREATE CONSTRAINT person_key IF NOT EXISTS FOR (n:Person) REQUIRE n.psc_key IS UNIQUE",
    "CREATE CONSTRAINT legal_entity_key IF NOT EXISTS FOR (n:LegalEntity) REQUIRE n.entity_key IS UNIQUE",
    "CREATE CONSTRAINT protected_psc_key IF NOT EXISTS FOR (n:ProtectedPSC) REQUIRE n.psc_key IS UNIQUE",
    "CREATE CONSTRAINT lei IF NOT EXISTS FOR (n:LEIEntity) REQUIRE n.lei IS UNIQUE",
    "CREATE INDEX controls_rid IF NOT EXISTS FOR ()-[r:CONTROLS]-() ON (r.rid)",
    "CREATE FULLTEXT INDEX entity_names IF NOT EXISTS "
    "FOR (n:Company|Person|LegalEntity|LEIEntity) ON EACH [n.name]",
]

Q_COMPANIES = """
UNWIND $rows AS row
MERGE (c:Company {company_number: row.company_number})
SET c += row, c.ftm_schema = 'Company'
"""
Q_PERSONS = """
UNWIND $rows AS row
MERGE (p:Person {psc_key: row.psc_key})
SET p += row, p.ftm_schema = 'Person'
"""
Q_LEGAL = """
UNWIND $rows AS row
MERGE (e:LegalEntity {entity_key: row.entity_key})
SET e += row, e.ftm_schema = 'LegalEntity'
"""
Q_LEI = """
UNWIND $rows AS row
MERGE (l:LEIEntity {lei: row.lei})
SET l += row, l.ftm_schema = 'LegalEntity'
"""

# Owner label cannot be a query parameter, so there is one template per label.
_OWNER_MATCH = {
    "Person": "MATCH (o:Person {psc_key: row.owner_key})",
    "LegalEntity": "MATCH (o:LegalEntity {entity_key: row.owner_key})",
    "Company": "MATCH (o:Company {company_number: row.owner_key})",
    "Unknown": "MERGE (o:ProtectedPSC {psc_key: row.owner_key})",
}
Q_CONTROLS = """
UNWIND $rows AS row
{owner_match}
MATCH (c:Company {{company_number: row.asset_company_number}})
MERGE (o)-[r:CONTROLS {{rid: row.rid}}]->(c)
SET r += row.props
"""

_REL_TYPES = {
    "IS_DIRECTLY_CONSOLIDATED_BY": "DIRECT_PARENT",
    "IS_ULTIMATELY_CONSOLIDATED_BY": "ULTIMATE_PARENT",
}
Q_LEI_REL = """
UNWIND $rows AS row
MATCH (child:LEIEntity {{lei: row.child_lei}})
MATCH (parent:LEIEntity {{lei: row.parent_lei}})
MERGE (child)-[r:{rel_type}]->(parent)
SET r.status = row.status, r.gleif_type = row.relationship_type
"""

# Fields kept out of the graph (see module docstring).
LEI_DROP = {"ra_entity_id", "source"}
OWNERSHIP_KEYS = {"owner_label", "owner_key", "asset_company_number"}


class Sink(Protocol):
    def run(self, query: str, params: dict | None = None) -> None: ...


class Neo4jSink:
    def __init__(self, settings: Neo4jSettings | None = None):
        from neo4j import GraphDatabase

        self.s = settings or Neo4jSettings()
        self.driver = GraphDatabase.driver(self.s.uri, auth=(self.s.user, self.s.password))
        self.driver.verify_connectivity()

    def run(self, query: str, params: dict | None = None) -> None:
        self.driver.execute_query(query, params or {}, database_=self.s.database)

    def query(self, query: str, params: dict | None = None) -> list[dict]:
        records, _, _ = self.driver.execute_query(query, params or {}, database_=self.s.database)
        return [r.data() for r in records]

    def wipe(self) -> None:
        """Delete everything in batches (CALL ... IN TRANSACTIONS needs an auto-commit session)."""
        with self.driver.session(database=self.s.database) as session:
            session.run("MATCH (n) CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS").consume()

    def close(self) -> None:
        self.driver.close()


def _batches(rows: Iterable[dict], size: int) -> Iterator[list[dict]]:
    buf: list[dict] = []
    for r in rows:
        buf.append(r)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _clean(row: dict, drop: set[str] = frozenset()) -> dict:
    """Neo4j properties can't hold None inside lists or nested maps; drop Nones and nested dicts."""
    return {k: v for k, v in row.items() if k not in drop and v is not None and not isinstance(v, dict)}


def _rid(row: dict) -> str:
    basis = "|".join(str(row.get(k)) for k in
                     ("owner_label", "owner_key", "asset_company_number", "psc_kind", "notified_on"))
    return hashlib.sha1(basis.encode()).hexdigest()[:24]


def _load_nodes(sink: Sink, path: Path, query: str, batch: int, drop=frozenset()) -> int:
    if not path.exists():
        return 0
    n = 0
    for b in _batches((_clean(r, drop | {"source"}) for r in read_jsonl(path)), batch):
        sink.run(query, {"rows": b})
        n += len(b)
    return n


def load_all(sink: Sink, processed: Path, batch: int = 2000, wipe: bool = False) -> dict:
    if wipe:
        sink.wipe()
    for q in CONSTRAINTS:
        sink.run(q)

    counts = {
        "Company": _load_nodes(sink, processed / "companies.jsonl", Q_COMPANIES, batch),
        "Person": _load_nodes(sink, processed / "persons.jsonl", Q_PERSONS, batch),
        "LegalEntity": _load_nodes(sink, processed / "legal_entities.jsonl", Q_LEGAL, batch),
        "LEIEntity": _load_nodes(sink, processed / "lei_records.jsonl", Q_LEI, batch, LEI_DROP),
    }

    by_label: dict[str, list[dict]] = defaultdict(list)
    own_path = processed / "ownerships.jsonl"
    if own_path.exists():
        for r in read_jsonl(own_path):
            props = _clean({k: v for k, v in r.items() if k not in OWNERSHIP_KEYS})
            by_label[r["owner_label"]].append({
                "rid": _rid(r), "owner_key": r["owner_key"],
                "asset_company_number": r["asset_company_number"], "props": props,
            })
    counts["CONTROLS"] = 0
    for label, rows in by_label.items():
        q = Q_CONTROLS.format(owner_match=_OWNER_MATCH[label])
        for b in _batches(rows, batch):
            sink.run(q, {"rows": b})
            counts["CONTROLS"] += len(b)

    rel_path = processed / "lei_relationships.jsonl"
    counts["LEI_REL"] = 0
    if rel_path.exists():
        by_type: dict[str, list[dict]] = defaultdict(list)
        for r in read_jsonl(rel_path):
            by_type[_REL_TYPES.get(r["relationship_type"], "GLEIF_RELATION")].append(r)
        for rel_type, rows in by_type.items():
            for b in _batches(rows, batch):
                sink.run(Q_LEI_REL.format(rel_type=rel_type), {"rows": b})
                counts["LEI_REL"] += len(b)
    return counts


# A few sanity queries to run after loading (also printed by `kyb load`).
CHECK_QUERIES = {
    "node_counts": "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY n DESC",
    "deepest_chains": """
        MATCH p = (top)-[:CONTROLS*2..8]->(c:Company)
        WHERE NOT ()-[:CONTROLS]->(top)
        RETURN c.company_number AS company, c.name AS name, length(p) AS layers
        ORDER BY layers DESC LIMIT 5""",
    "circular_ownership": """
        MATCH p = (c:Company)-[:CONTROLS*2..6]->(c)
        RETURN c.company_number AS company, length(p) AS cycle_len LIMIT 5""",
}
