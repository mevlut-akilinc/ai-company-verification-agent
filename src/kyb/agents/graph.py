"""LangGraph wiring: a deterministic supervisor routes between worker nodes.

The supervisor is plain code, not an LLM: it walks PIPELINE and sends the case
to the first step whose output is still missing. Routing is therefore
reproducible and auditable. Every worker returns to the supervisor, except the
gated ones (kyb.agents.inceleme): document, entity resolution and screening. A
conditional edge sends their result to an analyst review node first when the
analyst's decision changes what is computed next (unclean extraction, ambiguous
or missing registry match, screening alert).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from kyb.agents import inceleme, nodes
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


def build_graph(checkpointer=None):
    g = StateGraph(KYBState)
    g.add_node("supervisor", supervisor)
    kapili = {a.ajan for a in inceleme.ASAMALAR}
    for name, fn in NODE_FUNCS.items():
        g.add_node(name, fn)
        if name not in kapili:
            g.add_edge(name, "supervisor")
    # Kapılı ajanlar supervisor'a doğrudan dönmez: sonuç analist kararı gerektiriyorsa
    # önce ilgili inceleme düğümüne gider. reject -> review dolu -> route() END der.
    for asama in inceleme.ASAMALAR:
        g.add_node(asama.dugum, inceleme.inceleme_kapisi(asama))
        g.add_conditional_edges(asama.ajan, inceleme.yonlendir(asama),
                                ["supervisor", asama.dugum])
        g.add_edge(asama.dugum, "supervisor")
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
    """Kararı önce doğrular, sonra grafı sürdürür. Geçersiz karar ValueError verir ve
    grafa hiç gitmez: düğüm içinde atılan hata resume değerini checkpoint'te bırakır,
    sonraki geçerli kararda interrupt() yine eski değeri döndürür ve vaka kilitlenir."""
    payload = pending_review(graph, case_id)
    if payload is None:
        raise ValueError(f"{case_id} karar beklemiyor.")
    inceleme.dogrula(payload["stage"], case_state(graph, case_id), decision)
    return graph.invoke(Command(resume=decision), case_config(case_id))


def case_state(graph, case_id: str) -> dict:
    return graph.get_state(case_config(case_id)).values
