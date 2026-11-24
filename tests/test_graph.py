from pathlib import Path

import pytest

from kyb.agents import nodes
from kyb.agents.graph import build_graph, case_state, pending_review, resume_case, start_case
from kyb.agents.nodes import risk_scorer
from kyb.agents.state import MAX_REVISIONS
from kyb.belge import belge_metni
from kyb.extraction import Ortak, SirketBilgisi
from kyb.extractor import ExtractionError

SUBJ = {"company_number": "00000001", "name": "Demo Ltd"}
BELGELER = Path(__file__).parent / "fixtures" / "belgeler"

# ----------------------------------------------------------------- sahte LLM çıktıları
TEMIZ_METIN = "DEMO LOJİSTİK LİMİTED ŞİRKETİ, sicil no 38217. Müdür adres değişikliği."
TEMIZ = SirketBilgisi(unvan="DEMO LOJİSTİK LİMİTED ŞİRKETİ", sicil_no="38217",
                      sermaye_tutari=None, ortaklar=[])

# sicil_no belgede yok -> grounding 'kaldi'
UYDURMA = TEMIZ.model_copy(update={"sicil_no": "99999"})

ORAN_METNI = ("KUMSAL LİMİTED ŞİRKETİ, sicil no 73508. Sermaye 1.000.000 TL. "
              "Hakan Demirtaş 400.000 TL pay. Elif Sarı 300.000 TL pay.")
ORAN_HATALI = SirketBilgisi(
    unvan="KUMSAL LİMİTED ŞİRKETİ", sicil_no="73508", sermaye_tutari=1_000_000,
    ortaklar=[
        Ortak(ad="Hakan Demirtaş", tur="gercek", pay_tutari=400_000, pay_orani=None,
              kanit="Hakan Demirtaş 400.000 TL pay."),
        Ortak(ad="Elif Sarı", tur="gercek", pay_tutari=300_000, pay_orani=None,
              kanit="Elif Sarı 300.000 TL pay."),
    ],
)

CEVAPLAR = {TEMIZ_METIN: TEMIZ, ORAN_METNI: ORAN_HATALI, "UYDURMA": UYDURMA}


def sahte_extract(metin: str, **_):
    if metin.startswith("KESIK"):
        raise ExtractionError("Çıktı num_predict=1024 sınırında kesildi.", raw='{"unvan": "x',
                              meta={"done_reason": "length", "raw": '{"unvan": "x'})
    if metin.startswith("UYDURMA"):
        return UYDURMA, {"raw": "{}"}
    return CEVAPLAR[metin], {"raw": "{}", "output_tokens": 10}


@pytest.fixture(autouse=True)
def llm_yok(monkeypatch):
    monkeypatch.setattr(nodes, "extract_company", sahte_extract)


def temiz_vaka(g, case_id):
    return start_case(g, case_id, SUBJ, [{"text": TEMIZ_METIN, "name": "temiz.txt"}])


# ----------------------------------------------------------------- mevcut akış (temiz belge)
def test_runs_until_human_then_approves():
    g = build_graph()
    temiz_vaka(g, "t1")
    payload = pending_review(g, "t1")
    assert payload is not None and payload["stage"] == "report"
    assert payload["risk"]["model"] == "rules-v0"
    assert "KYB Ön Raporu" in payload["report_markdown"]
    assert "DEMO LOJİSTİK" in payload["report_markdown"]

    out = resume_case(g, "t1", {"action": "approve", "reviewer": "test"})
    assert out["review"]["action"] == "approve"
    assert pending_review(g, "t1") is None
    nodes_ = [a["node"] for a in out["audit"]]
    assert nodes_ == ["document_agent", "entity_resolution_agent", "ubo_agent", "screening_agent",
                      "risk_scorer", "report_agent", "human_review"]


def test_request_changes_loops_back_to_report():
    g = build_graph()
    temiz_vaka(g, "t2")
    resume_case(g, "t2", {"action": "request_changes", "note": "UBO yolunu göster"})
    payload = pending_review(g, "t2")
    assert payload is not None and "UBO yolunu göster" in payload["report_markdown"]
    st = case_state(g, "t2")
    assert st["report"]["version"] == 2 and st["revision_count"] == 1


def test_revision_limit_stops_loop():
    g = build_graph()
    temiz_vaka(g, "t3")
    for _ in range(MAX_REVISIONS + 1):
        resume_case(g, "t3", {"action": "request_changes", "note": "x"})
    assert pending_review(g, "t3") is None
    assert case_state(g, "t3").get("review") is None  # undecided, left for manual handling


def test_invalid_decision_rejected():
    g = build_graph()
    temiz_vaka(g, "t4")
    with pytest.raises(ValueError):
        resume_case(g, "t4", {"action": "maybe"})


