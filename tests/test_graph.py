import pytest

from kyb.agents.graph import build_graph, case_state, pending_review, resume_case, start_case
from kyb.agents.nodes import risk_scorer
from kyb.agents.state import MAX_REVISIONS

SUBJ = {"company_number": "00000001", "name": "Demo Ltd"}


def test_runs_until_human_then_approves():
    g = build_graph()
    start_case(g, "t1", SUBJ)
    payload = pending_review(g, "t1")
    assert payload is not None
    assert payload["risk"]["model"] == "rules-v0"
    assert "KYB Ön Raporu" in payload["report_markdown"]

    out = resume_case(g, "t1", {"action": "approve", "reviewer": "test"})
    assert out["review"]["action"] == "approve"
    assert pending_review(g, "t1") is None
    nodes = [a["node"] for a in out["audit"]]
    assert nodes == ["document_agent", "entity_resolution_agent", "ubo_agent", "screening_agent",
                     "risk_scorer", "report_agent", "human_review"]


def test_request_changes_loops_back_to_report():
    g = build_graph()
    start_case(g, "t2", SUBJ)
    resume_case(g, "t2", {"action": "request_changes", "note": "UBO yolunu göster"})
    payload = pending_review(g, "t2")
    assert payload is not None and "UBO yolunu göster" in payload["report_markdown"]
    st = case_state(g, "t2")
    assert st["report"]["version"] == 2 and st["revision_count"] == 1


def test_revision_limit_stops_loop():
    g = build_graph()
    start_case(g, "t3", SUBJ)
    for _ in range(MAX_REVISIONS + 1):
        resume_case(g, "t3", {"action": "request_changes", "note": "x"})
    assert pending_review(g, "t3") is None
    assert case_state(g, "t3").get("review") is None  # undecided, left for manual handling


def test_invalid_decision_rejected():
    g = build_graph()
    start_case(g, "t4", SUBJ)
    with pytest.raises(ValueError):
        resume_case(g, "t4", {"action": "maybe"})


def test_risk_scorer_is_deterministic():
    state = {
        "ubo": {"layers": 4, "has_cycle": True, "unresolved_foreign_entities": 0,
                "protected_pscs": 0, "beneficial_owners": []},
        "screening": {"threshold": 0.9, "hits": [{"score": 0.95}]},
    }
    a, b = risk_scorer(state)["risk"], risk_scorer(state)["risk"]
    assert a == b
    assert a["score"] == 20 + 30 + 25 + 50 and a["level"] == "high"
