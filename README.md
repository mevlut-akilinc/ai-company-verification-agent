# Agentic KYB Analyst

Multi-agent Know-Your-Business pipeline: document extraction, entity resolution,
beneficial-ownership (UBO) graph analytics and sanctions screening, with a human
analyst making the final decision.

**Design rule:** the LLM never decides. Code computes every number (ownership
percentages, risk score), and a human approves, rejects or sends the case back.

> Status: **Phase 0**, the data layer and the end-to-end flow are in place.
> Agent nodes marked `[stub]` in the audit trail return fixed outputs with the
> final shape and get replaced one by one.

```
               ┌──────────────── supervisor (deterministic router) ────────────────┐
START ─► document ─► entity resolution ─► UBO graph ─► screening ─► risk score ─► report ─► human review ─► END
                                                                                     ▲            │
                                                                                     └─ request_changes (max 2)
```

## Data (public sources only)

| Source | Used for | Licence note |
|---|---|---|
| UK Companies House PSC snapshot | ownership edges, UBO | Open Government Licence |
| UK Companies House basic company data | company details | Open Government Licence |
| GLEIF Golden Copy (Level 1 + Level 2) | LEI records, parent relationships, **ER gold pairs** | CC0 |
| OpenSanctions `targets.simple.csv` | screening | CC BY-NC 4.0: non-commercial use only |
| Synthetic Turkish registry documents | document extraction | generated, no real people |

These registers contain names of real individuals (PSCs, sanctioned persons, PEPs).
The project keeps only the fields KYB needs (no addresses, birth month/year only),
and public demos use synthetic cases.

### Subset strategy
A random sample would contain almost no multi-layer ownership chains, so
`kyb subset` builds the subset around them: ~60% chain clusters (seed company
plus all corporate ancestors, circular ownership included on purpose), the rest
simple companies, some with an LEI. Corporate owners are linked by registry
number only when the PSC record points to a UK register; otherwise a foreign
number such as `12345` would silently collide with UK company `00012345`.

### Evaluation leakage guard
Company↔LEI links derived from GLEIF's registration-authority ID are written
only to `data/eval/er_gold_ch_gleif.jsonl` and never loaded into the graph. The
entity-resolution agent must find them from names and addresses.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env

# 0) try everything offline on synthetic fixtures (seconds)
python scripts/make_fixtures.py data/raw
kyb subset --target 15
pytest -q

# 1) real data (~3.5 GB download; remove the fixture files in data/raw first)
kyb download                       # or: --sources ch_psc ch_basic --psc-parts 1-5
kyb subset --target 10000          # two passes over the PSC file, expect 15-40 min

# 2) graph
docker compose up -d               # Neo4j browser: http://localhost:7474
kyb load --wipe                    # prints node counts, deepest chains, cycles

# 3) flow with human approval
kyb case                           # terminal
kyb ui                             # Streamlit approval screen
```

## Layout

```
src/kyb/
  config.py            paths, Neo4j settings, UBO threshold (env-configurable)
  normalize.py         company numbers, PSC natures-of-control -> share/vote intervals
  models.py            FtM-aligned Pydantic models
  data/download.py     date-aware bulk downloader with resume
  data/subset.py       chain-centred subset + ER gold pairs + sanctions processing
  graph/load_neo4j.py  idempotent MERGE loader + sanity queries
  agents/              LangGraph state, nodes, supervisor, interrupt() review
  ui/app.py            analyst approval screen
scripts/make_fixtures.py   synthetic raw files in upstream formats
```
