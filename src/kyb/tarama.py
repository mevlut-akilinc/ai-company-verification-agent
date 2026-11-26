"""Yaptırım / PEP taraması: normalizasyon, skor ve karar.

LLM yok, saf fonksiyonlar. Aday bulma (SQLite indeksi) kyb.data.tarama_indeks'te;
burada sadece bir sorgu ile bir liste kaydının ne kadar benzediği ve karar var.

Skor = isim skoru + doğum etkisi + uyruk etkisi (0..1 arasına kırpılır).
  isim   : tüm alias'lar arasında en yüksek benzerlik (kelime hizalaması / token-sort)
  doğum  : ortak hassasiyette (yıl ya da yıl-ay) karşılaştırma. Uyuşursa küçük artış,
           çelişirse sert düşüş, bilgi yoksa etkisiz.
  uyruk  : aynı mantık, daha zayıf etki.
Karar: skor >= esik -> alarm. Sistem alarmı asla kendi kapatmaz. İsmi eşiği geçip
doğum çelişkisi yüzünden eşiğin altına düşen adaylar 'bastirilan' olarak ayrıca
döner: sessizce kaybolmazlar, rapora ve denetim izine yazılırlar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz
from rapidfuzz.distance import OSA, JaroWinkler

from kyb import er

Tip = Literal["kisi", "sirket"]
Liste = Literal["yaptirim", "pep", "diger"]

# Eşikler ölçümle seçilir (scripts/tarama_olcum.py); burası varsayılan.
ESIK = 0.90
DOGUM_UYUSUR = 0.05
DOGUM_CELISIR = -0.30
UYRUK_UYUSUR = 0.02
UYRUK_CELISIR = -0.05
# Uzun adda fazladan kelime: ortadaysa (göbek adı düşmüş olabilir) küçük ceza,
# baştaysa ya da sondaysa (ad / soyadı tutmuyor) büyük ceza.
FAZLA_KELIME_CEZASI = 0.02
UC_KELIME_CEZASI = 0.20
FONETIK_ESIT = 0.95
FONETIK_TABAN = 0.70  # gerçek harf çevirileri ~0.78-0.80, sahte kod eşleşmeleri ~0.40-0.52
ONEK_TAVANI = 0.75
# Tek kelimeli isim eşleşmesi ("PUTIN") alarm üretmesin.
TEK_KELIME_TAVANI = 0.80

UNVANLAR = {"MR", "MRS", "MS", "MISS", "MX", "DR", "SIR", "DAME", "LORD", "LADY", "PROF",
            "REV", "HON", "SHEIKH", "SHAIKH", "HRH", "CAPT", "COL", "GEN", "JR", "SR"}


# --------------------------------------------------------------------------- normalizasyon
def kisi_tokens(ad: str) -> list[str]:
    """Kişi adı kelimeleri. er.tokens'tan farkı: tek harfler birleştirilmez (J R R TOLKIEN),
    şirket eşanlamlıları uygulanmaz, unvanlar atılır. Latin olmayan harfler düşer;
    tamamen Kiril/Arapça bir alias boş liste verir ve indekse girmez."""
    s = er._ascii(ad or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s.replace("'", ""))
    return [t for t in s.split() if t not in UNVANLAR]


def sirket_tokens(ad: str) -> list[str]:
    """Şirket adı kelimeleri, hukuki biçim ekleri atılmış: listede 'X LLC' olarak geçen
    kurum sicilde 'X LIMITED' olabilir. Ad yalnız biçimden oluşuyorsa olduğu gibi kalır."""
    t = er.tokens(ad or "")
    cekirdek = [k for k in t if k not in er.BICIMLER and k != "&"]
    return cekirdek or t


def tokens(ad: str, tip: Tip) -> list[str]:
    return kisi_tokens(ad) if tip == "kisi" else sirket_tokens(ad)


def anahtar(tokenler: list[str]) -> str:
    """Kelime sırasından bağımsız kanonik biçim: PUTIN VLADIMIR == VLADIMIR PUTIN."""
    return " ".join(sorted(tokenler))


_FONETIK = [("DZH", "J"), ("ZH", "J"), ("DJ", "J"), ("PH", "F"), ("CK", "K"),
            ("X", "KS"), ("Q", "K"), ("C", "K"), ("W", "V"), ("Y", "I"), ("Z", "S")]


def fonetik(kelime: str) -> str:
    """Harf çevirisi farklarına dayanıklı kaba anahtar (aday daraltma için, skor için değil).
    Kh/H, y/i, zh/j, çift harf ve ünlü farkları aynı anahtara düşer:
    KHODORKOVSKY, HODORKOVSKI -> HDRKVSK; MOHAMMED, MUHAMMAD -> MMD."""
    s = kelime.upper()
    # Başta KH -> H (KHODORKOVSKY / HODORKOVSKY); içeride KH -> K. İçeride de H'ye
    # çevirmek kelimeyi fazla kısaltıyordu: POTEKHIN ile PUTIN aynı kodu alıyordu.
    s = ("H" + s[2:] if s.startswith("KH") else s).replace("KH", "K")
    for a, b in _FONETIK:
        s = s.replace(a, b)
    if not s:
        return s
    bas = "A" if s[0] in "AEIOU" else s[0]
    govde = re.sub(r"[AEIOUH]", "", s[1:])
    govde = re.sub(r"(.)\1+", r"\1", govde)
    return bas + govde


# --------------------------------------------------------------------------- isim skoru
def _kelime_benzerligi(a: str, b: str) -> float:
    """a kısa addan, b uzun addan. Baş harf yalnız kısa taraftaysa güçlü kanıttır
    (J ~ JOHN). Kısa addaki tam kelimeyi uzun addaki baş harfe bağlamak zayıftır:
    JAMES OVERLAND, JAMES O WILSON değildir."""
    if a == b:
        return 1.0
    if len(a) == 1:
        return 0.85 if b[0] == a else 0.0
    if len(b) == 1:
        return 0.5 if a[0] == b else 0.0
    jw = JaroWinkler.normalized_similarity(a, b)
    # Baba adı ilk ad değildir: VLADIMIR / VLADIMIROVICH, MIKHAIL / MIKHAILOVNA.
    # Jaro-Winkler'in önek bonusu bunları 0.9+ yapıyordu.
    kisa_k, uzun_k = sorted((a, b), key=len)
    if len(uzun_k) - len(kisa_k) >= 3 and uzun_k.startswith(kisa_k):
        return min(jw, ONEK_TAVANI)
    # Jaro-Winkler isimlerde cömert (JOHNSON / JANSEN 0.92); OSA ile ortalanır (Levenshtein
    # gibi ama iki harfin yer değiştirmesi tek hata: NAEMA / NAMEA).
    sim = (jw + OSA.normalized_similarity(a, b)) / 2
    # Harf çevirisi farkı (KHODORKOVSKY / HODORKOVSKI) yazım hatası değil, aynı addır.
    # Kısa kelimelerde kod çok kaba (JOHN ve JANE ikisi de JN); ilk harf de tutmalı
    # (ONUR / UNER, YILMAZ / ELMAZ aynı kodu alıyordu). KH/H başı aynı harf sayılır.
    # Kod tek başına kanıt değil, sadece zaten benzer kelimeyi yükseltir: COLIN ile
    # CALLANAN, COLQUHOUN ile CHALKIN de aynı kodu alıyor.
    if (FONETIK_TABAN <= sim < FONETIK_ESIT and min(len(a), len(b)) >= 4
            and _bas(a) == _bas(b)):
        fa = fonetik(a)
        if len(fa) >= 3 and fa == fonetik(b):
            return FONETIK_ESIT
    return sim


def _bas(kelime: str) -> str:
    return "H" if kelime.startswith("KH") else kelime[0]


# Baba adı ekleri: listede "PUTIN VLADIMIR VLADIMIROVICH" sırası yaygın; baba adı sonda
# kalsa da düşebilir.
_BABA_ADI = re.compile(r"(OVICH|EVICH|OVNA|EVNA|ICHNA|OGLU|OGLY|KYZY|ZADE)$")


def _hizala(kisa: list[str], uzun: list[str]) -> tuple[list[float], set[int]]:
    """Kısa addaki her kelimeyi uzun addaki farklı bir kelimeye açgözlü eşler.
    (kelime başına benzerlikler, uzun adda eşleşmeyen kelimelerin sırası)."""
    ciftler = sorted(((_kelime_benzerligi(a, b), i, j) for i, a in enumerate(kisa)
                      for j, b in enumerate(uzun)), reverse=True)
    kullanilan_i, kullanilan_j, skorlar = set(), set(), []
    for s, i, j in ciftler:
        if i in kullanilan_i or j in kullanilan_j:
            continue
        kullanilan_i.add(i)
        kullanilan_j.add(j)
        skorlar.append(s)
    return skorlar, set(range(len(uzun))) - kullanilan_j


def _fazla_kelime_cezasi(uzun: list[str], eslesmeyen: set[int]) -> float:
    """Ortadaki kelime (göbek adı) ya da baba adı düşebilir: küçük ceza. Baştaki ya da
    sondaki kelime düşmüşse büyük ihtimalle başka kişidir: ANDREW JOHN POWELL, ANDREW
    JOHN değildir. Ortadaki kelime sıra ters çevrilse de ortada kalır."""
    ceza = 0.0
    for j in eslesmeyen:
        uc = j in (0, len(uzun) - 1)
        ceza += UC_KELIME_CEZASI if uc and not _BABA_ADI.search(uzun[j]) else FAZLA_KELIME_CEZASI
    return ceza


def isim_skoru(q: list[str], c: list[str], tip: Tip) -> float:
    """İki kelime listesi arasında 0..1 benzerlik.

    Şirket: token-sort oranı (kelime sırası önemsiz, fazladan kelime cezalı:
    ROSNEFT / ROSNEFT TRADING farklı tüzel kişi olabilir).
    Kişi: kelime hizalaması, ortalama ile en zayıf kelimenin ortalaması. Tek bir
    karakter oranı (token-sort) ortak uzun bir ilk ada fazla ağırlık verir:
    VLADIMIR PUTIN / VLADIMIR USTINOV 0.90 alıyordu. Her kelimenin tutması gerekir."""
    if not q or not c:
        return 0.0
    if tip == "sirket":
        return fuzz.token_sort_ratio(" ".join(q), " ".join(c)) / 100
    kisa, uzun = (q, c) if len(q) <= len(c) else (c, q)
    s, eslesmeyen = _hizala(kisa, uzun)
    skor = (sum(s) / len(s) + min(s)) / 2 - _fazla_kelime_cezasi(uzun, eslesmeyen)
    if len(kisa) < 2:
        skor = min(skor, TEK_KELIME_TAVANI)
    return max(0.0, min(1.0, skor))


# --------------------------------------------------------------------------- doğum tarihi
def _parca(tarih: str) -> tuple[int, int | None] | None:
    m = re.match(r"^(\d{4})(?:-(\d{2}))?", (tarih or "").strip())
    if not m:
        return None
    return int(m.group(1)), (int(m.group(2)) if m.group(2) else None)


def dogum_karsilastir(sorgu: str | None, adaylar: list[str]) -> tuple[str, str]:
    """(durum, açıklama). durum: uyusuyor / yakin / celisiyor / bilinmiyor.

    Ortak hassasiyet: ikisinde de ay varsa yıl-ay, değilse yıl karşılaştırılır.
    Listede birden çok doğum tarihi olabilir; herhangi biri uyuşuyorsa uyuşma sayılır.
    ±1 yıl 'yakin': kaynaklarda veri hatası sık, ne artırır ne düşürür."""
    q = _parca(sorgu or "")
    tarihler = [p for p in (_parca(t) for t in adaylar) if p]
    if not q or not tarihler:
        return "bilinmiyor", "doğum bilgisi yok"
    en_iyi = "celisiyor"
    for yil, ay in tarihler:
        if yil == q[0] and (ay is None or q[1] is None or ay == q[1]):
            return "uyusuyor", f"doğum {sorgu} uyuşuyor"
        if abs(yil - q[0]) <= 1:
            en_iyi = "yakin"
    liste = ", ".join(sorted({t[:7] for t in adaylar}))
    if en_iyi == "yakin":
        return "yakin", f"doğum {sorgu} ~ {liste} (±1 yıl)"
    return "celisiyor", f"doğum çelişiyor ({sorgu} / {liste})"


# --------------------------------------------------------------------------- uyruk
# PSC uyruğu sıfat olarak gelir ("British"), OpenSanctions ISO kodu kullanır ("gb").
_UYRUK = {
    "BRITISH": "gb", "ENGLISH": "gb", "SCOTTISH": "gb", "WELSH": "gb", "NORTHERN IRISH": "gb",
    "UNITED KINGDOM": "gb", "IRISH": "ie", "AMERICAN": "us", "UNITED STATES": "us",
    "CANADIAN": "ca", "AUSTRALIAN": "au", "NEW ZEALANDER": "nz", "SOUTH AFRICAN": "za",
    "RUSSIAN": "ru", "UKRAINIAN": "ua", "BELARUSIAN": "by", "BELARUSSIAN": "by",
    "KAZAKH": "kz", "KAZAKHSTANI": "kz", "UZBEK": "uz", "AZERBAIJANI": "az", "ARMENIAN": "am",
    "GEORGIAN": "ge", "TURKISH": "tr", "CYPRIOT": "cy", "GREEK": "gr", "GERMAN": "de",
    "FRENCH": "fr", "ITALIAN": "it", "SPANISH": "es", "PORTUGUESE": "pt", "DUTCH": "nl",
    "BELGIAN": "be", "SWISS": "ch", "AUSTRIAN": "at", "SWEDISH": "se", "NORWEGIAN": "no",
    "DANISH": "dk", "FINNISH": "fi", "POLISH": "pl", "CZECH": "cz", "SLOVAK": "sk",
    "HUNGARIAN": "hu", "ROMANIAN": "ro", "BULGARIAN": "bg", "SERBIAN": "rs", "CROATIAN": "hr",
    "LITHUANIAN": "lt", "LATVIAN": "lv", "ESTONIAN": "ee", "MALTESE": "mt",
    "LUXEMBOURGISH": "lu", "MONEGASQUE": "mc", "ISRAELI": "il", "IRANIAN": "ir",
    "IRAQI": "iq", "SYRIAN": "sy", "LEBANESE": "lb", "JORDANIAN": "jo", "SAUDI": "sa",
    "SAUDI ARABIAN": "sa", "EMIRATI": "ae", "QATARI": "qa", "KUWAITI": "kw", "EGYPTIAN": "eg",
    "LIBYAN": "ly", "AFGHAN": "af", "PAKISTANI": "pk", "INDIAN": "in", "BANGLADESHI": "bd",
    "SRI LANKAN": "lk", "CHINESE": "cn", "HONG KONGER": "hk", "TAIWANESE": "tw",
    "JAPANESE": "jp", "KOREAN": "kr", "SOUTH KOREAN": "kr", "NORTH KOREAN": "kp",
    "VIETNAMESE": "vn", "THAI": "th", "MALAYSIAN": "my", "SINGAPOREAN": "sg",
    "INDONESIAN": "id", "FILIPINO": "ph", "NIGERIAN": "ng", "GHANAIAN": "gh", "KENYAN": "ke",
    "ZIMBABWEAN": "zw", "SUDANESE": "sd", "SOMALI": "so", "ETHIOPIAN": "et",
    "BRAZILIAN": "br", "MEXICAN": "mx", "ARGENTINE": "ar", "ARGENTINIAN": "ar",
    "COLOMBIAN": "co", "VENEZUELAN": "ve", "CUBAN": "cu", "CHILEAN": "cl",
    "MOLDOVAN": "md", "TAJIK": "tj", "KYRGYZ": "kg", "TURKMEN": "tm", "MYANMAR": "mm",
    "BURMESE": "mm", "NICARAGUAN": "ni",
}


def uyruk_kodu(uyruk: str | None) -> set[str]:
    """'British' -> {'gb'}; 'British,Irish' -> {'gb', 'ie'}; bilinmiyorsa boş küme."""
    if not uyruk:
        return set()
    kodlar = set()
    for parca in re.split(r"[,/;]| AND ", er._ascii(uyruk).upper()):
        kod = _UYRUK.get(re.sub(r"\s+", " ", parca).strip())
        if kod:
            kodlar.add(kod)
    return kodlar


def uyruk_karsilastir(sorgu: str | None, ulkeler: list[str]) -> tuple[str, str]:
    """OpenSanctions 'countries' alanı uyruk, vatandaşlık ve ikameti karıştırır; bu yüzden
    çelişki zayıf bir sinyaldir."""
    q = uyruk_kodu(sorgu)
    c = {u.lower() for u in ulkeler}
    if not q or not c:
        return "bilinmiyor", "uyruk bilgisi yok"
    if q & c:
        return "uyusuyor", f"uyruk {'/'.join(sorted(q & c))} uyuşuyor"
    return "celisiyor", f"uyruk farklı ({'/'.join(sorted(q))} / {'/'.join(sorted(c))})"


# --------------------------------------------------------------------------- skor ve karar
@dataclass
class Kayit:
    """İndeksteki bir liste kaydı."""
    id: str
    tip: Tip
    ad: str
    liste: Liste
    isimler: list[list[str]]           # normalize edilmiş alias kelimeleri
    dogum: list[str] = field(default_factory=list)
    ulkeler: list[str] = field(default_factory=list)
    programlar: list[str] = field(default_factory=list)
    datasets: list[str] = field(default_factory=list)


@dataclass
class Sorgu:
    ad: str
    tip: Tip
    dogum: str | None = None      # "1975-03" ya da "1975"
    uyruk: str | None = None      # PSC biçimi: "British"
    rol: str = ""                 # konu / ubo / yabanci_tuzel / zincir_tepesi
    ref: str | None = None        # psc_key, company_number vb.


_DOGUM_ETKI = {"uyusuyor": DOGUM_UYUSUR, "celisiyor": DOGUM_CELISIR}
_UYRUK_ETKI = {"uyusuyor": UYRUK_UYUSUR, "celisiyor": UYRUK_CELISIR}


def skorla(sorgu: Sorgu, kayit: Kayit, q_tokens: list[str] | None = None) -> dict:
    q = q_tokens if q_tokens is not None else tokens(sorgu.ad, sorgu.tip)
    isim, en_iyi = 0.0, []
    for c in kayit.isimler:
        s = isim_skoru(q, c, sorgu.tip)
        if s > isim:
            isim, en_iyi = s, c
    if sorgu.tip == "kisi":
        dogum, dogum_not = dogum_karsilastir(sorgu.dogum, kayit.dogum)
        uyruk, uyruk_not = uyruk_karsilastir(sorgu.uyruk, kayit.ulkeler)
    else:
        dogum, dogum_not = "bilinmiyor", ""
        uyruk, uyruk_not = "bilinmiyor", ""
    skor = isim + _DOGUM_ETKI.get(dogum, 0.0) + _UYRUK_ETKI.get(uyruk, 0.0)
    skor = round(max(0.0, min(1.0, skor)), 4)
    gerekce = " · ".join(p for p in (f"isim {isim:.2f} ({' '.join(en_iyi)})", dogum_not, uyruk_not)
                         if p)
    return {
        "candidate_id": kayit.id, "candidate_name": kayit.ad, "list": kayit.liste,
        "score": skor, "name_score": round(isim, 4), "dogum": dogum, "uyruk": uyruk,
        "gerekce": gerekce, "programs": kayit.programlar[:5], "datasets": kayit.datasets[:5],
        "candidate_birth": kayit.dogum[:3], "candidate_countries": kayit.ulkeler[:5],
    }


def karar(sorgu: Sorgu, skorlar: list[dict], esik: float = ESIK, en_fazla: int = 5) -> dict:
    """Eşik üstü -> alarm. İsmi eşik üstü ama doğum çelişkisiyle düşen -> bastırılan."""
    skorlar = sorted(skorlar, key=lambda s: s["score"], reverse=True)
    alarmlar = [s for s in skorlar if s["score"] >= esik][:en_fazla]
    bastirilan = [s for s in skorlar if s["score"] < esik <= s["name_score"]
                  and s["dogum"] == "celisiyor"][:en_fazla]
    return {
        "query": sorgu.ad, "tip": sorgu.tip, "rol": sorgu.rol, "ref": sorgu.ref,
        "dogum": sorgu.dogum, "uyruk": sorgu.uyruk,
        "sonuc": "alarm" if alarmlar else "temiz",
        "alarmlar": alarmlar, "bastirilan": bastirilan,
        "en_yakin": skorlar[0] if skorlar and not alarmlar else None,
    }
