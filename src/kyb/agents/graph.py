"""LangGraph wiring: a deterministic supervisor routes between worker nodes.

The supervisor is plain code, not an LLM: it walks PIPELINE and sends the case
to the first step whose output is still missing. Routing is therefore
reproducible and auditable. Every worker returns to the supervisor, except the
document agent: a conditional edge sends unclean extractions (grounding
failures, share-ratio warnings, extraction errors) to document_review first.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from kyb.agents import nodes
from kyb.agents.state import MAX_REVISIONS, PIPELINE, KYBState

NODE_FUNCS = {
    "document_agent": nodes.document_agent,
    "entity_resolution_agent": nodes.entity_resolution_agent,
    "ubo_agent": nodes.ubo_agent,
    "screening_agent": nodes.screening_agent,
    "risk_scorer": nodes.risk_scorer,
    "report_agent": nodes.report_agent,
    "human_review": nodes.human_review,
}


def supervisor(state: KYBState) -> dict:
    return {}


def route(state: KYBState) -> str:
    if state.get("review"):
        return END
    if state.get("revision_count", 0) > MAX_REVISIONS:
        return END  # too many revision loops: stop, case stays open for manual handling
    for node, key in PIPELINE:
        if state.get(key) is None:
            return node
    return END


def route_after_document(state: KYBState) -> str:
    """İlk koşullu kenar: grounding temiz ve uyarı yoksa akış sürer, değilse analist bakar."""
    return "document_review" if state.get("insan_incelemesi_gerekli", True) else "supervisor"


def build_graph(checkpointer=None):
    g = StateGraph(KYBState)
    g.add_node("supervisor", supervisor)
    for name, fn in NODE_FUNCS.items():
        g.add_node(name, fn)
        if name != "document_agent":
            g.add_edge(name, "supervisor")
    # Belge düğümü supervisor'a doğrudan dönmez; önce temiz mi diye bakılır.
    g.add_node("document_review", nodes.document_review)
    g.add_conditional_edges("document_agent", route_after_document,
                            ["supervisor", "document_review"])
    g.add_edge("document_review", "supervisor")  # reject -> review dolu -> route() END der
    g.add_edge(START, "supervisor")
    g.add_conditional_edges("supervisor", route, [*NODE_FUNCS, END])
    return g.compile(checkpointer=checkpointer or InMemorySaver())


# --------------------------------------------------------------------------- helpers for UI / CLI
def case_config(case_id: str) -> dict:
    return {"configurable": {"thread_id": case_id}}


def start_case(graph, case_id: str, subject: dict, documents: list[dict] | None = None) -> dict:
    init: KYBState = {"case_id": case_id, "subject": subject, "documents": documents or [],
                      "revision_count": 0}
    return graph.invoke(init, case_config(case_id))


def pending_review(graph, case_id: str) -> Any | None:
    """Payload of the open interrupt, or None if the case is not waiting for a human."""
    snap = graph.get_state(case_config(case_id))
    interrupts = getattr(snap, "interrupts", None) or [
        i for t in snap.tasks for i in getattr(t, "interrupts", ())
    ]
    return interrupts[0].value if interrupts else None


def resume_case(graph, case_id: str, decision: dict) -> dict:
    return graph.invoke(Command(resume=decision), case_config(case_id))


def case_state(graph, case_id: str) -> dict:
    return graph.get_state(case_config(case_id)).values
