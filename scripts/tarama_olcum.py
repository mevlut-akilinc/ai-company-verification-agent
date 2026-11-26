"""Tarama ölçümü: farklı eşiklerde duyarlılık ve yanlış pozitif oranı.

Üç set:
  pozitif     : listeden rastgele N yaptırımlı kişi, adı bozulmuş. Hepsi alarm vermeli
                (alarm = doğru kaydın skoru >= eşik). Bozma türleri: harf çevirisi,
                kelime sırası, göbek adı düşürme, yazım hatası, doğum tarihi silme.
                Ayrıca 'daraltma duyarlılığı': doğru kayıt aday listesine girdi mi?
                Girmediyse skorlama ne yaparsa yapsın bulamaz.
  negatif     : alt kümedeki gerçek UK PSC kişileri (persons.jsonl). Neredeyse hepsi temiz
                çıkmalı. Alarmlar alarmlar_negatif.jsonl'a yazılır ve tek tek okunmalı:
                bir kısmı gerçek eşleşme olabilir (UK'de yaptırımlı sahipli şirketler var).
  zor negatif : pozitif setteki kişilerin aynı adı, doğum yılı 10-30 yıl kaydırılmış.
                Doğru kayıt alarm VERMEMELİ (bastırılmalı).

Kullanım:  python scripts/tarama_olcum.py [--pozitif 200] [--negatif 2000] [--isci 6]
Çıktı:     runs/tarama_<zaman>/  (ozet.json, tablo.md, pozitif.jsonl, alarmlar_negatif.jsonl,
           zor_negatif.jsonl)
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

from kyb import tarama
from kyb.data.readers import read_jsonl
from kyb.data.tarama_indeks import TaramaIndeksi

KOK = Path(__file__).resolve().parents[1]
PROCESSED = KOK / "data" / "processed"
RUNS_DIR = KOK / "runs"
ESIKLER = [round(0.80 + 0.02 * i, 2) for i in range(10)]  # 0.80 .. 0.98
BOZMALAR = ["harf_cevirisi", "kelime_sirasi", "gobek_adi", "yazim_hatasi", "dogum_sil"]


# --------------------------------------------------------------------------- bozma
_CEVIRI = [("KH", "H"), ("Y", "I"), ("I", "Y"), ("ZH", "J"), ("KS", "X"), ("X", "KS"),
           ("OU", "U"), ("EI", "EY"), ("EY", "EI"), ("IY", "Y"), ("TS", "C"), ("SH", "S"),
           ("V", "W"), ("W", "V"), ("J", "Y"), ("PH", "F"), ("MM", "M"), ("LL", "L"),
           ("SS", "S"), ("OO", "U"), ("AA", "A"), ("C", "K"), ("K", "C")]


def harf_cevirisi(kelimeler: list[str], rnd: random.Random) -> list[str] | None:
    adaylar = [(i, a, b) for i, k in enumerate(kelimeler) if len(k) >= 4
               for a, b in _CEVIRI if a in k[1:]]
    if not adaylar:
        return None
    i, a, b = rnd.choice(adaylar)
    k = kelimeler[i]
    yer = k.index(a, 1)
    return kelimeler[:i] + [k[:yer] + b + k[yer + len(a):]] + kelimeler[i + 1:]


def yazim_hatasi(kelimeler: list[str], rnd: random.Random) -> list[str] | None:
    uygun = [i for i, k in enumerate(kelimeler) if len(k) >= 5]
    if not uygun:
        return None
    i = rnd.choice(uygun)
    k = list(kelimeler[i])
    j = rnd.randrange(1, len(k) - 1)
    tur = rnd.choice(["sil", "degistir", "yer_degistir"])
    if tur == "sil":
        del k[j]
    elif tur == "degistir":
        k[j] = rnd.choice([c for c in "AEIOUNRST" if c != k[j]])
    else:
        k[j], k[j + 1] = k[j + 1], k[j]
    return kelimeler[:i] + ["".join(k)] + kelimeler[i + 1:]


def boz(kelimeler: list[str], tur: str, rnd: random.Random) -> list[str] | None:
    if tur == "harf_cevirisi":
        return harf_cevirisi(kelimeler, rnd)
    if tur == "kelime_sirasi":
        return kelimeler[::-1] if len(kelimeler) >= 2 else None
    if tur == "gobek_adi":
        return [kelimeler[0], kelimeler[-1]] if len(kelimeler) >= 3 else None
    if tur == "yazim_hatasi":
        return yazim_hatasi(kelimeler, rnd)
    return list(kelimeler)  # dogum_sil: ad aynı, doğum verilmez


def psc_dogum(dogumlar: list[str]) -> str | None:
    """Listedeki ilk doğum tarihini PSC hassasiyetine (yıl-ay) indirger."""
    for d in dogumlar:
        m = re.match(r"^(\d{4})(?:-(\d{2}))?", d)
        if m:
            return f"{m.group(1)}-{m.group(2)}" if m.group(2) else m.group(1)
    return None


# --------------------------------------------------------------------------- skorlama (işçi)
_IX: TaramaIndeksi | None = None


def _isci_baslat():
    global _IX
    _IX = TaramaIndeksi()


def tum_skorlar(sorgu: dict) -> dict:
    """Bir sorgu için tüm adayların skorları (eşikten bağımsız: tablo sonradan çıkarılır)."""
    q = tarama.Sorgu(**{k: sorgu[k] for k in ("ad", "tip", "dogum", "uyruk")})
    kelimeler = tarama.tokens(q.ad, q.tip)
    idler = _IX.aday_idleri(kelimeler, q.tip) if kelimeler else []
    adaylar = _IX.kayitlar(idler)
    skorlar = [tarama.skorla(q, k, kelimeler) for k in adaylar]
    hedef = sorgu.get("hedef")
    hedef_skor = next((s for s in skorlar if s["candidate_id"] == hedef), None)
    en_iyi = sorted(skorlar, key=lambda s: s["score"], reverse=True)[:5]
    return {**sorgu, "aday_sayisi": len(adaylar),
            "hedef_adaylarda": hedef_skor is not None if hedef else None,
            "hedef_skor": hedef_skor["score"] if hedef_skor else None,
            "hedef_isim_skor": hedef_skor["name_score"] if hedef_skor else None,
            "hedef_gerekce": hedef_skor["gerekce"] if hedef_skor else None,
            "en_iyi": [{k: s[k] for k in ("candidate_id", "candidate_name", "list", "score",
                                          "name_score", "gerekce")} for s in en_iyi]}


# --------------------------------------------------------------------------- setler
def pozitif_set(ix: TaramaIndeksi, n: int, rnd: random.Random) -> list[dict]:
    satirlar = ix.db.execute(
        "SELECT os_id, ad, isimler, dogum FROM kayit WHERE tip='kisi' AND liste='yaptirim'"
    ).fetchall()
    rnd.shuffle(satirlar)
    out = []
    for os_id, ad, isimler, dogum in satirlar:
        tur = BOZMALAR[len(out) % len(BOZMALAR)]
        kelimeler = json.loads(isimler)[0]  # birincil adın normalize hali
        if len(kelimeler) < 2:
            continue
        bozuk = boz(kelimeler, tur, rnd)
        if bozuk is None:
            continue
        out.append({"set": "pozitif", "bozma": tur, "hedef": os_id, "orijinal": ad,
                    "ad": " ".join(bozuk), "tip": "kisi", "uyruk": None,
                    "dogum": None if tur == "dogum_sil" else psc_dogum(dogum.split(";"))})
        if len(out) >= n:
            break
    return out


def zor_negatif_set(pozitif: list[dict], rnd: random.Random) -> list[dict]:
    out = []
    for p in pozitif:
        if not p["dogum"] and p["bozma"] != "dogum_sil":
            continue
        yil = int((p["dogum"] or "")[:4] or 0)
        if not yil:
            continue
        kayma = rnd.choice([-1, 1]) * rnd.randint(10, 30)
        out.append({"set": "zor_negatif", "hedef": p["hedef"], "orijinal": p["orijinal"],
                    "ad": p["orijinal"], "tip": "kisi", "uyruk": None,
                    "dogum": f"{yil + kayma}-{rnd.randint(1, 12):02d}", "kayma": kayma})
    return out


def negatif_set(n: int, rnd: random.Random) -> list[dict]:
    kisiler = [k for k in read_jsonl(PROCESSED / "persons.jsonl") if k.get("name")]
    rnd.shuffle(kisiler)
    out = []
    for k in kisiler[:n]:
        dogum = (f"{k['birth_year']}-{k['birth_month']:02d}"
                 if k.get("birth_year") and k.get("birth_month") else None)
        out.append({"set": "negatif", "hedef": None, "psc_key": k.get("psc_key"),
                    "ad": k["name"], "tip": "kisi", "dogum": dogum,
                    "uyruk": k.get("nationality")})
    return out


# --------------------------------------------------------------------------- tablo
def tablo(sonuclar: list[dict]) -> list[dict]:
    pos = [r for r in sonuclar if r["set"] == "pozitif"]
    neg = [r for r in sonuclar if r["set"] == "negatif"]
    zor = [r for r in sonuclar if r["set"] == "zor_negatif"]
    satirlar = []
    for e in ESIKLER:
        satir = {"esik": e,
                 "duyarlilik": sum((r["hedef_skor"] or 0) >= e for r in pos) / max(len(pos), 1)}
        for tur in BOZMALAR:
            alt = [r for r in pos if r["bozma"] == tur]
            satir[f"duy_{tur}"] = sum((r["hedef_skor"] or 0) >= e for r in alt) / max(len(alt), 1)
        satir["yp_orani"] = sum(any(s["score"] >= e for s in r["en_iyi"]) for r in neg) / max(len(neg), 1)
        for liste in ("yaptirim", "pep", "diger"):
            satir[f"yp_{liste}"] = sum(any(s["score"] >= e and s["list"] == liste
                                           for s in r["en_iyi"]) for r in neg) / max(len(neg), 1)
        satir["zor_neg_bastirma"] = sum((r["hedef_skor"] or 0) < e for r in zor) / max(len(zor), 1)
        satirlar.append(satir)
    return satirlar


def md_tablo(satirlar: list[dict]) -> str:
    kolon = ["esik", "duyarlilik", *[f"duy_{t}" for t in BOZMALAR], "yp_orani", "yp_yaptirim",
             "yp_pep", "yp_diger", "zor_neg_bastirma"]
    out = ["| " + " | ".join(kolon) + " |", "|" + "---|" * len(kolon)]
    for s in satirlar:
        out.append("| " + " | ".join(f"{s[k]:.2f}" if k == "esik" else f"{s[k]:.1%}"
                                     for k in kolon) + " |")
    return "\n".join(out)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--pozitif", type=int, default=200)
    ap.add_argument("--negatif", type=int, default=2000)
    ap.add_argument("--isci", type=int, default=6)
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()

    rnd = random.Random(a.seed)
    ix = TaramaIndeksi()
    pozitif = pozitif_set(ix, a.pozitif, rnd)
    sorgular = [*pozitif, *zor_negatif_set(pozitif, rnd), *negatif_set(a.negatif, rnd)]
    print(f"{len(sorgular)} sorgu ({len(pozitif)} pozitif), {a.isci} işçi")

    t0 = time.time()
    with ProcessPoolExecutor(a.isci, initializer=_isci_baslat) as havuz:
        sonuclar = list(havuz.map(tum_skorlar, sorgular, chunksize=20))
    sure = time.time() - t0

    satirlar = tablo(sonuclar)
    pos = [r for r in sonuclar if r["set"] == "pozitif"]
    kosu = RUNS_DIR / f"tarama_{datetime.now():%Y%m%d_%H%M%S}"
    kosu.mkdir(parents=True, exist_ok=True)
    ozet = {
        "zaman": datetime.now().isoformat(timespec="seconds"),
        "parametreler": vars(a),
        "indeks": {k: ix.meta.get(k) for k in ("kurulum", "kayit", "surum")},
        "skor": {"varsayilan_esik": tarama.ESIK, "dogum_uyusur": tarama.DOGUM_UYUSUR,
                 "dogum_celisir": tarama.DOGUM_CELISIR, "fonetik_esit": tarama.FONETIK_ESIT},
        "saniye": round(sure, 1),
        "sorgu_basina_ms": round(1000 * sure * a.isci / max(len(sonuclar), 1), 1),
        "set_boyutlari": {s: sum(r["set"] == s for r in sonuclar)
                          for s in ("pozitif", "zor_negatif", "negatif")},
        "daraltma_duyarliligi": sum(bool(r["hedef_adaylarda"]) for r in pos) / max(len(pos), 1),
        "tablo": satirlar,
    }
    (kosu / "ozet.json").write_text(json.dumps(ozet, ensure_ascii=False, indent=2), "utf-8")
    (kosu / "tablo.md").write_text(md_tablo(satirlar) + "\n", "utf-8")
    for ad, filtre in [("pozitif", lambda r: r["set"] == "pozitif"),
                       ("zor_negatif", lambda r: r["set"] == "zor_negatif"),
                       ("alarmlar_negatif", lambda r: r["set"] == "negatif"
                        and any(s["score"] >= ESIKLER[0] for s in r["en_iyi"]))]:
        with open(kosu / f"{ad}.jsonl", "w", encoding="utf-8") as fh:
            for r in sorted(filter(filtre, sonuclar),
                            key=lambda r: -max([s["score"] for s in r["en_iyi"]] or [0])):
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\nDaraltma duyarlılığı: {ozet['daraltma_duyarliligi']:.1%} "
          f"({sure:.0f} sn, ~{ozet['sorgu_basina_ms']} ms/sorgu)\n")
    print(md_tablo(satirlar))
    print(f"\n-> {kosu}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
