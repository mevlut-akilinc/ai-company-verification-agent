"""İnceleme kapıları: ER belirsiz / bulunamadı, tarama alarmı, tarama atlandı, UBO kesildi."""

import pytest
from conftest import DEMO_STORE, FakeStore

from kyb.agents import nodes
from kyb.agents.graph import build_graph, case_state, pending_review, resume_case, start_case
from kyb.agents.nodes import risk_scorer
from kyb.data.tarama_indeks import IndeksYok


def dugumler(g, case_id):
    return [a["node"] for a in case_state(g, case_id)["audit"]]


# ----------------------------------------------------------------- ER bulunamadı
def test_er_bulunamadi_akisi_durdurur_numara_girilince_devam_eder():
    g = build_graph()
    start_case(g, "e1", {"company_number": "99999999", "name": ""})
    p = pending_review(g, "e1")
    assert p["stage"] == "entity" and p["durum"] == "bulunamadi"
    assert p["allowed_actions"] == ["enter_number", "reject", "select"]
    assert case_state(g, "e1").get("ubo") is None  # UBO 'atlandı' olmadı, bekliyor

    resume_case(g, "e1", {"action": "enter_number", "company_number": "00000001",
                          "reviewer": "test", "note": "sicilden bakıldı"})
    p = pending_review(g, "e1")
    assert p["stage"] == "report"
    st = case_state(g, "e1")
    assert st["entities"]["status"] == "ok"
    assert st["entities"]["subject"]["method"] == "analist_enter_number"
    assert st["ubo"]["status"] == "ok" and st["ubo"]["beneficial_owners"]
    assert st["incelemeler"]["entity"]["company_number"] == "00000001"
    assert "analist tarafından belirlendi" in p["report_markdown"]
    assert dugumler(g, "e1")[:3] == ["document_agent", "entity_resolution_agent", "entity_review"]


def test_er_gecersiz_numara_reddedilir_vaka_bekler():
    g = build_graph()
    start_case(g, "e2", {"company_number": "99999999", "name": ""})
    with pytest.raises(ValueError, match="bulunamadı"):
        resume_case(g, "e2", {"action": "enter_number", "company_number": "12345678"})
    assert pending_review(g, "e2")["stage"] == "entity"


def test_gecersiz_karardan_sonra_dogru_karar_calisir():
    """Regresyon: düğüm içinde atılan ValueError resume değerini checkpoint'te bırakıyordu;
    sonraki geçerli karar yine eski geçersiz değeri görüyor, vaka kilitleniyordu."""
    g = build_graph()
    start_case(g, "e5", {"company_number": "99999999", "name": ""})
    with pytest.raises(ValueError):
        resume_case(g, "e5", {"action": "enter_number", "company_number": "12345678"})
    resume_case(g, "e5", {"action": "enter_number", "company_number": "00000001"})
    assert pending_review(g, "e5")["stage"] == "report"
    with pytest.raises(ValueError):  # son onay aşaması da aynı
        resume_case(g, "e5", {"action": "maybe"})
    out = resume_case(g, "e5", {"action": "approve"})
    assert out["review"]["action"] == "approve"


def test_er_reject_vakayi_kapatir():
    g = build_graph()
    start_case(g, "e3", {"company_number": "99999999", "name": ""})
    out = resume_case(g, "e3", {"action": "reject", "note": "sicilde yok, müşteri bilgi vermedi"})
    assert out["review"]["stage"] == "entity" and out["review"]["action"] == "reject"
    assert pending_review(g, "e3") is None
    assert out.get("ubo") is None and out.get("risk") is None


# ----------------------------------------------------------------- ER belirsiz
IKIZ_STORE = FakeStore(companies=[{"company_number": "10000001", "name": "ACME TRADING LTD"},
                                  {"company_number": "10000002", "name": "ACME TRADING LIMITED"}])


@pytest.fixture
def ikiz(monkeypatch):
    monkeypatch.setattr(nodes, "get_store", lambda: IKIZ_STORE)


def test_er_belirsiz_analist_aday_secer(ikiz):
    g = build_graph()
    start_case(g, "e4", {"company_number": "", "name": "Acme Trading Ltd"})
    p = pending_review(g, "e4")
    assert p["stage"] == "entity" and p["durum"] == "belirsiz"
    assert {a["key"] for a in p["adaylar"]} == {"10000001", "10000002"}

    with pytest.raises(ValueError, match="aday listesinde yok"):
        resume_case(g, "e4", {"action": "select", "company_number": "00000001"})
    resume_case(g, "e4", {"action": "select", "company_number": "10000002"})
    st = case_state(g, "e4")
    assert st["entities"]["subject"]["company_number"] == "10000002"
    assert st["entities"]["onceki_durum"] == "belirsiz"
    assert pending_review(g, "e4")["stage"] == "report"


# ----------------------------------------------------------------- tarama alarmı
def _yaptirimli_store():
    # 00000001 <- %75+ Vladimir Putin (doğum 1952-10, Russian)
    return FakeStore(
        companies=[{"company_number": "00000001", "name": "DEMO LTD"}],
        edges=[{"asset": "00000001", "label": "Person",
                "owner": {"psc_key": "pp", "name": "Mr Vladimir Putin", "birth_year": 1952,
                          "birth_month": 10, "nationality": "Russian"},
                "edge": {"share_min": 75.0, "share_max": 100.0}}],
    )


