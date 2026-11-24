import pytest
from conftest import DEMO_STORE, FakeStore

from kyb import ubo
from kyb.agents import nodes


def kenar(asset, label, key, lo, hi, **owner):
    k = {"Person": "psc_key", "Company": "company_number", "LegalEntity": "entity_key"}[label]
    return {"asset": asset, "label": label, "owner": {k: key, "name": owner.pop("name", key), **owner},
            "edge": {} if lo is None else {"share_min": lo, "share_max": hi}}


def hesapla(edges, konu="C0", esik=25.0, cozumlenen=None):
    store = FakeStore(edges=edges)
    return ubo.hesapla(konu, ubo.zinciri_cek(store, konu, cozumlenen), esik, cozumlenen)


def by_name(r):
    return {b["name"]: b for b in r["beneficial_owners"]}


def test_kenar_payi_pay_ve_oyun_buyugu():
    assert ubo.kenar_payi({"share_min": 25, "share_max": 50, "vote_min": 50, "vote_max": 75}) == (50, 75)
    assert ubo.kenar_payi({"vote_min": 75, "vote_max": 100}) == (75, 100)
    assert ubo.kenar_payi({"appoint_directors": True}) == (None, None)


def test_dogrudan_ortak():
    r = hesapla([kenar("C0", "Person", "p1", 50, 75, name="A")])
    a = by_name(r)["A"]
    assert (a["effective_min"], a["effective_max"]) == (50, 75) and a["sinif"] == "kesin"
    assert r["layers"] == 0


def test_carpimsal_iki_katman():
    # C0 <- %50-75 C1 <- %50-75 kişi: etkin %25-56.25
    r = hesapla([kenar("C0", "Company", "C1", 50, 75), kenar("C1", "Person", "p1", 50, 75, name="A")])
    a = by_name(r)["A"]
    assert a["effective_min"] == pytest.approx(25) and a["effective_max"] == pytest.approx(56.25)
    assert a["sinif"] == "kesin"  # bantlar "more than": >50 * >50 = >25
    assert r["layers"] == 1


def test_kontrol_zinciri_carpimsal_esigin_altini_yakalar():
    # C0 <- %25-50 C1 <- %75-100 kişi: çarpımsal %18.75-50 (olası),
    # ama kişi C1'i çoğunlukla kontrol ediyor -> C1'in %25-50 payının tamamı: kesin
    r = hesapla([kenar("C0", "Company", "C1", 25, 50), kenar("C1", "Person", "p1", 75, 100, name="A")])
    a = by_name(r)["A"]
    assert a["effective_min"] == pytest.approx(18.75)
    assert (a["control_min"], a["control_max"]) == (25, 50)
    assert a["sinif"] == "kesin"


def test_azinlik_zinciri_olasi():
    # C0 <- %75-100 C1 <- %25-50 kişi: etkin %18.75-50, eşiği kapsıyor; kişi C1'de
    # çoğunlukta değil -> kontrol zinciri yok -> olası
    r = hesapla([kenar("C0", "Company", "C1", 75, 100), kenar("C1", "Person", "p1", 25, 50, name="A")])
    a = by_name(r)["A"]
    assert a["sinif"] == "olasi" and a["control_max"] == 0


def test_esik_alti_listeye_girmez():
    r = hesapla([kenar("C0", "Person", "p1", 25, 50, name="A"),
                 kenar("C0", "Company", "C1", 25, 50), kenar("C1", "Person", "p2", 25, 50, name="B")])
    assert "A" in by_name(r)
    assert "B" not in by_name(r)  # B: %6.25-25, max eşiği aşmıyor; zincir azınlık