# ----------------------------------------------------------------- document agent
def test_clean_document_skips_document_review():
    g = build_graph()
    temiz_vaka(g, "d1")
    st = case_state(g, "d1")
    assert st["insan_incelemesi_gerekli"] is False
    assert st["inceleme_nedenleri"] == []
    assert st["extraction"]["status"] == "ok"
    assert st["grounding"]["dogrulandi"] is True
    assert st["extraction_error"] is None
    doc_audit = st["audit"][0]
    assert doc_audit["node"] == "document_agent"
    assert doc_audit["llm_used"] is True and doc_audit["stub"] is False


def test_grounding_failure_goes_to_document_review_then_continues():
    g = build_graph()
    start_case(g, "d2", SUBJ, [{"text": "UYDURMA " + TEMIZ_METIN}])
    payload = pending_review(g, "d2")
    assert payload["stage"] == "document"
    assert "GROUNDING_KALDI" in payload["nedenler"]
    assert [k["alan"] for k in payload["sorunlu_kontroller"]] == ["sicil_no"]
    assert payload["allowed_actions"] == ["approve", "reject"]
    assert case_state(g, "d2").get("entities") is None  # sonraki ajana geçmedi

    resume_case(g, "d2", {"action": "approve", "note": "sicil manuel kontrol edildi"})
    payload = pending_review(g, "d2")
    assert payload["stage"] == "report"
    assert "Belge aşaması analist kararı: approve" in payload["report_markdown"]
    nodes_ = [a["node"] for a in case_state(g, "d2")["audit"]]
    assert nodes_[:3] == ["document_agent", "document_review", "entity_resolution_agent"]


def test_ratio_warning_goes_to_document_review_and_reject_ends_case():
    g = build_graph()
    start_case(g, "d3", SUBJ, [{"text": ORAN_METNI}])
    payload = pending_review(g, "d3")
    assert payload["stage"] == "document"
    assert "GROUNDING_KALDI" not in payload["nedenler"]  # grounding temiz, sorun oranlarda
    assert "SERMAYE_TOPLAMI_TUTMUYOR" in payload["nedenler"]
    assert "ORAN_TOPLAMI_TUTMUYOR" in payload["nedenler"]

    out = resume_case(g, "d3", {"action": "reject", "note": "toplam tutmuyor"})
    assert out["review"]["stage"] == "document" and out["review"]["action"] == "reject"
    assert pending_review(g, "d3") is None
    assert out.get("risk") is None  # akış belgede kapandı


def test_extraction_error_does_not_crash():
    g = build_graph()
    start_case(g, "d4", SUBJ, [{"text": "KESIK belge"}])
    st = case_state(g, "d4")
    assert st["extraction"]["status"] == "hata"
    assert st["extraction_error"]["kod"] == "CIKARIM_HATASI"
    assert st["extraction_error"]["ham_cikti"] == '{"unvan": "x'
    payload = pending_review(g, "d4")
    assert payload["stage"] == "document" and payload["hata"]["kod"] == "CIKARIM_HATASI"


def test_no_document_is_skipped_and_flow_continues():
    g = build_graph()
    start_case(g, "d5", SUBJ)
    st = case_state(g, "d5")
    assert st["extraction"]["status"] == "atlandi"
    assert st["insan_incelemesi_gerekli"] is False and st["inceleme_nedenleri"] == []
    assert st["audit"][0]["llm_used"] is False
    payload = pending_review(g, "d5")
    assert payload["stage"] == "report"  # belge onayına uğramadan rapora geldi
    assert "Belge verilmedi" in payload["report_markdown"]
    nodes_ = [a["node"] for a in st["audit"]]
    assert "document_review" not in nodes_


def test_document_stage_rejects_request_changes():
    g = build_graph()
    start_case(g, "d7", SUBJ, [{"text": "KESIK belge"}])
    with pytest.raises(ValueError):  # belge aşamasında request_changes yok
        resume_case(g, "d7", {"action": "request_changes"})


def test_unreadable_document_does_not_crash():
    g = build_graph()
    start_case(g, "d6", SUBJ, [{"path": str(BELGELER / "yok.pdf")}])
    assert pending_review(g, "d6")["nedenler"] == ["BELGE_OKUNAMADI"]


def test_pdf_text_is_read():
    metin = belge_metni({"path": str(BELGELER / "belge_02_ortak_yok_adres_degisikligi.pdf")})
    assert "38217" in metin


# ----------------------------------------------------------------- risk
def test_risk_scorer_is_deterministic():
    state = {
        "ubo": {"status": "ok", "layers": 4, "has_cycle": True, "unresolved_foreign_entities": 0,
                "protected_pscs": 0, "beneficial_owners": []},
        "screening": {"threshold": 0.9, "hits": [{"score": 0.95}]},
    }
    a, b = risk_scorer(state)["risk"], risk_scorer(state)["risk"]
    assert a == b
    assert a["score"] == 20 + 30 + 25 + 50 and a["level"] == "high"


def test_no_ubo_rule_needs_completed_ubo():
    """Konu şirket çözülemediyse 'faydalanıcı yok' denemez (eksik veri != risk sinyali)."""
    fired = {r["rule"] for r in risk_scorer({"ubo": {"status": "atlandi",
                                                      "beneficial_owners": []}})["risk"]["fired_rules"]}
    assert "no_ubo" not in fired
