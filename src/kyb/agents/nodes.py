"""Graph nodes.

Faz 0: the agent nodes are stubs returning fixed, clearly-marked outputs with
the final output *shape*, so the whole flow (supervisor -> agents -> risk ->
report -> human approval) runs end to end. Each stub is replaced by the real
implementation in later weeks without touching the graph wiring.

The risk scorer is already real: it is plain deterministic code, because the
design rule is "the LLM never decides; code computes the numbers; a human
makes the final call".
"""

from __future__ import annotations

from datetime import datetime, timezone

from langgraph.types import interrupt

from kyb.agents.state import KYBState, audit_entry
from kyb.config import UBO_THRESHOLD_PCT

VALID_ACTIONS = {"approve", "reject", "request_changes"}


# --------------------------------------------------------------------------- agents (stubs)
def document_agent(state: KYBState) -> dict:
    subj = state.get("subject", {})
    extraction = {
        "status": "stub",
        "fields": {
            "unvan": subj.get("name"),
            "sicil_no": subj.get("company_number"),
            "ortaklar": [
                {"ad": "Örnek Holding A.Ş.", "pay_orani": 60.0, "tur": "tuzel"},
                {"ad": "Ayşe Yılmaz", "pay_orani": 40.0, "tur": "gercek"},
            ],
        },
        # Every extracted value must point back to its source (doc, page).
        "provenance": [{"field": "ortaklar", "doc": "sentetik_sicil_gazetesi.pdf", "page": 1}],
    }
    return {"extraction": extraction,
            "audit": [audit_entry("document_agent", "2 ortak çıkarıldı", llm_used=True, stub=True)]}


def entity_resolution_agent(state: KYBState) -> dict:
    entities = {
        "status": "stub",
        "matches": [
            {"mention": "Örnek Holding A.Ş.", "node": "Company:00000001", "score": 0.97, "method": "splink"},
            {"mention": "Ayşe Yılmaz", "node": None, "score": None, "method": "no_candidate"},
        ],
    }
    return {"entities": entities,
            "audit": [audit_entry("entity_resolution_agent", "1/2 eşleşti", llm_used=False, stub=True)]}


def ubo_agent(state: KYBState) -> dict:
    ubo = {
        "status": "stub",
        "threshold_pct": UBO_THRESHOLD_PCT,
        "layers": 3,
        "has_cycle": False,
        "unresolved_foreign_entities": 1,
        "protected_pscs": 0,
        "beneficial_owners": [
            # PSC gives bands, so effective ownership is an interval.
            {"name": "Ayşe Yılmaz", "effective_min": 40.0, "effective_max": 40.0, "path_count": 1},
        ],
    }
    return {"ubo": ubo,
            "audit": [audit_entry("ubo_agent", "3 katman, 1 UBO", llm_used=False, stub=True)]}


def screening_agent(state: KYBState) -> dict:
    screening = {
        "status": "stub",
        "screened": 3,
        "hits": [
            {"query": "Ayse Yilmaz", "candidate_id": "os-demo-123", "candidate_name": "Ayse Yilmazer",
             "score": 0.82, "list": "demo"},
        ],
        "threshold": 0.9,
    }
    return {"screening": screening,
            "audit": [audit_entry("screening_agent", "1 olası eşleşme (eşik altı)", llm_used=False, stub=True)]}


# --------------------------------------------------------------------------- deterministic risk score
RISK_RULES = [
    # (id, description, points, predicate)
    ("deep_chain", "3 veya daha fazla kurumsal katman",
     20, lambda s: (s.get("ubo") or {}).get("layers", 0) >= 3),
    ("circular", "Döngüsel ortaklık",
     30, lambda s: bool((s.get("ubo") or {}).get("has_cycle"))),
    ("foreign_unresolved", "Çözümlenemeyen yabancı tüzel kişi",
     15, lambda s: (s.get("ubo") or {}).get("unresolved_foreign_entities", 0) > 0),
    ("protected_psc", "Kimliği gizli (super-secure) PSC",
     20, lambda s: (s.get("ubo") or {}).get("protected_pscs", 0) > 0),
    ("no_ubo", "Eşiği aşan gerçek faydalanıcı yok",
     25, lambda s: not (s.get("ubo") or {}).get("beneficial_owners")),
    ("screening_hit", "Eşik üstü tarama eşleşmesi",
     50, lambda s: any(h["score"] >= (s.get("screening") or {}).get("threshold", 0.9)
                       for h in (s.get("screening") or {}).get("hits", []))),
    ("screening_near", "Eşiğe yakın tarama eşleşmesi (0.75+)",
     10, lambda s: any(0.75 <= h["score"] < (s.get("screening") or {}).get("threshold", 0.9)
                       for h in (s.get("screening") or {}).get("hits", []))),
]
RISK_LEVELS = [(50, "high"), (20, "medium"), (0, "low")]