def test_ayni_kisi_iki_yoldan_toplanir():
    # Aynı kişi (ad + doğum) iki ayrı PSC kaydıyla iki yoldan: %12.5-25 + %12.5-25
    kisi = dict(name="A", birth_year=1970, birth_month=1)
    r = hesapla([kenar("C0", "Company", "C1", 50, 75), kenar("C0", "Company", "C2", 50, 75),
                 kenar("C1", "Person", "pA1", 25, 50, **kisi),
                 kenar("C2", "Person", "pA2", 25, 50, **kisi)])
    a = by_name(r)["A"]
    assert a["path_count"] == 2 and sorted(a["psc_keys"]) == ["pA1", "pA2"]
    assert a["effective_min"] == pytest.approx(25) and a["sinif"] == "kesin"


def test_yuzdesiz_kontrol():
    r = hesapla([kenar("C0", "Person", "p1", None, None, name="A")])
    a = by_name(r)["A"]
    assert a["sinif"] == "kontrol" and a["control_only_paths"] == 1


def test_dongu_sonsuz_donmez():
    r = hesapla([kenar("C0", "Company", "C1", 50, 75), kenar("C1", "Company", "C0", 50, 75),
                 kenar("C1", "Person", "p1", 25, 50, name="A")])
    assert r["has_cycle"] is True and not r["truncated"]
    assert "A" in by_name(r)


def test_yabanci_ve_tepe_sirket():
    r = hesapla([kenar("C0", "LegalEntity", "X", 50, 75, name="Offshore SA", country_registered="Panama"),
                 kenar("C0", "Company", "TOP", 25, 50, name="LISTED PLC")])
    assert r["unresolved_foreign_entities"] == 1
    assert [t["company_number"] for t in r["top_companies"]] == ["TOP"]
    assert r["beneficial_owners"] == []


def test_er_cozumledigi_tuzel_sahip_zincirde_yurunur():
    edges = [kenar("C0", "LegalEntity", "LE1", 75, 100, name="Parent Ltd"),
             kenar("C9", "Person", "p1", 75, 100, name="A")]
    assert hesapla(edges)["unresolved_foreign_entities"] == 1
    r = hesapla(edges, cozumlenen={"LE1": "C9"})
    assert r["unresolved_foreign_entities"] == 0 and by_name(r)["A"]["sinif"] == "kesin"


def test_derinlik_siniri(monkeypatch):
    monkeypatch.setattr(ubo, "MAX_DERINLIK", 3)
    edges = [kenar(f"C{i}", "Company", f"C{i+1}", 75, 100) for i in range(6)]
    assert hesapla(edges)["truncated"] is True


# ----------------------------------------------------------------- ajan
def test_agent_demo_store():
    state = {"entities": {"status": "ok", "subject": {"company_number": "00000001"}, "owners": []}}
    out = nodes.ubo_agent(state)
    u = out["ubo"]
    assert u["status"] == "ok" and u["layers"] == 1 and u["unresolved_foreign_entities"] == 1
    a = {b["name"]: b for b in u["beneficial_owners"]}["Ayşe Yılmaz"]
    assert a["sinif"] == "kesin"  # >75 * >50 = >37.5
    assert out["audit"][0]["stub"] is False


def test_agent_konu_cozulmediyse_atlar():
    out = nodes.ubo_agent({"entities": {"status": "bulunamadi"}})
    assert out["ubo"]["status"] == "atlandi" and out["ubo"]["beneficial_owners"] == []


def test_musterek_tutma_isaretlenir():
    # Üç kişi aynı şirkette her biri %75-100: toplam alt sınır 225 > 100 -> müşterek
    edges = [kenar("C0", "Company", "C1", 75, 100)] + [
        kenar("C1", "Person", f"p{i}", 75, 100, name=n) for i, n in enumerate("ABC")]
    r = hesapla(edges)
    assert r["joint_holding_companies"] == ["C1"]
    assert all(b["joint"] and b["sinif"] == "kesin" for b in r["beneficial_owners"])


def test_normal_ortaklik_musterek_degil():
    r = hesapla([kenar("C0", "Person", "p1", 50, 75, name="A"),
                 kenar("C0", "Person", "p2", 25, 50, name="B")])
    assert r["joint_holding_companies"] == [] and not any(b["joint"] for b in r["beneficial_owners"])
