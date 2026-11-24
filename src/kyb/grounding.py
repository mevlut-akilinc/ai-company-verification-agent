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

import math
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

from pydantic import BaseModel

from kyb.extraction import SirketBilgisi

Durum = Literal["gecti", "kismi", "kaldi", "atlandi"]

BENZERLIK_ESIGI = 0.9
# Asıl gerekçe pay_orani: hâlâ float ve float'lar == ile karşılaştırılmaz
# (belgeden ayrıştırılan 33.33 ile modelin 33.33'ü). Tutarlar int olsa da belgede
# kuruşlu sayılar (12.500,50) geçebilir; 0,01 iki yanda da kuruştan küçük farkı tolere eder.
SAYI_TOLERANSI = 0.01

# Aynı anlama gelen tırnak/kesme işaretleri -> tek karakter.
_TIRNAKLAR = str.maketrans({"’": "'", "‘": "'", "`": "'", "´": "'", "“": '"', "”": '"'})
# Türkçe sayı: nokta binlik, virgül ondalık ayırıcı. "1.000" hep "bin" kabul edilir.
#   tam kısım : önce gruplu (1.000.000), sonra düz (25, 350000); ters sırada
#               1.000.000 içinden sadece "1" yakalanırdı.
#   ondalık   : isteğe bağlı ",50"
#   sınırlar  : önünde/arkasında rakam ya da ayırıcı+rakam olmamalı; böylece
#               23.11.2026 gibi tarihler "23.11" ya da "11.202" diye bölünmez.
_SAYI_RE = re.compile(r"(?<!\d)(?<!\d[.,])(?:\d{1,3}(?:\.\d{3})+|\d+)(?:,\d+)?(?!\d|[.,]\d)")


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
    """Türkçe yazılmış sayıları float olarak döndürür.

    '1.000.000' -> 1000000.0, '12.500,50' -> 12500.5, '%33,33' -> 33.33.
    """
    return [float(s.replace(".", "").replace(",", ".")) for s in _SAYI_RE.findall(metin)]


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


def _sayi_kontrol(alan: str, deger: float | None, sayilar: list[float], not_: str) -> Kontrol:
    """Değer, kaynaktaki sayılardan biriyle eşleşmeli (modelin hesapladığı değerleri yakalar).

    Alt metin araması yerine sayı karşılaştırması: belgede '1.000.000', modelde 1000000.
    """
    if deger is None:
        return Kontrol(alan=alan, deger=None, durum="atlandi")
    if any(math.isclose(s, deger, rel_tol=0, abs_tol=SAYI_TOLERANSI) for s in sayilar):
        return Kontrol(alan=alan, deger=deger, durum="gecti")
    return Kontrol(alan=alan, deger=deger, durum="kaldi", not_=not_)


def _en_fazla_kismi(k: Kontrol) -> Kontrol:
    """Kanıt kısmi eşleştiyse ona dayanan hiçbir kontrol 'gecti' olamaz."""
    if k.durum == "gecti":
        return k.model_copy(update={"durum": "kismi", "not_": "kanıt belgede kısmi eşleşti"})
    return k


def ground(sonuc: SirketBilgisi, belge_metni: str) -> GroundingRaporu:
    """Çıkarım sonucundaki her iddiayı belgeye karşı kontrol eder."""
    belge = normalize(belge_metni)
    belge_sayilari = sayilari_bul(belge)  # bir kez
    kontroller = [
        _metin_kontrol("unvan", sonuc.unvan, belge),
        _sicil_kontrol(sonuc.sicil_no, belge),
        # Sermayenin kanıt cümlesi yok; belgenin tamamına bakılır.
        _sayi_kontrol("sermaye_tutari", sonuc.sermaye_tutari, belge_sayilari,
                      "belgede bu sayı geçmiyor"),
    ]
    for i, ortak in enumerate(sonuc.ortaklar):
        on = f"ortaklar[{i}]"
        # Kanıt belgede mi?
        kanit_kontrol = _metin_kontrol(f"{on}.kanit", ortak.kanit, belge)
        kontroller.append(kanit_kontrol)

        # Kanıt belgede yoksa ona dayanan kontroller anlamsız: kanıtın içinde
        # aramak, uydurma metnin içinde aramak olur. Null değer iddia değildir,
        # 'atlandi' kalır (kalan_sayisi şişmesin).
        if kanit_kontrol.durum == "kaldi":
            for alan, deger in (("ad", ortak.ad), ("pay_tutari", ortak.pay_tutari),
                                ("pay_orani", ortak.pay_orani)):
                if deger is None:
                    kontroller.append(Kontrol(alan=f"{on}.{alan}", deger=None, durum="atlandi"))
                else:
                    kontroller.append(Kontrol(alan=f"{on}.{alan}", deger=deger, durum="kaldi",
                                              not_="kanıt belgede bulunamadı"))
            continue

        kanit = normalize(ortak.kanit)
        kanit_sayilari = sayilari_bul(kanit)
        not_ = f"kanıttaki sayılar: {kanit_sayilari}"
        alt = [
            # Ad, belgenin tamamında değil kendi kanıtının içinde aranır:
            # isim belgede başka bir rolle (ör. yönetim kurulu) de geçebilir.
            _metin_kontrol(f"{on}.ad", ortak.ad, kanit),
            # Pay değerleri ortağın kendi kanıtında aranır: A'ya B'nin oranını yazan
            # model yakalanır (sayı belgede var ama A'nın kanıtında yok).
            _sayi_kontrol(f"{on}.pay_tutari", ortak.pay_tutari, kanit_sayilari, not_),
            _sayi_kontrol(f"{on}.pay_orani", ortak.pay_orani, kanit_sayilari, not_),
        ]
        if kanit_kontrol.durum == "kismi":
            alt = [_en_fazla_kismi(k) for k in alt]
        kontroller.extend(alt)
    return GroundingRaporu(kontroller=kontroller)
