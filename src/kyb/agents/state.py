"""Shared state passed between LangGraph nodes."""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, TypedDict

ReviewAction = Literal["approve", "reject", "request_changes"]


class KYBState(TypedDict, total=False):
    case_id: str
    subject: dict[str, Any]          # {"company_number": ..., "name": ...}
    documents: list[dict[str, Any]]  # input documents (paths / metadata)

    extraction: dict[str, Any] | None   # Document agent (hata olsa da dolu: status="hata")
    grounding: dict[str, Any] | None    # Document agent: GroundingRaporu + sayaçlar
    oran_analizi: dict[str, Any] | None  # Document agent: OranAnalizi
    extraction_error: dict[str, Any] | None  # Document agent: {"kod", "mesaj", "ham_cikti", ...}
    insan_incelemesi_gerekli: bool      # Document agent: belge aşamasında analist bakmalı mı
    inceleme_nedenleri: list[str]       # ör. ["GROUNDING_KALDI", "SERMAYE_TOPLAMI_TUTMUYOR"]
    document_review: dict[str, Any] | None  # Belge aşamasındaki analist kararı
    entities: dict[str, Any] | None     # Entity resolution agent
    ubo: dict[str, Any] | None          # Graph / UBO agent
    screening: dict[str, Any] | None    # Screening agent
    risk: dict[str, Any] | None         # Risk scorer (deterministic code)
    report: dict[str, Any] | None       # Report agent
    review: dict[str, Any] | None       # Human decision

    revision_count: int
    review_history: Annotated[list[dict[str, Any]], operator.add]
    audit: Annotated[list[dict[str, Any]], operator.add]  # append-only trail


# Order the supervisor walks through, and which state key marks each step as done.
PIPELINE: list[tuple[str, str]] = [
    ("document_agent", "extraction"),
    ("entity_resolution_agent", "entities"),
    ("ubo_agent", "ubo"),
    ("screening_agent", "screening"),
    ("risk_scorer", "risk"),
    ("report_agent", "report"),
    ("human_review", "review"),
]

MAX_REVISIONS = 2


def audit_entry(node: str, summary: str, *, llm_used: bool, stub: bool = False, **extra) -> dict:
    return {
        "node": node,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": summary,
        "llm_used": llm_used,
        "stub": stub,
        **extra,
    }
