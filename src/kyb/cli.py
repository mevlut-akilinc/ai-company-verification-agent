"""Command line entry point:  kyb <command> --help"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from kyb.config import Paths


def cmd_download(a):
    from kyb.data import download

    sources = a.sources or list(download.ALL_SOURCES)
    out = download.run(Paths().ensure().raw, sources, download.parse_parts(a.psc_parts), a.os_collection)
    for k, files in out.items():
        print(f"{k}: {[f.name for f in files]}")


def cmd_subset(a):
    from kyb.data.subset import SubsetParams, build_subset

    params = SubsetParams(target=a.target, seed=a.seed, complex_ratio=a.complex_ratio)
    manifest = build_subset(Paths(), params)
    print(json.dumps({k: manifest[k] for k in ("selection", "counts", "seconds")}, indent=2))


def cmd_load(a):
    from kyb.graph.load_neo4j import CHECK_QUERIES, Neo4jSink, load_all

    sink = Neo4jSink()
    try:
        counts = load_all(sink, Paths().processed, batch=a.batch, wipe=a.wipe)
        print(json.dumps(counts, indent=2))
        for name, q in CHECK_QUERIES.items():
            print(f"\n## {name}")
            for row in sink.query(q):
                print(row)
    finally:
        sink.close()


def cmd_tarama_indeks(a):
    from kyb.data import tarama_indeks

    dosyalar = [Path(a.kaynak)] if a.kaynak else tarama_indeks.kaynak_dosyalar()
    if not dosyalar:
        sys.exit("OpenSanctions CSV bulunamadı: önce 'kyb download --sources opensanctions'.")
    meta = tarama_indeks.kur(dosyalar, Path(a.hedef) if a.hedef else None)
    print(json.dumps({k: meta[k] for k in ("saniye", "kayit", "anahtar", "sayac")}, indent=2,
                     ensure_ascii=False))


def cmd_tara(a):
    from kyb import tarama
    from kyb.data.tarama_indeks import TaramaIndeksi

    sys.stdout.reconfigure(encoding="utf-8")
    ix = TaramaIndeksi()
    sonuc = ix.tara(tarama.Sorgu(ad=a.ad, tip=a.tip, dogum=a.dogum, uyruk=a.uyruk), a.esik)
    print(f"{sonuc['sonuc']} ({sonuc['aday_sayisi']} aday)")
    for h in sonuc["alarmlar"]:
        print(f"  ALARM {h['score']:.3f} [{h['list']}] {h['candidate_name']} ({h['candidate_id']})")
        print(f"        {h['gerekce']}")
    for h in sonuc["bastirilan"]:
        print(f"  bastırıldı {h['score']:.3f} [{h['list']}] {h['candidate_name']}: {h['gerekce']}")
    if sonuc["en_yakin"]:
        h = sonuc["en_yakin"]
        print(f"  en yakın {h['score']:.3f} {h['candidate_name']}: {h['gerekce']}")


def _print_document_review(p: dict) -> None:
    print("\n=== BELGE İNCELEMESİ GEREKLİ ===")
    print("Nedenler:", ", ".join(p["nedenler"]) or "-")
    if p.get("hata"):
        print(f"Hata: {p['hata']['kod']}: {p['hata']['mesaj']}")
    for k in p.get("sorunlu_kontroller", []):
        print(f"  [{k['durum']}] {k['alan']} = {k['deger']!r}  {k['not_']}")
    oa = p.get("oran_analizi") or {}
    for o in oa.get("ortaklar", []):
        oran = f"%{o['nihai_oran']:.2f}" if o["nihai_oran"] is not None else "-"
        print(f"  ortak {o['ad']}: {oran} ({o['kaynak']}) {', '.join(o['uyarilar'])}")
    if oa.get("uyarilar"):
        print("  şirket uyarıları:", ", ".join(oa["uyarilar"]))
    print()


def _print_entity_review(p: dict) -> None:
    print("\n=== SİCİL EŞLEŞTİRMESİ İNCELEMESİ ===")
    print(f"Aranan: {p['subject']} · ER sonucu: {p['durum']}")
    for a in p["adaylar"]:
        print(f"  aday {a['key']} · {a['name']} ({a['score']:.2f})")
    if p["uyarilar"]:
        print("Uyarılar:", ", ".join(p["uyarilar"]))
    print("select: adaylardan birini seç · enter_number: şirket numarası gir · reject: vakayı kapat\n")


def _print_screening_review(p: dict) -> None:
    print(f"\n=== TARAMA ALARMLARI (eşik {p['esik']:.2f}) ===")
    for h in p["alarmlar"]:
        print(f"[{h['alarm_id']}] {h['query']} ({h['rol']}) ~ {h['candidate_name']} "
              f"[{h['list']}] skor {h['score']:.2f}")
        print(f"     {h['gerekce']}")
        if h.get("programs"):
            print(f"     programlar: {'; '.join(h['programs'][:2])}")
    if p["bastirilan"]:
        print(f"({len(p['bastirilan'])} aday doğum çelişkisiyle bastırıldı, raporda listeli)")
    print()


def _ask_decision(p: dict) -> dict:
    stage = p.get("stage")
    if stage == "screening":
        kararlar = {}
        for h in p["alarmlar"]:
            c = input(f"{h['alarm_id']} gerçek eşleşme mi? [g=gerçek / y=yanlış alarm]: ")
            kararlar[h["alarm_id"]] = {"g": "gercek", "y": "yanlis"}.get(c.strip().lower(), c)
        return {"action": "kaydet", "kararlar": kararlar, "note": input("Not: ").strip(),
                "reviewer": "cli"}
    action = input(f"Karar [{'/'.join(p['allowed_actions'])}]: ").strip()
    decision = {"action": action, "reviewer": "cli"}
    if stage == "entity" and action in ("select", "enter_number"):
        decision["company_number"] = input("Şirket numarası: ").strip()
    decision["note"] = input("Not: ").strip()
    return decision


def cmd_case(a):
    from kyb.agents.graph import build_graph, case_state, pending_review, resume_case, start_case

    sys.stdout.reconfigure(encoding="utf-8")  # Windows konsolunda Türkçe
    g = build_graph()
    case_id = f"case-{uuid.uuid4().hex[:8]}"
    documents = [{"path": str(Path(a.belge).resolve())}] if a.belge else []
    start_case(g, case_id, {"company_number": a.company_number, "name": a.name}, documents)
    while (payload := pending_review(g, case_id)) is not None:
        stage = payload.get("stage")
        if stage == "document":
            _print_document_review(payload)
        elif stage == "entity":
            _print_entity_review(payload)
        elif stage == "screening":
            _print_screening_review(payload)
        else:
            print("\n" + payload["report_markdown"] + "\n")
        decision = _ask_decision(payload)
        try:
            resume_case(g, case_id, decision)
        except ValueError as e:
            print(e)
    state = case_state(g, case_id)
    review = state.get("review")
    print(f"\nSonuç: {review['stage']} aşamasında {review['action']}" if review
          else "\nSonuç: karar yok (revizyon sınırı)")
    print("\nDenetim izi:")
    for e in state["audit"]:
        print(f"  {e['at']}  {e['node']:<24} {e['summary']}{'  [stub]' if e['stub'] else ''}")


def cmd_ui(a):
    app = Path(__file__).parent / "ui" / "app.py"
    sys.exit(subprocess.call([sys.executable, "-m", "streamlit", "run", str(app)]))


def main(argv=None):
    p = argparse.ArgumentParser(prog="kyb")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="Ham bulk verileri indir")
    d.add_argument("--sources", nargs="*", choices=["ch_psc", "ch_basic", "gleif_lei", "gleif_rr", "opensanctions"])
    d.add_argument("--psc-parts", help="Tek dosya yerine PSC parçaları, ör. '1-5'")
    d.add_argument("--os-collection", default="default", help="OpenSanctions koleksiyonu (default / sanctions)")
    d.set_defaults(func=cmd_download)

    s = sub.add_parser("subset", help="Zincir odaklı geliştirme alt kümesi oluştur")
    s.add_argument("--target", type=int, default=10_000)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--complex-ratio", type=float, default=0.6)
    s.set_defaults(func=cmd_subset)

    l = sub.add_parser("load", help="Alt kümeyi Neo4j'ye yükle")
    l.add_argument("--batch", type=int, default=2000)
    l.add_argument("--wipe", action="store_true", help="Önce veritabanını boşalt")
    l.set_defaults(func=cmd_load)

    ti = sub.add_parser("tarama-indeks", help="OpenSanctions CSV'sinden tarama indeksi kur (bir kerelik)")
    ti.add_argument("--kaynak", help="CSV yolu (varsayılan: data/raw/opensanctions/*.csv)")
    ti.add_argument("--hedef", help="SQLite yolu (varsayılan: data/processed/tarama_indeks.sqlite)")
    ti.set_defaults(func=cmd_tarama_indeks)

    tr = sub.add_parser("tara", help="Tek bir ismi indekste tara")
    tr.add_argument("ad")
    tr.add_argument("--tip", choices=["kisi", "sirket"], default="kisi")
    tr.add_argument("--dogum", help="YYYY-MM ya da YYYY")
    tr.add_argument("--uyruk", help="PSC biçimi, ör. British")
    tr.add_argument("--esik", type=float, default=0.90)
    tr.set_defaults(func=cmd_tara)

    c = sub.add_parser("case",help="Terminalden tek vaka çalıştır (onay dahil)")
    c.add_argument("--company-number", default="00002065", help="UK şirket numarası")
    c.add_argument("--name", default="", help="Unvan (numara yoksa ER bununla arar)")
    c.add_argument("--belge", help="Belge agent'ına verilecek PDF/metin dosyası")
    c.set_defaults(func=cmd_case)

    u = sub.add_parser("ui", help="Streamlit onay ekranını aç")
    u.set_defaults(func=cmd_ui)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
