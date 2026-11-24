"""ER ölçümü: Company -> LEI bağlantısı, gold çiftlere karşı.

Alt kümedeki her şirket için link_lei çalışır. Gold: data/eval/er_gold_ch_gleif.jsonl
(GLEIF'in kendi sicil numarası eşlemesi; grafa yüklenmez).

  TP: eşleşti ve gold LEI ile aynı
  FP: eşleşti ama gold yok ya da farklı LEI
  FN: gold var ama doğru LEI ile eşleşmedi (yok / belirsiz / yanlış)
Belirsizler ayrıca sayılır: sistem karar vermedi, analiste gider.

Ayrıca zincirlerdeki LegalEntity sahiplerinin çözümlenme dağılımı (gold yok).

Kullanım:  python scripts/er_olcum.py
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from kyb import er
from kyb.data.readers import read_jsonl
from kyb.graph.store import get_store

KOK = Path(__file__).resolve().parents[1]
PROCESSED = KOK / "data" / "processed"
GOLD = KOK / "data" / "eval" / "er_gold_ch_gleif.jsonl"
RUNS_DIR = KOK / "runs"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    store = get_store()
    gold = {g["company_number"]: g["lei"] for g in read_jsonl(GOLD)}
    # Eski alt kümelerde gold sadece RA000585'ten üretiliyordu; İskoç/K. İrlanda
    # şirketleri eksikti. lei_records'taki sicil numarasından tamamlanır (sadece ölçümde).
    from kyb.config import GLEIF_RA_COMPANIES_HOUSE
    from kyb.normalize import normalize_company_number
    sirket_no = {c["company_number"] for c in read_jsonl(PROCESSED / "companies.jsonl")}
    for r in read_jsonl(PROCESSED / "lei_records.jsonl"):
        n = normalize_company_number(r.get("ra_entity_id"))
        if r.get("ra_id") in GLEIF_RA_COMPANIES_HOUSE and n in sirket_no:
            gold.setdefault(n, r["lei"])
    sirketler = list(read_jsonl(PROCESSED / "companies.jsonl"))

    t0 = time.perf_counter()
    sayac = Counter()
    hatalar = []
    for i, c in enumerate(sirketler, 1):
        r = er.link_lei(store, c)
        beklenen = gold.get(c["company_number"])
        sayac[r["karar"]] += 1
        if r["karar"] == "eslesti" and r["lei"] == beklenen:
            sayac["TP"] += 1
        elif r["karar"] == "eslesti":
            sayac["FP"] += 1
            hatalar.append({"tur": "FP", "sirket": c["name"], "beklenen": beklenen, "gelen": r})
        if beklenen and not (r["karar"] == "eslesti" and r["lei"] == beklenen):
            sayac["FN"] += 1
            if r["karar"] != "eslesti":
                hatalar.append({"tur": "FN", "sirket": c["name"], "beklenen": beklenen, "gelen": r})
        if beklenen and r["karar"] == "belirsiz":
            sayac["gold_belirsiz"] += 1
        if i % 1000 == 0:
            print(f"  {i}/{len(sirketler)}", flush=True)
    sure_lei = time.perf_counter() - t0

    tp, fp, fn = sayac["TP"], sayac["FP"], sayac["FN"]
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    # Zincirdeki çözümsüz tüzel sahipler
    sahip = Counter()
    sahip_ornek = []
    for le in read_jsonl(PROCESSED / "legal_entities.jsonl"):
        r = er.resolve_owner(store, le)
        etiket = r["karar"] + (f":{r['match']['label']}" if r["match"] else "")
        sahip[etiket] += 1
        if r["karar"] != "yok" and len(sahip_ornek) < 30:
            sahip_ornek.append(r)

    ozet = {
        "zaman": datetime.now().isoformat(timespec="seconds"),
        "esikler": {"kabul": er.KABUL_ESIGI, "inceleme": er.INCELEME_ESIGI, "fark": er.FARK_ESIGI},
        "sirket_sayisi": len(sirketler), "gold_cift": len(gold),
        "kararlar": {k: sayac[k] for k in ("eslesti", "belirsiz", "yok")},
        "TP": tp, "FP": fp, "FN": fn, "gold_belirsiz": sayac["gold_belirsiz"],
        "precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
        "sure_sn": round(sure_lei, 1),
        "sahip_cozumleme": dict(sahip),
    }
    kosu = RUNS_DIR / f"er_{datetime.now():%Y-%m-%d_%H%M%S}"
    kosu.mkdir(parents=True)
    (kosu / "ozet.json").write_text(json.dumps(ozet, ensure_ascii=False, indent=2), "utf-8")
    (kosu / "hatalar.json").write_text(json.dumps(hatalar, ensure_ascii=False, indent=2), "utf-8")
    (kosu / "sahip_ornekleri.json").write_text(json.dumps(sahip_ornek, ensure_ascii=False, indent=2),
                                               "utf-8")
    print(json.dumps(ozet, ensure_ascii=False, indent=2))
    print(f"Kayıt: {kosu.relative_to(KOK)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