def risk_scorer(state: KYBState) -> dict:
    fired = [
        {"rule": rid, "description": desc, "points": pts}
        for rid, desc, pts, pred in RISK_RULES
        if pred(state)
    ]
    score = sum(r["points"] for r in fired)
    level = next(lvl for floor, lvl in RISK_LEVELS if score >= floor)
    risk = {"score": score, "level": level, "fired_rules": fired, "model": "rules-v0"}
    return {"risk": risk,
            "audit": [audit_entry("risk_scorer", f"skor {score} ({level})", llm_used=False)]}


# --------------------------------------------------------------------------- report (stub)
def report_agent(state: KYBState) -> dict:
    subj = state.get("subject", {})
    risk = state.get("risk") or {}
    notes = [h.get("note") for h in state.get("review_history", []) if h.get("note")]
    rule_lines = [f"- {r['description']} (+{r['points']})" for r in risk.get("fired_rules", [])]
    ubo_lines = [f"- {b['name']}: %{b['effective_min']:.0f}–%{b['effective_max']:.0f}"
                 for b in (state.get("ubo") or {}).get("beneficial_owners", [])]
    lines = [
        f"# KYB Ön Raporu: {subj.get('name', '?')} ({subj.get('company_number', '?')})",
        "",
        f"**Risk:** {risk.get('level', '?')} (skor {risk.get('score', '?')})",
        "",
        "## Tetiklenen kurallar",
        *(rule_lines or ["- yok"]),
        "",
        "## Gerçek faydalanıcılar",
        *(ubo_lines or ["- tespit edilemedi"]),
    ]
    if notes:
        lines += ["", "## Analist notları (önceki tur)", *[f"- {n}" for n in notes]]
    lines += ["", "_Taslak rapor (Faz 0 stub). Nihai karar analiste aittir._"]
    report = {"status": "stub", "markdown": "\n".join(lines), "version": state.get("revision_count", 0) + 1}
    return {"report": report,
            "audit": [audit_entry("report_agent", f"rapor v{report['version']}", llm_used=True, stub=True)]}


# --------------------------------------------------------------------------- human in the loop
def human_review(state: KYBState) -> dict:
    """Pause the graph until an analyst decides. Code before interrupt() re-runs on resume,
    so it must stay side-effect free."""
    payload = {
        "case_id": state.get("case_id"),
        "subject": state.get("subject"),
        "risk": state.get("risk"),
        "report_markdown": (state.get("report") or {}).get("markdown"),
        "allowed_actions": sorted(VALID_ACTIONS),
    }
    decision = interrupt(payload)

    if not isinstance(decision, dict) or decision.get("action") not in VALID_ACTIONS:
        raise ValueError(f"Geçersiz karar: {decision!r}. action ∈ {sorted(VALID_ACTIONS)} olmalı.")
    record = {
        "action": decision["action"],
        "reviewer": decision.get("reviewer", "analyst"),
        "note": decision.get("note", ""),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_version": (state.get("report") or {}).get("version"),
    }
    update = {
        "review_history": [record],
        "audit": [audit_entry("human_review", f"{record['action']} ({record['reviewer']})",
                              llm_used=False, decision=record["action"])],
    }
    if record["action"] == "request_changes":
        # Clear report + review so the supervisor routes back to the report agent.
        update |= {"review": None, "report": None,
                   "revision_count": state.get("revision_count", 0) + 1}
    else:
        update["review"] = record
    return update
