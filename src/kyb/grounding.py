"""Grounding kontrolü: LLM çıktısındaki her iddiayı belge metnine geri bağlar.

LLM yok, sadece metin karşılaştırması: deterministik ve test edilebilir.
Hiçbir değeri silmez ya da düzeltmez; her kontrolü işaretler, karar analiste kalır.

Durumlar:
  gecti   : normalize edildikten sonra belgede/kanıtta birebir bulundu
  kismi   : birebir yok ama çok benzer bir parça var (analist bakmalı)
  kaldi   : dayanağı bulunamadı (büyük olasılıkla uydurma)
  atlandi : değer null, kontrol edilecek bir şey yok
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel

from kyb.extraction import SirketBilgisi

Durum = Literal["gecti", "kismi", "kaldi", "atlandi"]

BENZERLIK_ESIGI = 0.9
SAYI_TOLERANSI = 0.01

# Aynı anlama gelen tırnak/kesme işaretleri -> tek karakter.
_TIRNAKLAR = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'", "“": '"', "”": '"'})
_SAYI_RE = re.compile(r"\d+(?:[.,]\d+)?")


class Kontrol(BaseModel):
    alan: str
    deger: str | float | None
    durum: Durum
    not_: str = ""


class GroundingRaporu(BaseModel):
    kontroller: list[Kontrol]

    @property
    def kalan_sayisi(self) -> int:
        return sum(k.durum == "kaldi" for k in self.kontroller)

    @property
    def kismi_sayisi(self) -> int:
        return sum(k.durum == "kismi" for k in self.kontroller)

    @property
    def dogrulandi(self) -> bool:
        """Hiç 'kaldi' ve 'kismi' yoksa True."""
        return self.kalan_sayisi == 0 and self.kismi_sayisi == 0


# --------------------------------------------------------------------------- yardımcılar
def normalize(metin: str) -> str:
    """Sadece karşılaştırma için. Harf büyüklüğüne dokunmaz (Türkçe İ/i tuzağı)."""
    metin = unicodedata.normalize("NFC", metin)
    metin = metin.translate(_TIRNAKLAR)
    return re.sub(r"\s+", " ", metin).strip()


def en_iyi_benzerlik(parca: str, metin: str) -> float:
    """`parca`nın `metin` içindeki en benzer pencereyle oranı (0-1).

    Parça uzunluğunda bir pencere kelime sınırlarından kaydırılır.
    """
    if not parca or not metin:
        return 0.0
    n = len(parca)
    baslangiclar = [0] + [m.end() for m in re.finditer(r" ", metin)]
    en_iyi = 0.0
    for i in baslangiclar:
        pencere = metin[i:i + n]
        oran = SequenceMatcher(None, parca, pencere).ratio()
        if oran > en_iyi:
            en_iyi = oran
            if en_iyi == 1.0:
                break
    return en_iyi


def sayilari_bul(metin: str) -> list[float]:
    """'%60', '12,5', '40.0' gibi ifadelerdeki sayıları float olarak döndürür."""
    return [float(s.replace(",", ".")) for s in _SAYI_RE.findall(metin)]


# --------------------------------------------------------------------------- kontroller
def _metin_kontrol(alan: str, deger: str | None, kaynak: str) -> Kontrol:
    """Değer kaynakta geçiyor mu? Birebir -> gecti, benzer -> kismi, yoksa kaldi."""
    if deger is None:
        return Kontrol(alan=alan, deger=None, durum="atlandi")
    d = normalize(deger)
    if d in kaynak:
        return Kontrol(alan=alan, deger=deger, durum="gecti")
    oran = en_iyi_benzerlik(d, kaynak)
    durum: Durum = "kismi" if oran >= BENZERLIK_ESIGI else "kaldi"
    return Kontrol(alan=alan, deger=deger, durum=durum, not_=f"en iyi benzerlik {oran:.2f}")


def _sicil_kontrol(sicil_no: str | None, belge: str) -> Kontrol:
    """Numara belgede tam sayı olarak geçmeli: '845' değeri '845123' içinde sayılmaz."""
    if sicil_no is None:
        return Kontrol(alan="sicil_no", deger=None, durum="atlandi")
    desen = rf"(?<!\d){re.escape(sicil_no)}(?!\d)"
    if re.search(desen, belge):
        return Kontrol(alan="sicil_no", deger=sicil_no, durum="gecti")
    return Kontrol(alan="sicil_no", deger=sicil_no, durum="kaldi",
                   not_="belgede bu numara tam sayı olarak geçmiyor")


def _pay_kontrol(alan: str, pay: float | None, kanit: str) -> Kontrol:
    """Pay oranı sayısı kanıt metninde geçmeli (modelin hesapladığı değerleri yakalar)."""
    if pay is None:
        return Kontrol(alan=alan, deger=None, durum="atlandi")
    sayilar = sayilari_bul(kanit)
    if any(abs(s - pay) < SAYI_TOLERANSI for s in sayilar):
        return Kontrol(alan=alan, deger=pay, durum="gecti")
    return Kontrol(alan=alan, deger=pay, durum="kaldi",
                   not_=f"kanıttaki sayılar: {sayilar}")


def ground(sonuc: SirketBilgisi, belge_metni: str) -> GroundingRaporu:
    """Çıkarım sonucundaki her iddiayı belgeye karşı kontrol eder."""
    belge = normalize(belge_metni)
    kontroller = [
        _metin_kontrol("unvan", sonuc.unvan, belge),
        _sicil_kontrol(sonuc.sicil_no, belge),
    ]
    for i, ortak in enumerate(sonuc.ortaklar):
        on = f"ortaklar[{i}]"
        kanit = normalize(ortak.kanit)
        # Kanıt belgede mi?
        kontroller.append(_metin_kontrol(f"{on}.kanit", ortak.kanit, belge))
        # Ad, belgenin tamamında değil kendi kanıtının içinde aranır:
        # isim belgede başka bir rolle (ör. yönetim kurulu) de geçebilir.
        kontroller.append(_metin_kontrol(f"{on}.ad", ortak.ad, kanit))
        kontroller.append(_pay_kontrol(f"{on}.pay_orani", ortak.pay_orani, kanit))
    return GroundingRaporu(kontroller=kontroller)
