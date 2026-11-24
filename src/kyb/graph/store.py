"""Ajanların graf erişimi: sadece okuma, birkaç dar sorgu.

Ajanlar Cypher yazmaz, bu sınıfın metodlarını çağırır. Testlerde aynı arayüzü
sağlayan sahte bir store kullanılır (Neo4j gerekmez).
"""

from __future__ import annotations

from typing import Protocol

from kyb.config import Neo4jSettings

FULLTEXT_INDEX = "entity_names"


class GraphStore(Protocol):
    def company(self, company_number: str) -> dict | None: ...
    def search(self, label: str, query: str, limit: int = 10) -> list[dict]: ...
    def upstream_legal_entities(self, company_number: str, max_depth: int = 10) -> list[dict]: ...
    def controllers(self, company_numbers: list[str]) -> list[dict]: ...


class Neo4jStore:
    def __init__(self, settings: Neo4jSettings | None = None):
        from neo4j import GraphDatabase

        self.s = settings or Neo4jSettings()
        # Sunucu uyarıları (ör. hiç yazılmamış özellik adı) loglara dökülmesin.
        self.driver = GraphDatabase.driver(self.s.uri, auth=(self.s.user, self.s.password),
                                           connection_timeout=5,
                                           notifications_min_severity="OFF")
        self.driver.verify_connectivity()

    def _q(self, query: str, **params) -> list[dict]:
        records, _, _ = self.driver.execute_query(query, params, database_=self.s.database)
        return [r.data() for r in records]

    def company(self, company_number: str) -> dict | None:
        rows = self._q("MATCH (c:Company {company_number: $n}) RETURN properties(c) AS c",
                       n=company_number)
        return rows[0]["c"] if rows else None

    def search(self, label: str, query: str, limit: int = 10) -> list[dict]:
        """Fulltext adayları. label: Company | LEIEntity | Person | LegalEntity."""
        if label not in {"Company", "LEIEntity", "Person", "LegalEntity"}:
            raise ValueError(label)
        rows = self._q(
            f"CALL db.index.fulltext.queryNodes('{FULLTEXT_INDEX}', $q) YIELD node, score "
            f"WHERE node:{label} RETURN properties(node) AS n, score LIMIT $limit",
            q=query, limit=limit)
        return [{**r["n"], "_ft_score": r["score"]} for r in rows]

    def upstream_legal_entities(self, company_number: str, max_depth: int = 10) -> list[dict]:
        """Konu şirketin sahiplik zincirindeki, sicilde çözülemeyen tüzel sahipler."""
        rows = self._q(
            f"MATCH p = (le:LegalEntity)-[:CONTROLS*1..{int(max_depth)}]->"
            "(:Company {company_number: $n}) "
            "WHERE all(r IN relationships(p) WHERE r.ceased_on IS NULL) "  # sadece aktif zincir
            "RETURN DISTINCT properties(le) AS le",
            n=company_number)
        return [r["le"] for r in rows]

    def controllers(self, company_numbers: list[str]) -> list[dict]:
        """Verilen şirketlere gelen AKTİF kontrol kenarları (sona erenler hariç)."""
        return self._q(
            "MATCH (o)-[r:CONTROLS]->(c:Company) "
            "WHERE c.company_number IN $nums AND r.ceased_on IS NULL "
            "RETURN c.company_number AS asset, labels(o)[0] AS label, properties(o) AS owner, "
            "properties(r) AS edge",
            nums=list(company_numbers))

    def close(self) -> None:
        self.driver.close()


_store: GraphStore | None = None


def get_store() -> GraphStore:
    """Süreç başına tek bağlantı. Neo4j kapalıysa hata fırlatır; çağıran yakalar."""
    global _store
    if _store is None:
        _store = Neo4jStore()
    return _store