@pytest.fixture
def yaptirimli(monkeypatch):
    monkeypatch.setattr(nodes, "get_store", _yaptirimli_store)


def _alarma_kadar(g, case_id):
    start_case(g, case_id, {"company_number": "00000001", "name": ""})
    p = pending_review(g, case_id)
    assert p["stage"] == "screening"
    return p


def test_tarama_alarmi_akisi_durdurur(yaptirimli):
    g = build_graph()
    p = _alarma_kadar(g, "s1")
    assert [(a["alarm_id"], a["candidate_id"], a["list"]) for a in p["alarmlar"]] == [
        ("A1", "Q7747", "yaptirim")]
    assert "doğum 1952-10 uyuşuyor" in p["alarmlar"][0]["gerekce"]
    assert case_state(g, "s1").get("risk") is None  # risk analist kararını bekliyor


def test_tarama_karari_her_alarm_icin_zorunlu(yaptirimli):
    g = build_graph()
    _alarma_kadar(g, "s2")
    with pytest.raises(ValueError, match="eksik"):
        resume_case(g, "s2", {"action": "kaydet", "kararlar": {}})
    with pytest.raises(ValueError, match="gecersiz"):
        resume_case(g, "s2", {"action": "kaydet", "kararlar": {"A1": "belki"}})
    assert pending_review(g, "s2")["stage"] == "screening"


def test_onaylanan_yaptirim_yuksek_risk(yaptirimli):
    g = build_graph()
    _alarma_kadar(g, "s3")
    resume_case(g, "s3", {"action": "kaydet", "kararlar": {"A1": "gercek"}, "reviewer": "test"})
    p = pending_review(g, "s3")
    assert p["stage"] == "report"
    assert p["risk"]["level"] == "high"
    assert "sanction_confirmed" in {r["rule"] for r in p["risk"]["fired_rules"]}
    assert "GERÇEK EŞLEŞME" in p["report_markdown"]
    assert case_state(g, "s3")["incelemeler"]["screening"]["kararlar"] == {"A1": "gercek"}


def test_yanlis_alarm_puan_almaz(yaptirimli):
    g = build_graph()
    _alarma_kadar(g, "s4")
    resume_case(g, "s4", {"action": "kaydet", "kararlar": {"A1": "yanlis"}})
    kurallar = {r["rule"] for r in pending_review(g, "s4")["risk"]["fired_rules"]}
    assert not kurallar & {"sanction_confirmed", "pep_confirmed", "screening_unreviewed"}


def test_temiz_vaka_tarama_incelemesine_ugramaz():
    g = build_graph()
    start_case(g, "s5", {"company_number": "00000001", "name": ""})
    assert pending_review(g, "s5")["stage"] == "report"
    st = case_state(g, "s5")
    assert st["screening"]["status"] == "ok" and st["screening"]["hits"] == []
    # konu şirket + Ayşe Yılmaz + yabancı tüzel kişi
    assert st["screening"]["screened"] == 3
    assert "screening_review" not in dugumler(g, "s5")


# ----------------------------------------------------------------- tarama atlandı
def test_indeks_yoksa_tarama_atlanir_acik_konu(monkeypatch):
    def yok():
        raise IndeksYok("indeks yok")

    monkeypatch.setattr(nodes, "get_tarama_indeksi", yok)
    g = build_graph()
    start_case(g, "s6", {"company_number": "00000001", "name": ""})
    p = pending_review(g, "s6")
    assert p["stage"] == "report"
    assert case_state(g, "s6")["screening"]["status"] == "atlandi"
    assert "screening_skipped" in {r["rule"] for r in p["risk"]["fired_rules"]}
    assert any("taraması yapılamadı" in k for k in p["acik_konular"])


def test_taranacak_ad_yoksa_temiz_sayilmaz(monkeypatch):
    def kapali():
        raise ConnectionError("Neo4j kapalı")

    monkeypatch.setattr(nodes, "get_store", kapali)
    g = build_graph()
    start_case(g, "s7", {"company_number": "00000001", "name": ""})
    st = case_state(g, "s7")
    assert st["entities"]["status"] == "hata"
    assert st["screening"]["status"] == "atlandi" and "taranacak ad yok" in st["screening"]["neden"]
    assert "screening_skipped" in {r["rule"] for r in st["risk"]["fired_rules"]}


# ----------------------------------------------------------------- risk kuralları
def _risk(hits=(), **ubo):
    return risk_scorer({"screening": {"status": "ok", "hits": list(hits)},
                        "ubo": {"status": "ok", "beneficial_owners": [{"sinif": "kesin"}],
                                **ubo}})["risk"]


def test_onayli_pep_orta_risk():
    r = _risk([{"list": "pep", "analist_karari": "gercek"}])
    assert r["level"] == "medium" and r["score"] == 25


def test_onayli_yaptirim_puandan_bagimsiz_yuksek():
    r = _risk([{"list": "yaptirim", "analist_karari": "gercek"}])
    assert r["level"] == "high"


def test_ubo_kesildi_kural_ve_acik_konu():
    r = _risk(truncated=True)
    assert "ubo_truncated" in {x["rule"] for x in r["fired_rules"]}
    assert any("kesildi" in k for k in nodes.acik_konular({"ubo": {"truncated": True}}))


def test_demo_store_degismedi():
    # Bu dosyadaki fixture'lar DEMO_STORE'u değiştirmez (diğer testler ona dayanıyor).
    assert DEMO_STORE.company("00000001")["name"] == "DEMO LTD"
