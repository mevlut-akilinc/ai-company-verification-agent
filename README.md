# Agentic KYB Analyst

Multi-agent Know-Your-Business pipeline: document extraction, entity resolution,
beneficial-ownership (UBO) graph analytics and sanctions screening, with a human
analyst making the final decision.

**Design rule:** the LLM never decides. Code computes every number (ownership
percentages, risk score), uncertain results go to a human, and every step is
written to an append-only audit trail.

> **Status:** document, entity-resolution and UBO agents run on real data
> (UK Companies House + GLEIF in Neo4j, local LLM via Ollama). The **screening
> agent is still a stub** (fixed demo output, marked `[stub]` in the audit
> trail). See [Results](#results) and [Limitations](#known-limitations).

## Pipeline

```
                ┌──────────── supervisor (deterministic router, plain code) ────────────┐
START ─► document ──┬──────────► entity resolution ─► UBO graph ─► screening ─► risk score ─► report ─► human review ─► END
                    │                  ▲                                                       ▲            │
                    └─► document ──────┘ approve                                               └─ request_changes (max 2)
                        review ─► reject ─► END
```

- **Supervisor** is code, not an LLM: it sends the case to the first step whose
  output is missing, so routing is reproducible and auditable.
- **Conditional edge:** after the document agent, a clean extraction continues;
  grounding failures, share-ratio warnings or extraction errors pause the graph
  (`interrupt()`) for an analyst (`approve` / `reject`).
- **Human review** at the end: `approve`, `reject` or `request_changes`
  (loops back to the report, at most twice).
- A case starts from a **UK company number** (or a name). A document is optional.

| Agent             | What it does                                                                                     | LLM | Status                    |
| ----------------- | ------------------------------------------------------------------------------------------------ | --- | ------------------------- |
| document          | PDF/text → schema-constrained extraction → grounding → share-ratio checks                        | yes | real                      |
| entity resolution | subject company by number or name; company → LEI; unresolved corporate owners → company / LEI    | no  | real (`rules-v1`)         |
| UBO               | walks active PSC edges upward, effective ownership intervals, control chains, flags              | no  | real (`psc-bands-v1`)     |
| screening         | sanctions / PEP name screening                                                                   | –   | **stub**                  |
| risk scorer       | deterministic rules with points                                                                  | no  | real (`rules-v0`)         |
| report            | Markdown report from state                                                                       | no  | template (LLM later)      |

## Results

### Document extraction (`scripts/olcum.py`)

Local model `qwen3:4b-instruct` (Ollama, RTX 3050 4 GB, seed 42), 6 synthetic
Turkish registry gazette notices with hand-written gold files.

| Metric                                        | Value                    |
| --------------------------------------------- | ------------------------ |
| Field accuracy                                | 62 / 72 (86%)            |
| Company-level warning match                   | 6 / 6                    |
| Schema errors                                 | 0                        |
| Real errors caught by grounding (recall)      | 10 / 10                  |
| Time per document                             | 4–30 s                   |

Extraction is guarded in layers: grammar-constrained generation (JSON schema),
schema rules (patterns, bounds, required keys), `done_reason` check for truncated
output, Pydantic validation, and a deterministic **grounding** step that ties every
claimed value back to the source text (`gecti` / `kismi` / `kaldi` / `atlandi`).

### Entity resolution: Company → LEI (`scripts/er_olcum.py`)

10,000 subset companies against 2,805 gold pairs (GLEIF registration-authority
IDs, never loaded into the graph).

| Version                       | Precision | Recall | F1    |
| ----------------------------- | --------- | ------ | ----- |
| first rules                   | 0.951     | 0.899  | 0.924 |
| + sibling-company rule, fixes | **0.998** | 0.899  | 0.946 |

Error analysis showed most false positives were **sibling companies** that differ
by one token (`X BIDCO` / `X MIDCO`, `FUND II` / `FUND VI`, `X LTD` / `X UK LTD`).
Such pairs are no longer auto-matched; they become `belirsiz` (ambiguous) for the
analyst. Most remaining misses are renamed companies whose LEI record carries a
different name; names alone cannot recover them.

### UBO

On 400 chained companies: 7.7 s in total, no truncation. For most of them no
natural person crosses the threshold because the chain ends at a listed PLC, at
a company declaring "no registrable person", or at an unresolved foreign entity.

## Data (public sources only)

| Source                                | Used for                                             | Licence note                          |
| ------------------------------------- | ---------------------------------------------------- | ------------------------------------- |
| UK Companies House PSC snapshot       | ownership edges, UBO                                 | Open Government Licence               |
| UK Companies House basic company data | company details                                      | Open Government Licence               |
| GLEIF Golden Copy (Level 1 + Level 2) | LEI records, parent relationships, **ER gold pairs** | CC0                                   |
| OpenSanctions `targets.simple.csv`    | screening                                            | CC BY-NC 4.0: non-commercial use only |
| Synthetic Turkish registry documents  | document extraction                                  | generated, no real people             |

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

Current subset: 10,000 companies, 8,365 person PSCs, 1,682 unresolved corporate
PSCs, 16,876 control edges, 5,918 LEI records; deepest chain 19 layers.

### Evaluation leakage guard

Company↔LEI links derived from GLEIF's registration-authority ID are written
only to `data/eval/er_gold_ch_gleif.jsonl` and never loaded into the graph. The
entity-resolution agent must find them from names (plus country and registration
authority). All three Companies House registers count as UK (England & Wales
`RA000585`, Northern Ireland `RA000586`, Scotland `RA000587`).

## How the UBO agent computes ownership

- **Edge interest** = the larger of the share and vote bands. PSC data gives
  bands (">25% to 50%"), so every value is an interval `[min, max]`.
- **Effective ownership:** bands are multiplied along each path; paths to the
  same person are added (capped at 100%).
- **Control chain:** if every intermediate edge is a majority (>50%), the person
  controls that link's whole stake. This catches control that multiplication
  hides (e.g. 75% of a company holding 30%).
- **Classes:** `kesin` (definite), `olasi` (band straddles the threshold),
  `kontrol` (control rights without a percentage).
- **Flags:** circular ownership, unresolved foreign entities, protected PSCs,
  **joint holding** (incoming stakes sum to >100%: percentages must not be
  added), and the companies at the top of the chain with their PSC statements.
- Threshold is configurable (`KYB_UBO_THRESHOLD_PCT`, default 25, as in UK PSC
  and Turkish MASAK rules).

## Known limitations

- **Screening is a stub**; the "near screening match (+10)" in every report is demo data.
- **Band uncertainty:** multiplying PSC bands over many layers gives wide
  intervals (e.g. 3%–50% after 8 layers). Exact stakes need the share register.
- **Listed parents:** chains often end at listed PLCs that are exempt from PSC
  filing; the `no_ubo` rule (+25) cannot tell them apart without listing data.
- **No protected (super-secure) or nominee PSCs in the current subset**, so those
  rules are covered by unit tests only.
- PSC records are not deduplicated; the same person is merged by name + birth month/year.
- Cases live in memory (`InMemorySaver`); they are lost when the app stops.
- Document agent issues are tracked in [`docs/bilinen_sorunlar.md`](docs/bilinen_sorunlar.md).

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Windows (Git Bash): source .venv/Scripts/activate
pip install -e ".[dev]"
cp .env.example .env

# 0) try everything offline on synthetic fixtures (seconds; LLM and Neo4j are faked in tests)
python scripts/make_fixtures.py data/raw
kyb subset --target 15
pytest -q

# 1) real data (~3.5 GB download; remove the fixture files in data/raw first)
kyb download                       # or: --sources ch_psc ch_basic --psc-parts 1-5
kyb subset --target 10000          # two passes over the PSC file, ~15 min

# 2) graph
docker compose up -d               # Neo4j browser: http://localhost:7474
kyb load --wipe                    # prints node counts, deepest chains, cycles

# 3) local LLM (only needed for documents)
ollama pull qwen3:4b-instruct

# 4) run a case with human approval
kyb case --company-number 00066076                 # terminal
kyb case --company-number 00002065 --belge tests/fixtures/belgeler/belge_02_ortak_yok_adres_degisikligi.pdf
kyb ui                                             # Streamlit approval screen

# 5) measurements
python scripts/olcum.py            # document extraction vs gold   -> runs/<timestamp>/
python scripts/er_olcum.py         # entity resolution vs gold     -> runs/er_<timestamp>/
```

Example cases: `00066076` (joint holding, 3 definite UBOs), `00238937` (circular
ownership), `00042342` (6 layers ending in Jersey), `00002065` (chain ends at a
listed PLC), `17244867` (8 layers, band uncertainty).

## Layout

```
src/kyb/
  config.py            paths, Neo4j settings, UBO threshold, GLEIF registry codes
  normalize.py         company numbers, PSC natures-of-control -> share/vote intervals
  models.py            FtM-aligned Pydantic models
  belge.py             document text (PDF via pypdf, or plain text)
  extraction.py        extraction schema (SirketBilgisi / Ortak)
  extractor.py         Ollama call with schema-constrained output
  grounding.py         ties every extracted value back to the source text
  oranlar.py           share-ratio analysis and warnings
  er.py                name normalisation, scoring, decisions, subject / LEI / owner resolution
  ubo.py               ownership-chain fetch (BFS) and effective-ownership computation
  data/download.py     date-aware bulk downloader with resume
  data/subset.py       chain-centred subset + ER gold pairs + sanctions processing
  graph/load_neo4j.py  idempotent MERGE loader + sanity queries
  graph/store.py       read-only graph access used by the agents
  agents/              LangGraph state, nodes, supervisor, conditional edges, interrupt() reviews
  ui/app.py            analyst approval screen (document and report stages)
scripts/
  make_fixtures.py     synthetic raw files in upstream formats
  olcum.py             document extraction measurement
  er_olcum.py          entity-resolution measurement
docs/
  bilinen_sorunlar.md  document agent: v0 measurement and known issues
```

## Tech stack

LangGraph · Ollama (Qwen3-4B-Instruct, local) · Pydantic v2 · Neo4j / Cypher ·
Streamlit · Docker Compose · pytest · pypdf
