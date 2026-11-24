"""Entity resolution: şirket adlarını karşılaştırma ve aday seçimi.

LLM yok, saf fonksiyonlar: deterministik ve test edilebilir. Aday bulma (graf
sorgusu) store katmanında; burada sadece puanlama ve karar var.

Karar kuralı (decide):
  eslesti   : en iyi aday >= KABUL_ESIGI ve ikinciden en az FARK_ESIGI önde
  belirsiz  : en iyi aday >= INCELEME_ESIGI ama kabul koşulu sağlanmadı
  yok       : hiçbir aday INCELEME_ESIGI'ne ulaşmadı
Belirsiz durumda seçim yapılmaz; adaylar analiste gösterilir.

Kardeş şirket kuralı: grup yapılarında şirketler tek bir ayırt edici kelimeyle
ayrılır (X BIDCO / X MIDCO, FUND II / FUND VI, X LIMITED / X UK LIMITED). Bu
kelimelerden biri farklıysa karakter benzerliği yüksek olsa da puan
AYIRICI_TAVAN'da kesilir: otomatik eşleşmez, en fazla 'belirsiz' olur.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Literal

KABUL_ESIGI = 0.93
INCELEME_ESIGI = 0.85
FARK_ESIGI = 0.02
# Hukuki biçim farklıysa (LTD vs PLC vs LLP) büyük olasılıkla farklı tüzel kişi.
BICIM_CEZASI = 0.05
AYIRICI_TAVAN = 0.90
_ROMA = re.compile(r"^(?=[IVX]+$)X{0,3}(IX|IV|V?I{0,3})$")
AYIRICI_KELIMELER = {"UK", "GB", "GP", "LP", "BIDCO", "MIDCO", "TOPCO", "HOLDCO", "FINCO", "PROPCO",
                     "OPCO", "DEBTCO", "NOMINEE", "NOMINEES", "TRUSTEE", "TRUSTEES", "INTERNATIONAL",
                     "EUROPE", "GLOBAL", "OVERSEAS", "SERVICES", "FINANCE", "PROPERTIES",
                     "INVESTMENTS", "TRADING", "GROUP", "HOLDINGS",
                     # ülke/bölge: X (US) HOLDINGS ile X HOLDINGS farklı tüzel kişi
                     "US", "USA", "IRELAND", "EU", "ASIA", "AMERICA", "AMERICAS", "JERSEY",
                     "GUERNSEY", "SCOTLAND", "WALES", "ENGLAND", "LUXEMBOURG", "NETHERLANDS"}

# Çok kelimeli biçimler önce, tek kelimeye indirgenir.
_IFADELER = [
    (r"\bPUBLIC LIMITED COMPANY\b", "PLC"),
    (r"\bLIMITED LIABILITY PARTNERSHIP\b", "LLP"),
    (r"\bLIMITED LIABILITY COMPANY\b", "LLC"),
    (r"\bLIMITED PARTNERSHIP\b", "LP"),
    (r"\bCOMMUNITY INTEREST COMPANY\b", "CIC"),
]
_ESANLAMLI = {"LIMITED": "LTD", "INCORPORATED": "INC", "CORPORATION": "CORP",
              "COMPANY": "CO", "AND": "&"}
BICIMLER = {"LTD", "PLC", "LLP", "LP", "LLC", "CIC", "INC", "CORP", "CO", "SA", "SAS", "SARL",
            "SPA", "SRL", "BV", "NV", "AG", "GMBH", "KG", "AB", "AS", "OY", "SE", "ULC", "PTE"}

Karar = Literal["eslesti", "belirsiz", "yok"]


def _ascii(metin: str) -> str:
    # Renováveis -> Renovaveis; İ -> I. Türkçe ı noktasız kalır, ayrıca eşlenir.
    metin = metin.replace("ı", "i").replace("İ", "I")
    return unicodedata.normalize("NFKD", metin).encode("ascii", "ignore").decode()


def tokens(ad: str) -> list[str]:
    """Karşılaştırma için kanonik kelimeler (büyük harf, noktalama yok, biçimler kısaltılmış)."""
    s = _ascii(ad).upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s.replace(".", " "))
    s = re.sub(r"\s+", " ", s).strip()
    # S A -> SA, B V -> BV: tek harf dizilerini birleştir (S.A., B.V., N.V.)
    s = re.sub(r"\b([A-Z])(?: ([A-Z]))+\b", lambda m: m.group(0).replace(" ", ""), s)
    for desen, yerine in _IFADELER:
        s = re.sub(desen, yerine, s)
    return [_ESANLAMLI.get(t, t) for t in s.split()]


def ayir(ad: str) -> tuple[str, frozenset[str]]:
    """(çekirdek ad, hukuki biçim). Sadece EN SONDAKİ biçim kelimesi alınır:
    'AA CORPORATION LIMITED'da CORPORATION adın parçasıdır; ikisini birden atmak
    onu 'AA LIMITED' ile aynı yapardı. Tek istisna 'GMBH & CO KG' gibi bileşikler."""
    t = tokens(ad)
    bicim: list[str] = []
    if len(t) > 1 and t[-1] in BICIMLER:
        bicim.append(t.pop())
        if bicim == ["KG"] and t[-2:] == ["&", "CO"]:
            t = t[:-2]
    return " ".join(t), frozenset(bicim)


def ayirici_mi(kelime: str) -> bool:
    """Kardeş şirketleri ayıran kelime: sayı içeren, Roma rakamı ya da grup rolü."""
    return (len(kelime) == 1 or any(c.isdigit() for c in kelime) or bool(_ROMA.match(kelime))
            or kelime in AYIRICI_KELIMELER)


def benzerlik(a: str, b: str) -> float:
    """0-1. Çekirdek ad birebir aynıysa 1 (biçim farkı hariç)."""
    ca, ba = ayir(a)
    cb, bb = ayir(b)
    if not ca or not cb:
        oran = SequenceMatcher(None, " ".join(tokens(a)), " ".join(tokens(b))).ratio()
    elif ca == cb:
        oran = 1.0
    else:
        duz = SequenceMatcher(None, ca, cb).ratio()
        sirali = SequenceMatcher(None, " ".join(sorted(ca.split())),
                                 " ".join(sorted(cb.split()))).ratio()
        oran = max(duz, sirali)
        if any(ayirici_mi(k) for k in set(ca.split()) ^ set(cb.split())):
            oran = min(oran, AYIRICI_TAVAN)
    if ba and bb and not (ba & bb):
        oran -= BICIM_CEZASI
    return max(0.0, round(oran, 4))


def arama_sorgusu(ad: str) -> str | None:
    """Lucene fulltext sorgusu: çekirdek kelimeler, özel karakterler kaçışlı.

    Biçim kelimeleri (LTD, PLC) atılır, yoksa her LIMITED şirket aday olur.
    """
    cekirdek, _ = ayir(ad)
    kelimeler = [k for k in cekirdek.split() if k != "&"]
    if not kelimeler:
        return None
    return " ".join(re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", k) for k in kelimeler)


def decide(adaylar: list[dict]) -> tuple[Karar, dict | None]:
    """adaylar: 'score' alanı olan, puana göre azalan sıralı liste."""
    if not adaylar or adaylar[0]["score"] < INCELEME_ESIGI:
        return "yok", None
    en_iyi = adaylar[0]
    ikinci = adaylar[1]["score"] if len(adaylar) > 1 else 0.0
    if en_iyi["score"] >= KABUL_ESIGI and en_iyi["score"] - ikinci >= FARK_ESIGI:
        return "eslesti", en_iyi
    return "belirsiz", None


# --------------------------------------------------------------------------- çözümleme (store ile)
from kyb.config import GLEIF_RA_COMPANIES_HOUSE, GLEIF_RA_UNKNOWN  # noqa: E402

ULKE_CEZASI = 0.10        # UK şirketi için GB dışı LEI adayı
ADAY_SAYISI = 10


def _puanla(ad: str, adaylar: list[dict], anahtar: str, ceza=None) -> list[dict]:
    """Adayları ada göre puanlar; ceza(aday) -> (düşülecek puan, not)."""
    sonuc = []
    for a in adaylar:
        puan = benzerlik(ad, a.get("name") or "")
        notlar = []
        if ceza:
            dus, n = ceza(a)
            if dus:
                puan = max(0.0, round(puan - dus, 4))
                notlar.append(n)
        sonuc.append({"key": a.get(anahtar), "name": a.get("name"), "score": puan,
                      "notes": notlar, **{k: a.get(k) for k in ("country", "ra_id") if k in a}})
    # Eşit puanda anahtara göre sırala: sonuç deterministik kalsın.
    return sorted(sonuc, key=lambda x: (-x["score"], str(x["key"])))


def _ara(store, label: str, ad: str) -> list[dict]:
    q = arama_sorgusu(ad)
    return store.search(label, q, ADAY_SAYISI) if q else []


def resolve_subject(store, company_number: str | None, name: str | None) -> dict:
    """Konu şirketi graftaki Company düğümüne bağlar."""
    from kyb.normalize import normalize_company_number

    uyarilar: list[str] = []
    numara = normalize_company_number(company_number)
    if company_number and not numara:
        uyarilar.append("NUMARA_GECERSIZ")
    if numara:
        c = store.company(numara)
        if c:
            sonuc = {"karar": "eslesti", "method": "registry_id", "score": 1.0,
                     "company_number": numara, "name": c.get("name"), "candidates": []}
            if name and benzerlik(name, c.get("name") or "") < INCELEME_ESIGI:
                uyarilar.append("UNVAN_UYUSMUYOR")
            return {**sonuc, "uyarilar": uyarilar}
        uyarilar.append("NUMARA_SICILDE_YOK")
    if not name:
        return {"karar": "yok", "method": None, "score": None, "company_number": None,
                "name": None, "candidates": [], "uyarilar": uyarilar}

    adaylar = _puanla(name, _ara(store, "Company", name), "company_number")
    karar, secilen = decide(adaylar)
    return {"karar": karar, "method": "name_search",
            "score": secilen["score"] if secilen else None,
            "company_number": secilen["key"] if secilen else None,
            "name": secilen["name"] if secilen else None,
            "candidates": adaylar[:5], "uyarilar": uyarilar}


def link_lei(store, company: dict) -> dict:
    """UK şirketini GLEIF kaydına bağlar. Sadece ad + ülke + sicil makamı kullanılır;
    sicil numarası (ra_entity_id) grafa yüklenmez, gold cevap sızmaz."""
    def ceza(a):
        if a.get("country") and a["country"] != "GB":
            return ULKE_CEZASI, f"ülke {a['country']}"
        if a.get("ra_id") and a["ra_id"] not in GLEIF_RA_COMPANIES_HOUSE | {GLEIF_RA_UNKNOWN}:
            return BICIM_CEZASI, f"sicil makamı {a['ra_id']}"
        return 0, ""

    adaylar = _puanla(company["name"], _ara(store, "LEIEntity", company["name"]), "lei", ceza)
    karar, secilen = decide(adaylar)
    return {"karar": karar, "lei": secilen["key"] if secilen else None,
            "name": secilen["name"] if secilen else None,
            "score": secilen["score"] if secilen else None, "candidates": adaylar[:5]}


def resolve_owner(store, entity: dict) -> dict:
    """Sicilde çözülemeyen tüzel sahibi önce UK şirketlerine, sonra GLEIF'e bağlamayı dener.
    Yabancı kayıtlı sahip UK şirketiyle eşleştirilmez (aynı adlı farklı tüzel kişi riski)."""
    from kyb.normalize import looks_uk_registered

    uk = looks_uk_registered({"country_registered": entity.get("country_registered")})
    sonuc = {"entity_key": entity.get("entity_key"), "name": entity.get("name"),
             "country_registered": entity.get("country_registered"), "uk": uk,
             "karar": "yok", "match": None, "candidates": []}
    hedefler = [("Company", "company_number")] if uk else []
    hedefler.append(("LEIEntity", "lei"))
    for label, anahtar in hedefler:
        adaylar = _puanla(entity["name"], _ara(store, label, entity["name"]), anahtar)
        karar, secilen = decide(adaylar)
        if karar == "eslesti":
            return {**sonuc, "karar": karar, "match": {"label": label, **secilen},
                    "candidates": adaylar[:3]}
        if karar == "belirsiz" and sonuc["karar"] == "yok":
            sonuc |= {"karar": "belirsiz", "candidates": adaylar[:3]}
    return sonuc
