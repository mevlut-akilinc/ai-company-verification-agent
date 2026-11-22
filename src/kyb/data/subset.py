"""Build a development subset from the raw bulk files.

A random sample of companies would contain almost no multi-layer ownership
chains, which are exactly what the UBO agent must handle. So the subset is
built *around chains*:

1. GLEIF pass A      map UK company number -> LEI (RA000585 records)
2. PSC pass 1        collect active corporate-owner edges (child <- owner) and
                     reservoir-sample simple, individually owned companies
3. Selection         ~60% chain clusters (seed + all corporate ancestors, cycles
                     included on purpose), the rest simple companies, part of
                     them with an LEI so entity resolution has gold pairs
4. PSC pass 2        full PSC records for selected companies
5. Basic data pass   company details for selected companies
6. GLEIF pass B      LEI records for linked companies, their parents, plus
                     unlinked GB distractors (so ER is not trivially easy)

Outputs land in data/processed/*.jsonl, gold ER pairs in data/eval/.
Gold pairs are NOT written into the graph, otherwise the ER evaluation leaks.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from tqdm import tqdm

from kyb.config import GLEIF_RA_COMPANIES_HOUSE, Paths
from kyb.data.readers import iter_csv, iter_psc, require_columns, write_jsonl
from kyb.models import (
    Company, LegalEntity, LEIRecord, LEIRelationship, Ownership, Person, SanctionTarget,
)
from kyb.normalize import looks_uk_registered, normalize_company_number, parse_natures

# --------------------------------------------------------------------------- columns
GLEIF_COLS = {
    "lei": "LEI",
    "name": "Entity.LegalName",
    "country": "Entity.LegalAddress.Country",
    "jurisdiction": "Entity.LegalJurisdiction",
    "entity_status": "Entity.EntityStatus",
    "registration_status": "Registration.RegistrationStatus",
    "ra_id": "Entity.RegistrationAuthority.RegistrationAuthorityID",
    "ra_entity_id": "Entity.RegistrationAuthority.RegistrationAuthorityEntityID",
}
GLEIF_REQUIRED = ("lei", "name", "ra_id", "ra_entity_id")

RR_COLS = {
    "child": "Relationship.StartNode.NodeID",
    "parent": "Relationship.EndNode.NodeID",
    "type": "Relationship.RelationshipType",
    "status": "Relationship.RelationshipStatus",
}

CH_COLS = {
    "number": "CompanyNumber",
    "name": "CompanyName",
    "status": "CompanyStatus",
    "category": "CompanyCategory",
    "inc": "IncorporationDate",
    "country": "RegAddress.Country",
    "postcode": "RegAddress.PostCode",
}
CH_SIC = [f"SICCode.SicText_{i}" for i in range(1, 5)]

# --------------------------------------------------------------------------- PSC kinds
INDIVIDUAL = {"individual-person-with-significant-control", "individual-beneficial-owner"}
CORPORATE = {"corporate-entity-person-with-significant-control", "corporate-entity-beneficial-owner"}
LEGAL = {"legal-person-person-with-significant-control", "legal-person-beneficial-owner"}
SECURE = {"super-secure-person-with-significant-control", "super-secure-beneficial-owner"}
STATEMENT = "persons-with-significant-control-statement"


@dataclass
class SubsetParams:
    target: int = 10_000          # approx. number of companies
    complex_ratio: float = 0.6    # share of companies coming from ownership chains
    lei_ratio: float = 0.15       # share of simple companies that must have an LEI
    min_chain_depth: int = 2      # corporate layers above a seed
    max_cluster: int = 200        # skip giant groups so one conglomerate can't eat the budget
    cycle_budget_ratio: float = 0.03
    distractor_leis: int = 3_000  # unlinked GB LEI records for realistic ER
    seed: int = 42


# --------------------------------------------------------------------------- helpers
def _glob(raw: Path, sub: str) -> list[Path]:
    d = raw / sub
    if not d.exists():
        return []
    return sorted(p for p in d.iterdir() if p.suffix.lower() in (".zip", ".csv", ".json", ".jsonl"))


def _psc_key(company_number: str, data: dict) -> str:
    self_link = (data.get("links") or {}).get("self") or ""
    if self_link:
        return self_link.rstrip("/").split("/")[-1]
    basis = f"{company_number}|{data.get('kind')}|{data.get('name')}|{data.get('notified_on')}"
    return "h_" + hashlib.sha1(basis.encode()).hexdigest()[:20]


def _corporate_owner_number(data: dict) -> str | None:
    ident = data.get("identification") or {}
    if not looks_uk_registered(ident):
        return None
    return normalize_company_number(ident.get("registration_number"))


class Reservoir:
    def __init__(self, k: int, rng: random.Random):
        self.k, self.rng, self.items, self.n = k, rng, [], 0

    def add(self, item) -> None:
        self.n += 1
        if len(self.items) < self.k:
            self.items.append(item)
        else:
            j = self.rng.randrange(self.n)
            if j < self.k:
                self.items[j] = item


def chain_depths(owners: dict[str, set[str]]) -> tuple[dict[str, int], set[str]]:
    """Longest corporate chain above each company (iterative DFS, cycle-safe).

    depth = 0: no corporate owner; 1: owned by a company with no corporate owner; ...
    Nodes on a cycle are reported separately; cycle edges don't add depth.
    """
    depth: dict[str, int] = {}
    state: dict[str, int] = {}  # 0 = on stack, 1 = done
    cycles: set[str] = set()
    for start in owners:
        if start in state:
            continue
        state[start] = 0
        stack = [(start, iter(owners.get(start, ())))]
        while stack:
            node, it = stack[-1]
            pushed = False
            for o in it:
                st = state.get(o)
                if st is None:
                    state[o] = 0
                    stack.append((o, iter(owners.get(o, ()))))
                    pushed = True
                    break
                if st == 0:
                    cycles.update((o, node))
            if not pushed:
                stack.pop()
                state[node] = 1
                depth[node] = max(
                    (depth.get(o, 0) + 1 for o in owners.get(node, ()) if state.get(o) == 1),
                    default=0,
                )
    return depth, cycles


def ancestors(node: str, owners: dict[str, set[str]]) -> set[str]:
    seen, frontier = set(), [node]
    while frontier:
        cur = frontier.pop()
        for o in owners.get(cur, ()):
            if o not in seen:
                seen.add(o)
                frontier.append(o)
    seen.discard(node)
    return seen


# --------------------------------------------------------------------------- passes
def gleif_ch_map(lei_files: list[Path]) -> dict[str, str]:
    """UK company number -> LEI, from GLEIF records registered at Companies House."""
    out: dict[str, str] = {}
    checked = False
    for row in tqdm(iter_csv(lei_files), desc="GLEIF pass A", unit=" rec"):
        if not checked:
            require_columns(row, [GLEIF_COLS[c] for c in GLEIF_REQUIRED], "GLEIF LEI-CDF")
            checked = True
        if row.get(GLEIF_COLS["ra_id"]) != GLEIF_RA_COMPANIES_HOUSE:
            continue
        cn = normalize_company_number(row.get(GLEIF_COLS["ra_entity_id"]))
        if cn:
            out.setdefault(cn, row[GLEIF_COLS["lei"]])
    return out


def psc_pass1(psc_files, ch_lei, params, rng):
    owners: dict[str, set[str]] = defaultdict(set)
    owner_names: dict[str, str] = {}
    simple = Reservoir(params.target * 2, rng)
    with_lei = Reservoir(params.target, rng)
    for cn_raw, data in tqdm(iter_psc(psc_files), desc="PSC pass 1", unit=" rec"):
        cn = normalize_company_number(cn_raw)
        if not cn or data.get("ceased_on"):
            continue
        kind = data.get("kind", "")
        if kind in CORPORATE:
            owner = _corporate_owner_number(data)
            if owner and owner != cn:
                owners[cn].add(owner)
                owner_names.setdefault(owner, data.get("name") or "")
        elif kind in INDIVIDUAL:
            simple.add(cn)
            if cn in ch_lei:
                with_lei.add(cn)
    return dict(owners), owner_names, simple.items, with_lei.items


def select_companies(owners, ch_lei, simple, with_lei, params, rng) -> tuple[set[str], dict]:
    depth, cycles = chain_depths(owners)
    selected: set[str] = set()
    stats = {"cycle_nodes_total": len(cycles), "chain_seeds_total": 0}

    # 1) a few cycle clusters on purpose (circular ownership is a classic red flag)
    cycle_budget = int(params.target * params.cycle_budget_ratio)
    cyc = sorted(cycles)
    rng.shuffle(cyc)
    for c in cyc:
        if len(selected) >= cycle_budget:
            break
        cl = {c} | ancestors(c, owners)
        if len(cl) <= params.max_cluster:
            selected |= cl

    # 2) chain clusters: deepest first, LEI-linked preferred, random tie-break
    complex_budget = int(params.target * params.complex_ratio)
    seeds = [c for c, d in depth.items() if d >= params.min_chain_depth]
    stats["chain_seeds_total"] = len(seeds)
    seeds.sort(key=lambda c: (-min(depth[c], 6), c not in ch_lei, rng.random()))
    for s in seeds:
        if len(selected) >= complex_budget:
            break
        if s in selected:
            continue
        cl = {s} | ancestors(s, owners)
        if len(cl) <= params.max_cluster:
            selected |= cl
    stats["from_chains"] = len(selected)

    # 3) simple companies (no corporate owner); some with LEI for ER gold pairs
    lei_budget = int((params.target - len(selected)) * params.lei_ratio)
    added_lei = 0
    for c in dict.fromkeys(with_lei):
        if added_lei >= lei_budget:
            break
        if c not in selected and c not in owners:
            selected.add(c)
            added_lei += 1
    for c in dict.fromkeys(simple):
        if len(selected) >= params.target:
            break
        if c not in owners:
            selected.add(c)
    stats["simple_with_lei"] = added_lei
    stats["max_depth_selected"] = max((depth.get(c, 0) for c in selected), default=0)
    stats["cycle_nodes_selected"] = len(selected & cycles)
    return selected, stats


def psc_pass2(psc_files, selected: set[str]):
    persons: dict[str, dict] = {}
    legal: dict[str, dict] = {}
    ownerships: list[dict] = []
    statements: dict[str, list[str]] = defaultdict(list)
    for cn_raw, data in tqdm(iter_psc(psc_files), desc="PSC pass 2", unit=" rec"):
        cn = normalize_company_number(cn_raw)
        if cn not in selected:
            continue
        kind = data.get("kind", "")
        if kind == STATEMENT:
            if not data.get("ceased_on") and data.get("statement"):
                statements[cn].append(data["statement"])
            continue
        if kind not in INDIVIDUAL | CORPORATE | LEGAL | SECURE:
            continue  # exemptions etc.

        key = _psc_key(cn, data)
        resolved_by = None
        if kind in INDIVIDUAL:
            dob = data.get("date_of_birth") or {}
            persons[key] = Person(
                psc_key=key, name=data.get("name") or "",
                nationality=data.get("nationality"),
                country_of_residence=data.get("country_of_residence"),
                birth_year=dob.get("year"), birth_month=dob.get("month"),
            ).model_dump()
            label, owner_key = "Person", key
        elif kind in SECURE:
            label, owner_key = "Unknown", key
        else:
            owner_num = _corporate_owner_number(data) if kind in CORPORATE else None
            if owner_num and owner_num in selected and owner_num != cn:
                label, owner_key, resolved_by = "Company", owner_num, "registration_number"
            else:
                ident = data.get("identification") or {}
                legal[key] = LegalEntity(
                    entity_key=key, name=data.get("name") or "", kind=kind,
                    registration_number=ident.get("registration_number"),
                    country_registered=ident.get("country_registered") or ident.get("place_registered"),
                    legal_form=ident.get("legal_form"),
                ).model_dump()
                label, owner_key = "LegalEntity", key

        ctrl = parse_natures(data.get("natures_of_control"))
        ownerships.append(Ownership(
            owner_label=label, owner_key=owner_key, asset_company_number=cn, psc_kind=kind,
            natures_of_control=data.get("natures_of_control") or [],
            notified_on=data.get("notified_on"), ceased_on=data.get("ceased_on"),
            resolved_by=resolved_by,
            **{k: v for k, v in ctrl.as_dict().items() if k != "unknown_natures"},
        ).model_dump())
    return persons, legal, ownerships, statements


def basic_pass(ch_files, selected, owner_names, statements) -> list[dict]:
    found: dict[str, dict] = {}
    checked = False
    for row in tqdm(iter_csv(ch_files), desc="Basic company data", unit=" rec"):
        if not checked:
            require_columns(row, [CH_COLS["number"], CH_COLS["name"]], "Companies House basic data")
            checked = True
        cn = normalize_company_number(row.get(CH_COLS["number"]))
        if cn not in selected:
            continue
        found[cn] = Company(
            company_number=cn, name=row.get(CH_COLS["name"], ""),
            status=row.get(CH_COLS["status"]) or None,
            category=row.get(CH_COLS["category"]) or None,
            incorporation_date=row.get(CH_COLS["inc"]) or None,
            country=row.get(CH_COLS["country"]) or None,
            postcode=row.get(CH_COLS["postcode"]) or None,
            sic_codes=[row[c] for c in CH_SIC if row.get(c) and row[c].lower() != "none supplied"],
            psc_statements=statements.get(cn, []),
        ).model_dump()
    # Owners that are not (or no longer) in the live register, e.g. dissolved.
    for cn in selected - found.keys():
        found[cn] = Company(
            company_number=cn, name=owner_names.get(cn) or "UNKNOWN",
            status="not_in_basic_data", psc_statements=statements.get(cn, []),
        ).model_dump()
    return list(found.values())


def gleif_pass_b(lei_files, rr_files, needed_seed: set[str], params, rng):
    rels: list[dict] = []
    parents: dict[str, set[str]] = defaultdict(set)
    if rr_files:
        checked = False
        for row in tqdm(iter_csv(rr_files), desc="GLEIF RR", unit=" rec"):
            if not checked:
                require_columns(row, [RR_COLS["child"], RR_COLS["parent"], RR_COLS["type"]], "GLEIF RR-CDF")
                checked = True
            r = LEIRelationship(
                child_lei=row[RR_COLS["child"]], parent_lei=row[RR_COLS["parent"]],
                relationship_type=row[RR_COLS["type"]], status=row.get(RR_COLS["status"]) or None,
            )
            parents[r.child_lei].add(r.parent_lei)
            rels.append(r.model_dump())

    needed = set(needed_seed)
    frontier = list(needed)
    while frontier:  # walk up the consolidation tree
        cur = frontier.pop()
        for p in parents.get(cur, ()):
            if p not in needed:
                needed.add(p)
                frontier.append(p)
    rels = [r for r in rels if r["child_lei"] in needed and r["parent_lei"] in needed]

    records: list[dict] = []
    distractors = Reservoir(params.distractor_leis, rng)
    for row in tqdm(iter_csv(lei_files), desc="GLEIF pass B", unit=" rec"):
        lei = row.get(GLEIF_COLS["lei"])
        rec = None
        if lei and (lei in needed or row.get(GLEIF_COLS["country"]) == "GB"):
            kw = {k: (row.get(col) or None) for k, col in GLEIF_COLS.items()}
            kw["name"] = kw["name"] or ""
            rec = LEIRecord(**kw).model_dump()
        if rec is None:
            continue
        if lei in needed:
            records.append(rec)
        else:
            distractors.add(rec)
    records.extend(distractors.items)
    return records, rels


def process_opensanctions(files: list[Path], out: Path,
                          schemas=("Person", "Company", "Organization", "LegalEntity")) -> int:
    def split(v: str) -> list[str]:
        return [x.strip() for x in (v or "").split(";") if x.strip()]

    def rows():
        for row in tqdm(iter_csv(files), desc="OpenSanctions", unit=" rec"):
            if row.get("schema") not in schemas or not row.get("name"):
                continue
            yield SanctionTarget(
                id=row["id"], schema=row["schema"], name=row["name"],
                aliases=split(row.get("aliases")), birth_date=split(row.get("birth_date")),
                countries=split(row.get("countries")), identifiers=split(row.get("identifiers")),
                sanctions=split(row.get("sanctions")), datasets=split(row.get("dataset")),
            ).model_dump(by_alias=True)

    return write_jsonl(out, rows())


# --------------------------------------------------------------------------- main
def build_subset(paths: Paths, params: SubsetParams) -> dict:
    paths.ensure()
    t0 = time.time()
    rng = random.Random(params.seed)
    raw = paths.raw
    psc_files = _glob(raw, "ch_psc")
    ch_files = _glob(raw, "ch_basic")
    lei_files = _glob(raw, "gleif_lei")
    rr_files = _glob(raw, "gleif_rr")
    os_files = _glob(raw, "opensanctions")
    if not psc_files:
        raise FileNotFoundError(f"No PSC files in {raw / 'ch_psc'}. Run `kyb download` first.")

    ch_lei = gleif_ch_map(lei_files) if lei_files else {}
    if not lei_files:
        print("! GLEIF files missing: no LEI linkage, no ER gold pairs")

    owners, owner_names, simple, with_lei = psc_pass1(psc_files, ch_lei, params, rng)
    selected, sel_stats = select_companies(owners, ch_lei, simple, with_lei, params, rng)
    persons, legal, ownerships, statements = psc_pass2(psc_files, selected)
    if not ch_files:
        print("! Basic company data missing: company details will be minimal")
    companies = basic_pass(ch_files, selected, owner_names, statements)

    gold = [
        {"company_number": c, "lei": ch_lei[c]} for c in sorted(selected) if c in ch_lei
    ]
    lei_records, lei_rels = ([], [])
    if lei_files:
        lei_records, lei_rels = gleif_pass_b(lei_files, rr_files, {g["lei"] for g in gold}, params, rng)

    p = paths.processed
    counts = {
        "companies": write_jsonl(p / "companies.jsonl", companies),
        "persons": write_jsonl(p / "persons.jsonl", persons.values()),
        "legal_entities": write_jsonl(p / "legal_entities.jsonl", legal.values()),
        "ownerships": write_jsonl(p / "ownerships.jsonl", ownerships),
        "lei_records": write_jsonl(p / "lei_records.jsonl", lei_records),
        "lei_relationships": write_jsonl(p / "lei_relationships.jsonl", lei_rels),
        "er_gold_pairs": write_jsonl(paths.eval / "er_gold_ch_gleif.jsonl", gold),
    }
    if os_files:
        counts["sanctions"] = process_opensanctions(os_files, p / "sanctions.jsonl")

    manifest = {
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "seconds": round(time.time() - t0, 1),
        "params": asdict(params),
        "selection": sel_stats,
        "counts": counts,
        "sources": {k: [f.name for f in v] for k, v in {
            "ch_psc": psc_files, "ch_basic": ch_files, "gleif_lei": lei_files,
            "gleif_rr": rr_files, "opensanctions": os_files}.items()},
    }
    (p / "subset_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest
