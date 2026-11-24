"""Pay oranı analizi: çıkarımdan SONRA çalışan saf fonksiyon.

LLM çağırmaz, girdiyi değiştirmez, aynı girdiye aynı çıktıyı verir.
Karar vermez; sadece uyarı kodu koyar. Türkçe açıklamayı rapor katmanı yazar.
Yuvarlama yapılmaz; yuvarlama sadece gösterimde.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel

from kyb.extraction import SirketBilgisi

# Tutarlar tamsayı TL: toplam ve karşılaştırma kesin (sum, ==), tolerans yok.
# Oran (puan): belge oranı 2 ondalığa yuvarlar, ortak başına en fazla 0,005 hata.
# 0,05 -> 10 ortağa kadar yuvarlama birikimini tolere eder (3 x 33,33 = 99,99).
ORAN_TOLERANSI = 0.05

Uyari = Literal[
    "PAY_SERMAYEDEN_BUYUK",      # ortak: pay_tutari > sermaye_tutari
    "ORAN_TUTARSIZ",             # ortak: hesaplanan ile belgedeki oran farklı
    "ORAN_BILINMIYOR",           # ortak: ne hesaplanabildi ne belgede var
    "SERMAYE_TOPLAMI_TUTMUYOR",  # şirket: pay tutarları toplamı != sermaye
    "EKSIK_PAY_BILGISI",         # şirket: bazı ortaklarda tutar var, bazılarında yok
    "ORAN_TOPLAMI_TUTMUYOR",     # şirket: nihai oranlar toplamı != 100
]
Kaynak = Literal["hesaplandi", "belgeden", "yok"]


class OrtakOrani(BaseModel):
    ad: str
    hesaplanan_oran: float | None
    belgedeki_oran: float | None
    nihai_oran: float | None
    kaynak: Kaynak
    uyarilar: list[Uyari]


class OranAnalizi(BaseModel):
    ortaklar: list[OrtakOrani]
    toplam_pay_tutari: int | None     # sadece tüm ortaklarda tutar varsa
    toplam_oran: float | None         # sadece tüm ortaklarda nihai oran varsa
    uyarilar: list[Uyari]


def _yakin(a: float, b: float, tol: float) -> bool:
    return math.isclose(a, b, rel_tol=0, abs_tol=tol)


def hesapla_oranlar(bilgi: SirketBilgisi) -> OranAnalizi:
    """Ortak oranlarını hesaplar, belgeyle karşılaştırır, tutarsızlıkları işaretler."""
    sermaye = bilgi.sermaye_tutari
    ortaklar: list[OrtakOrani] = []

    for o in bilgi.ortaklar:
        uyarilar: list[Uyari] = []
        hesaplanan = None
        if o.pay_tutari is not None and sermaye is not None:
            hesaplanan = o.pay_tutari / sermaye * 100
            if o.pay_tutari > sermaye:
                uyarilar.append("PAY_SERMAYEDEN_BUYUK")
            if o.pay_orani is not None and not _yakin(hesaplanan, o.pay_orani, ORAN_TOLERANSI):
                uyarilar.append("ORAN_TUTARSIZ")

        # Hesaplanan önce: iki ham sayıdan kodla üretildi, ikisi de grounding'den geçer.
        kaynak: Kaynak
        if hesaplanan is not None:
            nihai, kaynak = hesaplanan, "hesaplandi"
        elif o.pay_orani is not None:
            nihai, kaynak = o.pay_orani, "belgeden"
        else:
            nihai, kaynak = None, "yok"
            uyarilar.append("ORAN_BILINMIYOR")

        ortaklar.append(OrtakOrani(ad=o.ad, hesaplanan_oran=hesaplanan,
                                   belgedeki_oran=o.pay_orani, nihai_oran=nihai,
                                   kaynak=kaynak, uyarilar=uyarilar))

    sirket_uyarilari: list[Uyari] = []
    toplam_tutar: int | None = None
    toplam_oran: float | None = None
    if bilgi.ortaklar:  # ortak yoksa toplam kontrolü yok; geçerli bir durum
        tutarlar = [o.pay_tutari for o in bilgi.ortaklar]
        if all(t is not None for t in tutarlar):
            toplam_tutar = sum(tutarlar)
            if sermaye is not None and toplam_tutar != sermaye:
                sirket_uyarilari.append("SERMAYE_TOPLAMI_TUTMUYOR")
        elif any(t is not None for t in tutarlar):
            # Eksikleri 0 saymıyoruz: sorunsuz belgede yanlış alarm üretirdi.
            sirket_uyarilari.append("EKSIK_PAY_BILGISI")

        oranlar = [x.nihai_oran for x in ortaklar]
        if all(r is not None for r in oranlar):
            toplam_oran = math.fsum(oranlar)
            if not _yakin(toplam_oran, 100, ORAN_TOLERANSI):
                sirket_uyarilari.append("ORAN_TOPLAMI_TUTMUYOR")

    return OranAnalizi(ortaklar=ortaklar, toplam_pay_tutari=toplam_tutar,
                       toplam_oran=toplam_oran, uyarilar=sirket_uyarilari)
