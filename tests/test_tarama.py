import pytest

from kyb import tarama
from kyb.data.tarama_indeks import IndeksYok, TaramaIndeksi, liste_turu
from kyb.tarama import Sorgu, dogum_karsilastir, fonetik, isim_skoru, kisi_tokens, uyruk_kodu


# ----------------------------------------------------------------- normalizasyon
def test_kisi_tokens_unvan_ve_bas_harfler():
    assert kisi_tokens("Mr Vladimir Vladimirovich PUTIN") == ["VLADIMIR", "VLADIMIROVICH", "PUTIN"]
    # er.tokens tek harfleri birleştirirdi (JRR); kişi adında baş harfler ayrı kalır.
    assert kisi_tokens("J. R. R. Tolkien") == ["J", "R", "R", "TOLKIEN"]
    assert kisi_tokens("Ayşe Yılmaz") == ["AYSE", "YILMAZ"]
    assert kisi_tokens("Владимир Путин") == []  # Latin olmayan: indekse girmez


def test_sirket_tokens_bicim_atilir():
    assert tarama.sirket_tokens("Rosneft Trading S.A.") == ["ROSNEFT", "TRADING"]
    assert tarama.sirket_tokens("Limited") == ["LTD"]  # yalnız biçimse olduğu gibi


@pytest.mark.parametrize("a,b", [("KHODORKOVSKY", "HODORKOVSKI"), ("MOHAMMED", "MUHAMMAD"),
                                 ("PRIGOZHIN", "PRIGOJIN"), ("ALEKSANDR", "ALEXANDER"),
                                 ("YEVGENY", "EVGENIY")])
def test_fonetik_harf_cevirisi(a, b):
    assert fonetik(a) == fonetik(b)


def test_fonetik_ic_kh_kisaltmaz():
    assert fonetik("POTEKHIN") != fonetik("PUTIN")


# ----------------------------------------------------------------- isim skoru
def t(ad):
    return kisi_tokens(ad)


def test_kelime_sirasi_onemsiz():
    assert isim_skoru(t("Putin Vladimir"), t("Vladimir Putin"), "kisi") == 1.0


def test_gobek_adi_dusmesi_kucuk_ceza():
    s = isim_skoru(t("Vladimir Putin"), t("Vladimir Vladimirovich Putin"), "kisi")
    assert 0.95 <= s < 1.0


def test_harf_cevirisi_yuksek():
    assert isim_skoru(t("Mikhail Hodorkovski"), t("Mikhail Khodorkovsky"), "kisi") >= 0.93


def test_ortak_ilk_ad_yetmez():
    # token-sort bunu 0.90 yapıyordu; her kelimenin tutması gerekir.
    assert isim_skoru(t("Vladimir Putin"), t("Vladimir Ustinov"), "kisi") < 0.85


def test_baba_adi_ilk_ad_degil():
    assert isim_skoru(t("Vladimir Putin"), t("Vitalii Vladimirovich Potekhin"), "kisi") < 0.85


def test_bas_harf_ters_yonde_zayif():
    assert isim_skoru(t("James O Wilson"), t("James Overland"), "kisi") < 0.85
    # Baş harf zayıf kanıt: eşiğin hemen altı, doğum uyuşursa (+0.05) alarm olur.
    assert 0.85 <= isim_skoru(t("J Smith"), t("John Smith"), "kisi") < tarama.ESIK


def test_tek_kelime_alarm_uretmez():
    assert isim_skoru(t("Putin"), t("Vladimir Putin"), "kisi") <= tarama.TEK_KELIME_TAVANI


def test_kisa_kelime_fonetik_birlesmez():
    assert isim_skoru(t("John Smith"), t("Jane Smith"), "kisi") < 0.9


# ----------------------------------------------------------------- doğum ve uyruk
@pytest.mark.parametrize("q,c,durum", [
    ("1975-03", ["1975-03-12"], "uyusuyor"),
    ("1975-03", ["1975"], "uyusuyor"),           # ortak hassasiyet: yıl
    ("1975", ["1975-07-01"], "uyusuyor"),
    ("1975-03", ["1975-08-01"], "yakin"),        # aynı yıl, farklı ay: hata payı
    ("1975-03", ["1976"], "yakin"),
    ("1975-03", ["1955-01-01"], "celisiyor"),
    ("1975-03", ["1955", "1975-03-02"], "uyusuyor"),  # birden çok tarih: biri yeter
    (None, ["1975"], "bilinmiyor"),
    ("1975-03", [], "bilinmiyor"),
])
def test_dogum(q, c, durum):
    assert dogum_karsilastir(q, c)[0] == durum


