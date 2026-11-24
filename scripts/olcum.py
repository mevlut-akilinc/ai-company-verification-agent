"""Gold dosyalarına karşı çıkarım ölçümü.

Her gold dosyası için: PDF -> metin (pypdf) -> extract_company -> ground ->
hesapla_oranlar -> gold ile alan alan karşılaştırma. Her koşu
runs/<tarih-saat>/ altına kaydedilir; prompt ya da model değişince eski
koşularla karşılaştırmak için.

Kullanım:  python scripts/olcum.py
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from kyb.belge import pdf_metni
from kyb.extraction import SirketBilgisi
from kyb.extractor import MODEL, SYSTEM_PROMPT, ExtractionError, extract_company
from kyb.grounding import ground
from kyb.oranlar import hesapla_oranlar

KOK = Path(__file__).resolve().parents[1]
GOLD_DIR = KOK / "tests" / "fixtures" / "gold"
BELGE_DIR = KOK / "tests" / "fixtures" / "belgeler"
RUNS_DIR = KOK / "runs"

SAYI_TOLERANSI = 0.01
ORTAK_ALANLARI = ("ad", "tur", "pay_tutari", "pay_orani")  # kanit karşılaştırılmaz


# --------------------------------------------------------------------------- yardımcılar
def turkce_kucuk(metin: str) -> str:
    """Büyük/küçük harften bağımsız karşılaştırma anahtarı.

    Türkçe küçük harf: str.lower() 'I'yı 'i', 'İ'yi 'i' + birleşik nokta yapar.
    Sonra ı -> i: unvanda yabancı kelime olabilir ('NORTHBRIDGE' Türkçe kuralla
    'northbrıdge' olur, model 'Northbridge' yazar). Bedeli: 'Sarı' ile 'Sari'
    eşit sayılır; 'ad' alanı ayrıca birebir karşılaştırıldığı için kabul edilebilir.
    """
    metin = metin.replace("I", "ı").replace("İ", "i").lower().replace("ı", "i")
    return re.sub(r"\s+", " ", metin).strip()


def _ayni_sayi(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return math.isclose(a, b, rel_tol=0, abs_tol=SAYI_TOLERANSI)


def _alan(beklenen, gelen, dogru: bool) -> dict:
    return {"beklenen": beklenen, "gelen": gelen, "dogru": dogru}


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=KOK, capture_output=True, text=True, check=True)
        return r.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _sha(metin: str) -> str:
    return hashlib.sha256(metin.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- karşılaştırma
def karsilastir(beklenen: dict, sonuc: SirketBilgisi) -> dict:
    alanlar = {
        "unvan": _alan(beklenen["unvan"], sonuc.unvan,
                       turkce_kucuk(beklenen["unvan"]) == turkce_kucuk(sonuc.unvan)),
        "sicil_no": _alan(beklenen["sicil_no"], sonuc.sicil_no,
                          beklenen["sicil_no"] == sonuc.sicil_no),
        "sermaye_tutari": _alan(beklenen["sermaye_tutari"], sonuc.sermaye_tutari,
                                _ayni_sayi(beklenen["sermaye_tutari"], sonuc.sermaye_tutari)),
    }

    # Ortaklar sıraya göre değil, Türkçe küçük harfli ada göre eşleşir.
    # 'ad' alanı ise birebir karşılaştırılır (büyük harf aynen kopyalanmalı).
    kullanilan: set[int] = set()
    eksik: list[str] = []
    for go in beklenen["ortaklar"]:
        anahtar = turkce_kucuk(go["ad"])
        idx = next((i for i, o in enumerate(sonuc.ortaklar)
                    if i not in kullanilan and turkce_kucuk(o.ad) == anahtar), None)
        on = f"ortaklar[{go['ad']}]"
        if idx is None:
            eksik.append(go["ad"])
            for a in ORTAK_ALANLARI:  # eksik ortağın alanları yanlış sayılır
                alanlar[f"{on}.{a}"] = _alan(go[a], None, False)
            continue
        kullanilan.add(idx)
        mo = sonuc.ortaklar[idx]
        alanlar[f"{on}.ad"] = _alan(go["ad"], mo.ad, go["ad"] == mo.ad)
        alanlar[f"{on}.tur"] = _alan(go["tur"], mo.tur, go["tur"] == mo.tur)
        for a in ("pay_tutari", "pay_orani"):
            alanlar[f"{on}.{a}"] = _alan(go[a], getattr(mo, a), _ayni_sayi(go[a], getattr(mo, a)))

    # Gold'da olmayan ortaklar (müdür/YK üyesi, ya da aynı kişinin ikinci kaydı).
    fazla = [o.ad for i, o in enumerate(sonuc.ortaklar) if i not in kullanilan]
    alanlar["ortaklar.fazla"] = _alan([], fazla, not fazla)

    return {
        "alanlar": alanlar,
        "eksik_ortaklar": eksik,
        "fazla_ortaklar": fazla,
        "dogru": sum(a["dogru"] for a in alanlar.values()),
        "toplam": len(alanlar),
    }


# --------------------------------------------------------------------------- tek belge
def olc(gold_yolu: Path) -> dict:
    gold = json.loads(gold_yolu.read_text(encoding="utf-8"))
    kayit: dict = {
        "belge": gold["belge"], "gold": gold_yolu.name,
        "metin": None, "sure_sn": None, "meta": None, "ham_cikti": None, "hata": None,
        "sonuc": None, "grounding": None, "oran_analizi": None,
        "uyari_karsilastirma": None, "karsilastirma": None,
    }

    try:
        metin = pdf_metni(BELGE_DIR / gold["belge"])
    except Exception as e:  # noqa: BLE001 - ölçüm tek belge yüzünden durmasın
        kayit["hata"] = f"PDF okunamadı: {type(e).__name__}: {e}"
        return kayit
    kayit["metin"] = metin

    t0 = time.perf_counter()
    try:
        sonuc, meta = extract_company(metin)
    except ExtractionError as e:
        kayit.update(sure_sn=time.perf_counter() - t0, hata=str(e), ham_cikti=e.raw,
                     meta={k: v for k, v in e.meta.items() if k != "raw"})
        return kayit
    except Exception as e:  # noqa: BLE001 - ör. Ollama kapalı
        kayit.update(sure_sn=time.perf_counter() - t0, hata=f"{type(e).__name__}: {e}")
        return kayit
    kayit["sure_sn"] = time.perf_counter() - t0
    kayit["ham_cikti"] = meta["raw"]
    kayit["meta"] = {k: v for k, v in meta.items() if k != "raw"}
    kayit["sonuc"] = sonuc.model_dump()

    rapor = ground(sonuc, metin)
    kayit["grounding"] = {"kalan_sayisi": rapor.kalan_sayisi, "kismi_sayisi": rapor.kismi_sayisi,
                          **rapor.model_dump()}

    analiz = hesapla_oranlar(sonuc)
    kayit["oran_analizi"] = analiz.model_dump()
    beklenen_uyari = sorted(gold["beklenen_sirket_uyarilari"])
    gelen_uyari = sorted(analiz.uyarilar)
    kayit["uyari_karsilastirma"] = _alan(beklenen_uyari, gelen_uyari,
                                         set(beklenen_uyari) == set(gelen_uyari))

    kayit["karsilastirma"] = karsilastir(gold["beklenen"], sonuc)
    return kayit


def _satir(k: dict) -> str:
    ad = k["belge"].removesuffix(".pdf")
    if k["hata"]:
        sure = f"{k['sure_sn']:5.1f}s" if k["sure_sn"] is not None else "   -  "
        return f"{ad:45} {sure}  HATA: {k['hata'][:80]}"
    c, g = k["karsilastirma"], k["grounding"]
    uyari = "✓" if k["uyari_karsilastirma"]["dogru"] else "✗"
    return (f"{ad:45} {k['sure_sn']:5.1f}s  alan {c['dogru']:2}/{c['toplam']:<2}  "
            f"eksik {len(c['eksik_ortaklar'])} fazla {len(c['fazla_ortaklar'])}  "
            f"kaldı {g['kalan_sayisi']} kısmi {g['kismi_sayisi']}  uyarı {uyari}")


# --------------------------------------------------------------------------- ana akış
def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows konsolunda Türkçe ve ✓/✗
    gold_dosyalari = sorted(GOLD_DIR.glob("*.json"))
    if not gold_dosyalari:
        print(f"Gold dosyası yok: {GOLD_DIR}")
        return 1

    zaman = datetime.now()
    kosu_dir = RUNS_DIR / zaman.strftime("%Y-%m-%d_%H%M%S")  # Windows: ':' yasak
    kosu_dir.mkdir(parents=True)
    print(f"Model: {MODEL}   Kayıt: {kosu_dir.relative_to(KOK)}\n")

    kayitlar = []
    for gy in gold_dosyalari:
        k = olc(gy)
        (kosu_dir / f"{Path(k['belge']).stem}.json").write_text(
            json.dumps(k, ensure_ascii=False, indent=2), encoding="utf-8")
        print(_satir(k), flush=True)
        kayitlar.append(k)

    basarili = [k for k in kayitlar if not k["hata"]]
    dogru = sum(k["karsilastirma"]["dogru"] for k in basarili)
    toplam = sum(k["karsilastirma"]["toplam"] for k in basarili)
    uyari_ok = sum(k["uyari_karsilastirma"]["dogru"] for k in basarili)
    sure = sum(k["sure_sn"] or 0 for k in kayitlar)

    ozet = {
        "zaman": zaman.isoformat(timespec="seconds"),
        "model": MODEL,
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_kirli": bool(_git("status", "--porcelain")),  # commit'lenmemiş değişiklik var mı
        "prompt_sha": _sha(SYSTEM_PROMPT),
        "sema_sha": _sha(json.dumps(SirketBilgisi.model_json_schema(), sort_keys=True)),
        "belge_sayisi": len(kayitlar),
        "hata_sayisi": len(kayitlar) - len(basarili),
        "alan_dogru": dogru,
        "alan_toplam": toplam,
        "uyari_dogru": uyari_ok,
        "toplam_sure_sn": sure,
        "belgeler": [_satir(k) for k in kayitlar],
    }
    (kosu_dir / "ozet.json").write_text(json.dumps(ozet, ensure_ascii=False, indent=2),
                                        encoding="utf-8")

    oran = f"{dogru / toplam:.0%}" if toplam else "-"
    print(f"\nAlan doğruluğu: {dogru}/{toplam} ({oran})   "
          f"Uyarı eşleşmesi: {uyari_ok}/{len(basarili)}   "
          f"Hata: {ozet['hata_sayisi']}   Süre: {sure:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
