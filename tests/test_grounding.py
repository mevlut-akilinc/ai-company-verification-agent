"""Grounding: Türkçe sayı biçimi ve sayısal alan kontrolleri."""

import pytest

from kyb.extraction import Ortak, SirketBilgisi
from kyb.grounding import ground, sayilari_bul


@pytest.mark.parametrize("metin, beklenen", [
    ("1.000.000 TL", [1000000.0]),
    ("12.500,50 TL", [12500.5]),
    ("25 TL itibari değerde", [25.0]),
    ("%33,33", [33.33]),
    ("23.11.2026 tarihinde", []),          # 23.11 ya da 11.202 olmamalı
    ("payı %60.", [60.0]),                 # cümle sonu noktası ondalık değil
    ("1.000", [1000.0]),                   # Türkçe: hep "bin"
])
def test_sayilari_bul(metin, beklenen):
    assert sayilari_bul(metin) == beklenen


BELGE = (
    "Örnek Ticaret A.Ş. 23.11.2026 tarihinde tescil edilmiştir. "
    "Şirketin sermayesi 1.000.000 TL'dir. "
    "Ali Yılmaz 600.000 TL karşılığı %60 paya sahiptir. "
    "Ayşe Kaya 400.000 TL karşılığı %40 paya sahiptir."
)


def _sonuc(sermaye: int, ali_tutar: int, ali_oran: float) -> SirketBilgisi:
    return SirketBilgisi(
        unvan="Örnek Ticaret A.Ş.",
        sicil_no=None,
        sermaye_tutari=sermaye,
        ortaklar=[
            Ortak(ad="Ali Yılmaz", tur="gercek", pay_tutari=ali_tutar, pay_orani=ali_oran,
                  kanit="Ali Yılmaz 600.000 TL karşılığı %60 paya sahiptir."),
        ],
    )


def _durumlar(rapor) -> dict[str, str]:
    return {k.alan: k.durum for k in rapor.kontroller}


def test_turkce_bicimli_sayilar_gecer():
    d = _durumlar(ground(_sonuc(1_000_000, 600_000, 60), BELGE))
    assert d["sermaye_tutari"] == "gecti"
    assert d["ortaklar[0].pay_tutari"] == "gecti"
    assert d["ortaklar[0].pay_orani"] == "gecti"


def test_belgede_olmayan_sayi_kalir():
    d = _durumlar(ground(_sonuc(1_500_000, 600_000, 60), BELGE))
    assert d["sermaye_tutari"] == "kaldi"


def test_baska_ortagin_degeri_kanitta_yok():
    # 400.000 belgede var ama Ali'nin kanıtında yok.
    d = _durumlar(ground(_sonuc(1_000_000, 400_000, 40), BELGE))
    assert d["ortaklar[0].pay_tutari"] == "kaldi"
    assert d["ortaklar[0].pay_orani"] == "kaldi"


def test_kanit_belgede_yoksa_bagli_kontroller_gecemez():
    # Uydurma kanıt kendi içinde tutarlı: ad ve sayılar kanıtta geçiyor.
    sonuc = SirketBilgisi(unvan="Örnek Ticaret A.Ş.", sicil_no=None, sermaye_tutari=1_000_000, ortaklar=[
        Ortak(ad="Veli Demir", tur="gercek", pay_tutari=900_000, pay_orani=90,
              kanit="Veli Demir 900.000 TL karşılığı %90 paya sahiptir."),
    ])
    d = _durumlar(ground(sonuc, BELGE))
    assert d["ortaklar[0].kanit"] == "kaldi"
    for alan in ("ad", "pay_tutari", "pay_orani"):
        assert d[f"ortaklar[0].{alan}"] == "kaldi"


def test_null_sayi_atlanir():
    sonuc = SirketBilgisi(unvan="Örnek Ticaret A.Ş.", sicil_no=None, sermaye_tutari=None, ortaklar=[])
    assert _durumlar(ground(sonuc, BELGE))["sermaye_tutari"] == "atlandi"
