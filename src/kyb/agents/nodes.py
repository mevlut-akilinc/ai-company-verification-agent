"""Graph nodes.

The document agent is real (LLM extraction + deterministic grounding and share
ratio checks). The other agent nodes are still stubs returning fixed,
clearly-marked outputs with the final output *shape*, so the whole flow runs
end to end. Each stub is replaced by the real implementation later.

The risk scorer is already real: it is plain deterministic code, because the
design rule is "the LLM never decides; code computes the numbers; a human
makes the final call".
"""

from __future__ import annotations

from datetime import datetime, timezone

from langgraph.types import interrupt

from kyb.agents.state import KYBState, audit_entry
from kyb.belge import belge_adi, belge_metni
from kyb.config import UBO_THRESHOLD_PCT
from kyb.extractor import MODEL, ExtractionError, extract_company
from kyb.grounding import ground
from kyb.oranlar import hesapla_oranlar

VALID_ACTIONS = {"approve", "reject", "request_changes"}
DOCUMENT_ACTIONS = {"approve", "reject"}


# --------------------------------------------------------------------------- document agent
def _belge_hatasi(kod: str, mesaj: str, *, belge: str | None, llm_used: bool, **detay) -> dict:
    """Hata yolu: akış durmaz. extraction None kalmamalı, yoksa supervisor bu düğüme
    geri döner ve sonsuz döngü olur; status='hata' ile 'çalıştı ama sonuç yok' denir."""
    hata = {"kod": kod, "mesaj": mesaj, "belge": belge, **detay}
    return {
        "extraction": {"status": "hata", "belge": belge, "fields": None},
        "grounding": None,
        "oran_analizi": None,
        "extraction_error": hata,
        "insan_incelemesi_gerekli": True,
        "inceleme_nedenleri": [kod],
        "audit": [audit_entry("document_agent", f"{kod}: {mesaj[:120]}", llm_used=llm_used)],
    }


def document_agent(state: KYBState) -> dict:
    """belge metni -> extract_company (LLM) -> ground -> hesapla_oranlar.

    LLM sadece çıkarım yapar; grounding ve oranlar deterministik koddur. Düğüm hiçbir
    değeri düzeltmez; şüpheli her şeyi işaretleyip vakayı analiste yönlendirir.
    """
    belgeler = state.get("documents") or []
    if not belgeler:
        return _belge_hatasi("BELGE_YOK", "Vakaya belge eklenmedi.", belge=None, llm_used=False)

    nedenler: list[str] = []
    if len(belgeler) > 1:
        # Çoklu belge birleştirme henüz yok; sessizce atlamak yerine analiste söyle.
        nedenler.append("BIRDEN_FAZLA_BELGE")
    belge = belgeler[0]
    ad = belge_adi(belge)

    try:
        metin = belge_metni(belge)
    except Exception as e:  # noqa: BLE001 - bozuk/eksik dosya vakayı çökertmesin
        return _belge_hatasi("BELGE_OKUNAMADI", f"{type(e).__name__}: {e}", belge=ad, llm_used=False)
    if not metin.strip():
        return _belge_hatasi("BELGE_BOS", "Belgeden metin çıkmadı (taranmış PDF olabilir).",
                             belge=ad, llm_used=False)

    try:
        sonuc, meta = extract_company(metin)
    except ExtractionError as e:
        return _belge_hatasi("CIKARIM_HATASI", str(e), belge=ad, llm_used=True, ham_cikti=e.raw,
                             meta={k: v for k, v in e.meta.items() if k != "raw"})
    except Exception as e:  # noqa: BLE001 - ör. Ollama kapalı
        return _belge_hatasi("LLM_CAGRISI_BASARISIZ", f"{type(e).__name__}: {e}", belge=ad,
                             llm_used=True)

    rapor = ground(sonuc, metin)
    analiz = hesapla_oranlar(sonuc)

    if rapor.kalan_sayisi:
        nedenler.append("GROUNDING_KALDI")
    if rapor.kismi_sayisi:
        nedenler.append("GROUNDING_KISMI")
    # Şirket ve ortak düzeyindeki oran uyarıları; tekrarsız, ilk görülme sırasıyla.
    for kod in [*analiz.uyarilar, *(u for o in analiz.ortaklar for u in o.uyarilar)]:
        if kod not in nedenler:
            nedenler.append(kod)

    gerekli = bool(nedenler)
    ozet = (f"{len(sonuc.ortaklar)} ortak; grounding kaldı {rapor.kalan_sayisi} "
            f"kısmi {rapor.kismi_sayisi}; uyarı {len(nedenler)}"
            + (" -> insan incelemesi" if gerekli else ""))
    return {
        "extraction": {
            "status": "ok",
            "belge": ad,
            "fields": sonuc.model_dump(),
            "meta": {k: v for k, v in meta.items() if k != "raw"},
        },
        "grounding": {"dogrulandi": rapor.dogrulandi, "kalan_sayisi": rapor.kalan_sayisi,
                      "kismi_sayisi": rapor.kismi_sayisi, **rapor.model_dump()},
        "oran_analizi": analiz.model_dump(),
        "extraction_error": None,
        "insan_incelemesi_gerekli": gerekli,
        "inceleme_nedenleri": nedenler,
        "audit": [audit_entry("document_agent", ozet, llm_used=True, stub=False,
                              model=MODEL, nedenler=nedenler)],
    }


