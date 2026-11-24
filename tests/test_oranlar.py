"""hesapla_oranlar: saf fonksiyon, LLM yok."""

import pytest
from pydantic import ValidationError

from kyb.extraction import Ortak, SirketBilgisi
from kyb.oranlar import hesapla_oranlar


def _ortak(ad: str, tutar: int | None = None, oran: float | None = None) -> Ortak:
    return Ortak(ad=ad, tur="gercek", pay_tutari=tutar, pay_orani=oran, kanit=f"{ad} ortaktır.")


def _sirket(ortaklar: list[Ortak], sermaye: int | None = None) -> SirketBilgisi:
    return SirketBilgisi(unvan="Örnek A.Ş.", sicil_no=None, sermaye_tutari=sermaye,
                         ortaklar=ortaklar)


def test_tutarli_belge_uyari_yok():
    s = _sirket([_ortak("A", 500_000, 50), _ortak("B", 300_000, 30), _ortak("C", 200_000, 20)],
                sermaye=1_000_000)
    r = hesapla_oranlar(s)
    assert r.uyarilar == []
    assert all(o.uyarilar == [] for o in r.ortaklar)
    assert all(o.kaynak == "hesaplandi" for o in r.ortaklar)
    assert r.ortaklar[0].nihai_oran == pytest.approx(50)
    assert r.toplam_pay_tutari == pytest.approx(1_000_000)
    assert r.toplam_oran == pytest.approx(100)


def test_paylar_sermayeyi_tutmuyor():
    # 6. test belgesinin tuzağı: paylar toplamı sermayeye eşit değil.
    s = _sirket([_ortak("A", 500_000), _ortak("B", 400_000)], sermaye=1_000_000)
    r = hesapla_oranlar(s)
    assert "SERMAYE_TOPLAMI_TUTMUYOR" in r.uyarilar
    assert "ORAN_TOPLAMI_TUTMUYOR" in r.uyarilar


def test_uc_esit_ortak_yuvarlama_hata_degil():
    s = _sirket([_ortak("A", oran=33.33), _ortak("B", oran=33.33), _ortak("C", oran=33.33)])
    r = hesapla_oranlar(s)
    assert r.uyarilar == []
    assert all(o.kaynak == "belgeden" for o in r.ortaklar)


def test_sifir_pay_orani_reddedilir():
    # Float döngüsü 33.33e-2000 -> 0.0 üretir; sessizce %0 olmamalı.
    with pytest.raises(ValidationError):
        _ortak("A", oran=0.0)


def test_oran_tutarsiz():
    s = _sirket([_ortak("A", 500_000, 60), _ortak("B", 500_000, 50)], sermaye=1_000_000)
    r = hesapla_oranlar(s)
    assert "ORAN_TUTARSIZ" in r.ortaklar[0].uyarilar
    assert "ORAN_TUTARSIZ" not in r.ortaklar[1].uyarilar
    # Hesaplanan önceliklidir.
    assert r.ortaklar[0].nihai_oran == pytest.approx(50)


def test_pay_sermayeden_buyuk():
    s = _sirket([_ortak("A", 2_000_000)], sermaye=1_000_000)
    assert "PAY_SERMAYEDEN_BUYUK" in hesapla_oranlar(s).ortaklar[0].uyarilar


def test_oran_bilinmiyor():
    s = _sirket([_ortak("A")])
    r = hesapla_oranlar(s)
    assert r.ortaklar[0].kaynak == "yok"
    assert r.ortaklar[0].nihai_oran is None
    assert "ORAN_BILINMIYOR" in r.ortaklar[0].uyarilar
    assert r.toplam_oran is None


def test_eksik_pay_bilgisi_sifir_sayilmaz():
    s = _sirket([_ortak("A", 500_000, 50), _ortak("B", oran=50)], sermaye=1_000_000)
    r = hesapla_oranlar(s)
    assert "EKSIK_PAY_BILGISI" in r.uyarilar
    assert "SERMAYE_TOPLAMI_TUTMUYOR" not in r.uyarilar
    assert r.toplam_pay_tutari is None
    assert r.toplam_oran == pytest.approx(100)


def test_sadece_yuzde_veren_belge_eksik_sayilmaz():
    s = _sirket([_ortak("A", oran=60), _ortak("B", oran=40)])
    assert hesapla_oranlar(s).uyarilar == []


def test_ortak_yok_gecerli_durum():
    r = hesapla_oranlar(_sirket([], sermaye=1_000_000))
    assert r.ortaklar == [] and r.uyarilar == []
    assert r.toplam_pay_tutari is None and r.toplam_oran is None


def test_girdi_degismez():
    s = _sirket([_ortak("A", 500_000, 60)], sermaye=1_000_000)
    once = s.model_dump()
    hesapla_oranlar(s)
    assert s.model_dump() == once
