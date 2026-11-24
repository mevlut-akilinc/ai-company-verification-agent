import pytest
from conftest import FakeStore

from kyb import er
from kyb.agents import nodes


# ----------------------------------------------------------------- ad normalizasyonu
@pytest.mark.parametrize("ad,cekirdek,bicim", [
    ("Lloyds Bank PLC", "LLOYDS BANK", {"PLC"}),
    ("LLOYDS BANK PUBLIC LIMITED COMPANY", "LLOYDS BANK", {"PLC"}),
    ("Edp Renováveis, S.A.", "EDP RENOVAVEIS", {"SA"}),
    ("AA CORPORATION LIMITED", "AA CORP", {"LTD"}),        # CORPORATION adın parçası
    ("Smith & Sons Ltd.", "SMITH & SONS", {"LTD"}),
    ("Smith and Sons Limited", "SMITH & SONS", {"LTD"}),
    ("Müller GmbH & Co KG", "MULLER GMBH", {"KG"}),
])
def test_ayir(ad, cekirdek, bicim):
    assert er.ayir(ad) == (cekirdek, frozenset(bicim))


def test_ayni_sirket_farkli_yazim():
    assert er.benzerlik("HSBC Bank plc", "HSBC BANK PUBLIC LIMITED COMPANY") == 1.0
    assert er.benzerlik("Smith & Sons Ltd", "SMITH AND SONS LIMITED") == 1.0


@pytest.mark.parametrize("a,b", [
    ("CALIFORNIA MIDCO LIMITED", "CALIFORNIA BIDCO LIMITED"),
    ("COMPASS II LIMITED", "COMPASS I LIMITED"),
    ("ECI 12 NOMINEES LIMITED", "ECI 11 NOMINEES LIMITED"),
    ("BABCOCK SERVICES LTD", "BABCOCK SERVICES UK LIMITED"),
    ("MICRO FOCUS (US) HOLDINGS", "MICRO FOCUS HOLDINGS LTD"),
    ("EVERO ENERGY H HOLDINGS LIMITED", "EVERO ENERGY HOLDINGS LIMITED"),
])
def test_kardes_sirketler_otomatik_eslesmez(a, b):
    assert er.benzerlik(a, b) <= er.AYIRICI_TAVAN < er.KABUL_ESIGI


def test_farkli_bicim_cezasi():
    assert er.benzerlik("ACME LTD", "ACME LLP") == 1.0 - er.BICIM_CEZASI


def test_arama_sorgusu_kacis_ve_bicim():
    assert er.arama_sorgusu("Foo (UK) Bar-Baz Limited") == "FOO UK BAR BAZ"
    assert er.arama_sorgusu("Limited") is None or er.arama_sorgusu("Limited") == "LTD"


def test_decide():
    assert er.decide([]) == ("yok", None)
    assert er.decide([{"score": 0.5}])[0] == "yok"
    assert er.decide([{"score": 0.97}, {"score": 0.80}])[0] == "eslesti"
    assert er.decide([{"score": 0.97}, {"score": 0.96}])[0] == "belirsiz"  # fark yetersiz
    assert er.decide([{"score": 0.88}])[0] == "belirsiz"


# ----------------------------------------------------------------- store ile çözümleme
STORE = FakeStore(
    companies=[{"company_number": "01234567", "name": "ACME WIDGETS LIMITED"},
               {"company_number": "07654321", "name": "ACME WIDGETS UK LIMITED"},
               {"company_number": "SC012345", "name": "THISTLE TRADING LIMITED"}],
    leis=[{"lei": "LEI_ACME", "name": "ACME WIDGETS LIMITED", "country": "GB", "ra_id": "RA000585"},
          {"lei": "LEI_ACME_DE", "name": "ACME WIDGETS GMBH", "country": "DE", "ra_id": "RA000242"},
          {"lei": "LEI_EDP", "name": "EDP RENOVAVEIS SA", "country": "ES", "ra_id": "RA000533"}],
    legal_entities=[
        {"entity_key": "k1", "name": "Edp Renovaveis, S.A.", "country_registered": "Spain"},
        {"entity_key": "k2", "name": "Acme Widgets Ltd", "country_registered": "England"},
        {"entity_key": "k3", "name": "Bilinmeyen Holding A.S.", "country_registered": "Turkey"},
    ],
    upstream={"01234567": ["k1", "k2", "k3"]},
)


def test_konu_numara_ile():
    r = er.resolve_subject(STORE, "1234567", "Acme Widgets Ltd")  # baştaki 0 eksik
    assert r["karar"] == "eslesti" and r["method"] == "registry_id"
    assert r["company_number"] == "01234567" and r["uyarilar"] == []


def test_konu_numara_var_unvan_uyusmuyor():
    r = er.resolve_subject(STORE, "01234567", "Totally Different Plc")
    assert r["karar"] == "eslesti" and "UNVAN_UYUSMUYOR" in r["uyarilar"]


def test_konu_numara_sicilde_yok_ada_dusulur():
    r = er.resolve_subject(STORE, "09999999", "Thistle Trading Ltd")
    assert r["method"] == "name_search" and r["company_number"] == "SC012345"
    assert "NUMARA_SICILDE_YOK" in r["uyarilar"]


def test_konu_ad_ile_kardes_sirket_karismaz():
    r = er.resolve_subject(STORE, None, "Acme Widgets Ltd")
    assert r["karar"] == "eslesti" and r["company_number"] == "01234567"


def test_lei_yabanci_aday_cezali():
    r = er.link_lei(STORE, {"name": "ACME WIDGETS LIMITED"})
    assert r["karar"] == "eslesti" and r["lei"] == "LEI_ACME"


def test_sahip_cozumleme():
    sonuc = {r["entity_key"]: r for r in
             (er.resolve_owner(STORE, le) for le in STORE.upstream_legal_entities("01234567"))}
    assert sonuc["k1"]["match"]["label"] == "LEIEntity"            # yabancı -> GLEIF
    assert sonuc["k2"]["match"] == {**sonuc["k2"]["match"], "label": "Company",
                                    "key": "01234567"}             # UK -> sicil
    assert sonuc["k3"]["karar"] == "yok"


# ----------------------------------------------------------------- ajan düğümü
def test_agent_ok(monkeypatch):
    monkeypatch.setattr(nodes, "get_store", lambda: STORE)
    out = nodes.entity_resolution_agent({"subject": {"company_number": "01234567",
                                                     "name": "Acme Widgets Ltd"}})
    ent = out["entities"]
    assert ent["status"] == "ok" and ent["lei"]["lei"] == "LEI_ACME"
    assert ent["owners_resolved"] == 2 and len(ent["owners"]) == 3
    assert out["audit"][0]["stub"] is False and out["audit"][0]["llm_used"] is False


def test_agent_bulunamadi(monkeypatch):
    monkeypatch.setattr(nodes, "get_store", lambda: STORE)
    ent = nodes.entity_resolution_agent({"subject": {"name": "Hiç Olmayan Şirket"}})["entities"]
    assert ent["status"] == "bulunamadi" and ent["lei"] is None


def test_agent_graf_kapali_cokmez(monkeypatch):
    def patla():
        raise ConnectionError("neo4j kapalı")
    monkeypatch.setattr(nodes, "get_store", patla)
    out = nodes.entity_resolution_agent({"subject": {"company_number": "01234567"}})
    assert out["entities"]["status"] == "hata" and "ConnectionError" in out["entities"]["hata"]