def document_review(state: KYBState) -> dict:
    """Belge aşamasında analist onayı. approve -> akış devam eder, reject -> vaka kapanır.

    interrupt() öncesi kod resume'da yeniden çalışır; yan etkisiz kalmalı.
    """
    grounding = state.get("grounding") or {}
    payload = {
        "stage": "document",
        "case_id": state.get("case_id"),
        "subject": state.get("subject"),
        "nedenler": state.get("inceleme_nedenleri", []),
        "hata": state.get("extraction_error"),
        "extraction": (state.get("extraction") or {}).get("fields"),
        # Analist sadece sorunlu kontrolleri görsün.
        "sorunlu_kontroller": [k for k in grounding.get("kontroller", [])
                               if k["durum"] in ("kaldi", "kismi")],
        "oran_analizi": state.get("oran_analizi"),
        "allowed_actions": sorted(DOCUMENT_ACTIONS),
    }
    decision = interrupt(payload)

    if not isinstance(decision, dict) or decision.get("action") not in DOCUMENT_ACTIONS:
        raise ValueError(f"Geçersiz karar: {decision!r}. action ∈ {sorted(DOCUMENT_ACTIONS)} olmalı.")
    record = {
        "stage": "document",
        "action": decision["action"],
        "reviewer": decision.get("reviewer", "analyst"),
        "note": decision.get("note", ""),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nedenler": state.get("inceleme_nedenleri", []),
    }
    update = {
        "document_review": record,
        "review_history": [record],
        "audit": [audit_entry("document_review", f"belge {record['action']} ({record['reviewer']})",
                              llm_used=False, decision=record["action"])],
    }
    if record["action"] == "reject":
        update["review"] = record  # supervisor -> END
    return update


# --------------------------------------------------------------------------- agents (stubs)


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
    extraction = state.get("extraction") or {}
    fields = extraction.get("fields") or {}
    doc_review = state.get("document_review")
    belge_lines = [
        f"- Kaynak: {extraction.get('belge') or '-'} (durum: {extraction.get('status', '-')})",
        f"- Unvan: {fields.get('unvan') or '-'} · Sicil no: {fields.get('sicil_no') or '-'}",
        f"- Ortak sayısı: {len(fields.get('ortaklar') or [])}",
        f"- İnceleme nedenleri: {', '.join(state.get('inceleme_nedenleri') or []) or 'yok'}",
    ]
    if doc_review:
        belge_lines.append(f"- Belge aşaması analist kararı: {doc_review['action']} "
                           f"({doc_review['reviewer']})")
    lines = [
        f"# KYB Ön Raporu: {subj.get('name', '?')} ({subj.get('company_number', '?')})",
        "",
        f"**Risk:** {risk.get('level', '?')} (skor {risk.get('score', '?')})",
        "",
        "## Belge çıkarımı",
        *belge_lines,
        "",
        "## Tetiklenen kurallar",
        *(rule_lines or ["- yok"]),
        "",
        "## Gerçek faydalanıcılar",
        *(ubo_lines or ["- tespit edilemedi"]),
    ]
    if notes:
        lines += ["", "## Analist notları (önceki tur)", *[f"- {n}" for n in notes]]
    lines += ["", "_Taslak rapor (belge dışı bölümler stub). Nihai karar analiste aittir._"]
    report = {"status": "stub", "markdown": "\n".join(lines), "version": state.get("revision_count", 0) + 1}
    return {"report": report,
            "audit": [audit_entry("report_agent", f"rapor v{report['version']}", llm_used=True, stub=True)]}


# --------------------------------------------------------------------------- human in the loop
def human_review(state: KYBState) -> dict:
    """Pause the graph until an analyst decides. Code before interrupt() re-runs on resume,
    so it must stay side-effect free."""
    payload = {
        "stage": "report",
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
        "stage": "report",
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