def test_uyruk_sifat_iso():
    assert uyruk_kodu("British") == {"gb"}
    assert uyruk_kodu("British,Irish") == {"gb", "ie"}
    assert uyruk_kodu("Martian") == set()
    assert tarama.uyruk_karsilastir("Russian", ["ru", "cy"])[0] == "uyusuyor"
    assert tarama.uyruk_karsilastir("British", ["ru"])[0] == "celisiyor"


# ----------------------------------------------------------------- liste türü
def test_liste_turu():
    assert liste_turu(["EU-RUS"], []) == "yaptirim"
    assert liste_turu([], ["UK FCDO Sanctions List"]) == "yaptirim"  # sanctions alanı boş olsa da
    # 'OpenSanctions' kelimesi yaptırım sayılmaz
    assert liste_turu([], ["PEP position annotations by OpenSanctions"]) == "pep"
    assert liste_turu([], ["Interpol Red Notices"]) == "diger"
    # 'sanctions' alanı dolu ama yaptırım değil: yönetici yasağı / ihale yasağı
    assert liste_turu(["company directors disqualification act 1986"],
                      ["UK Companies House Disqualified Directors"]) == "diger"
    assert liste_turu(["excluded"], ["US SAM Procurement Exclusions"]) == "diger"
    assert liste_turu([], ["Pakistan NACTA Proscribed Persons"]) == "yaptirim"


# ----------------------------------------------------------------- indeks
def test_indeks_sayaclari(tarama_ix):
    sayac = tarama_ix.meta["sayac"]
    assert sayac["latin_ad_yok"] == 1          # yalnız Kiril adlı kayıt
    assert tarama_ix.kayit("ship-1") is None   # gemi indekse girmez
    assert tarama_ix.kayit("fcdo-1").liste == "yaptirim"
    assert tarama_ix.kayit("Q-pep").liste == "pep"
    assert not tarama_ix.bayat_mi()


def test_tarama_alarm_ve_gerekce(tarama_ix):
    r = tarama_ix.tara(Sorgu(ad="Mr Vladimir PUTIN", tip="kisi", dogum="1952-10", uyruk="Russian"))
    assert r["sonuc"] == "alarm"
    h = r["alarmlar"][0]
    assert h["candidate_id"] == "Q7747" and h["list"] == "yaptirim"
    assert "doğum 1952-10 uyuşuyor" in h["gerekce"] and "uyruk ru uyuşuyor" in h["gerekce"]


def test_tarama_yazim_hatasi_adayi_bulur(tarama_ix):
    r = tarama_ix.tara(Sorgu(ad="Vladimir Poutin", tip="kisi"))
    assert r["sonuc"] == "alarm" and r["alarmlar"][0]["candidate_id"] == "Q7747"


def test_tip_uyumu(tarama_ix):
    # Gemi ve kişi kaydı aynı adda; şirket sorgusu ikisine de bakmaz.
    assert tarama_ix.tara(Sorgu(ad="Vladimir Putin", tip="sirket"))["sonuc"] == "temiz"
    r = tarama_ix.tara(Sorgu(ad="Rosneft Oil Co LLC", tip="sirket"))
    assert r["sonuc"] == "alarm" and r["alarmlar"][0]["candidate_id"] == "NK-ros"


def test_dogum_celiskisi_bastirilir_ama_kaybolmaz(tarama_ix):
    r = tarama_ix.tara(Sorgu(ad="John Smith", tip="kisi", dogum="1985-06", uyruk="British"))
    assert r["sonuc"] == "temiz"
    assert [b["candidate_id"] for b in r["bastirilan"]] == ["Q-pep"]
    assert r["bastirilan"][0]["dogum"] == "celisiyor"


def test_temiz(tarama_ix):
    r = tarama_ix.tara(Sorgu(ad="Ayşe Yılmaz", tip="kisi", dogum="1980-05"))
    assert r["sonuc"] == "temiz" and not r["alarmlar"] and not r["bastirilan"]


def test_bayat_indeks(tmp_path):
    from conftest import kur_tarama_indeksi

    ix = kur_tarama_indeksi(tmp_path)
    assert not ix.bayat_mi()
    (tmp_path / "os.csv").write_text("degisti", encoding="utf-8")
    assert ix.bayat_mi()


def test_indeks_yoksa_acik_hata(tmp_path):
    with pytest.raises(IndeksYok):
        TaramaIndeksi(tmp_path / "yok.sqlite")
