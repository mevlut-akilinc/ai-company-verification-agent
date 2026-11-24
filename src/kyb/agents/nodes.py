"""Graph nodes.

The document agent is real (LLM extraction + deterministic grounding and share
ratio checks). The other agent nodes are still stubs returning fixed,
clearly-marked outputs with the final output *shape*, so the whole flow runs
end to end. Each stub is replaced by the real implementation later.

The risk scorer is already real: it is plain deterministic code, because the
design rule is "the LLM never decides; code computes the numbers; a human
makes the final call".
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone

from langgraph.types import interrupt

from kyb import er
from kyb import ubo as ubo_hesap
from kyb.agents.state import KYBState, audit_entry
from kyb.belge import belge_adi, belge_metni
from kyb.config import UBO_THRESHOLD_PCT
from kyb.extractor import MODEL, ExtractionError, extract_company
from kyb.graph.store import get_store
from kyb.grounding import ground
from kyb.oranlar import hesapla_oranlar

VALID_ACTIONS = {"approve", "reject", "request_changes"}
DOCUMENT_ACTIONS = {"approve", "reject"}


# --------------------------------------------------------------------------- document agent
def _belge_hatasi(kod: str, mesaj: str, *, belge: str | None, llm_used: bool, **detay) -> dict:
    """Hata yolu: akış durmaz. extraction None kalmamalı, yoksa supervisor bu düğüme
    geri döner ve sonsuz döngü olur; status='hata' ile 'çalıştı ama sonuç yok' denir."""
    hata = {"kod": kod, "mesaj": mesaj, "belge": belge, **detay}
    return {
        "extraction": {"status": "hata", "belge": belge, "fields": None},
        "grounding": None,
        "oran_analizi": None,
        "extraction_error": hata,
        "insan_incelemesi_gerekli": True,
        "inceleme_nedenleri": [kod],
        "audit": [audit_entry("document_agent", f"{kod}: {mesaj[:120]}", llm_used=llm_used)],
    }


def document_agent(state: KYBState) -> dict:
    """belge metni -> extract_company (LLM) -> ground -> hesapla_oranlar.

    LLM sadece çıkarım yapar; grounding ve oranlar deterministik koddur. Düğüm hiçbir
    değeri düzeltmez; şüpheli her şeyi işaretleyip vakayı analiste yönlendirir.
    """
    belgeler = state.get("documents") or []
    if not belgeler:
        # Belge isteğe bağlı: vaka şirket numarasıyla başlar, ana kaynak sicil grafıdır.
        # extraction None kalmamalı (supervisor tekrar buraya yönlendirirdi).
        return {
            "extraction": {"status": "atlandi", "belge": None, "fields": None},
            "grounding": None,
            "oran_analizi": None,
            "extraction_error": None,
            "insan_incelemesi_gerekli": False,
            "inceleme_nedenleri": [],
            "audit": [audit_entry("document_agent", "belge verilmedi, atlandı", llm_used=False)],
        }

    nedenler: list[str] = []
    if len(belgeler) > 1:
        # Çoklu belge birleştirme henüz yok; sessizce atlamak yerine analiste söyle.
        nedenler.append("BIRDEN_FAZLA_BELGE")
    belge = belgeler[0]
    ad = belge_adi(belge)

    try:
        metin = belge_metni(belge)
    except Exception as e:  # noqa: BLE001 - bozuk/eksik dosya vakayı çökertmesin
        return _belge_hatasi("BELGE_OKUNAMADI", f"{type(e).__name__}: {e}", belge=ad, llm_used=False)
    if not metin.strip():
        return _belge_hatasi("BELGE_BOS", "Belgeden metin çıkmadı (taranmış PDF olabilir).",
                             belge=ad, llm_used=False)

    try:
        sonuc, meta = extract_company(metin)
    except ExtractionError as e:
        return _belge_hatasi("CIKARIM_HATASI", str(e), belge=ad, llm_used=True, ham_cikti=e.raw,
                             meta={k: v for k, v in e.meta.items() if k != "raw"})
    except Exception as e:  # noqa: BLE001 - ör. Ollama kapalı
        return _belge_hatasi("LLM_CAGRISI_BASARISIZ", f"{type(e).__name__}: {e}", belge=ad,
                             llm_used=True)

    rapor = ground(sonuc, metin)
    analiz = hesapla_oranlar(sonuc)

    if rapor.kalan_sayisi:
        nedenler.append("GROUNDING_KALDI")
    if rapor.kismi_sayisi:
        nedenler.append("GROUNDING_KISMI")
    # Şirket ve ortak düzeyindeki oran uyarıları; tekrarsız, ilk görülme sırasıyla.
    for kod in [*analiz.uyarilar, *(u for o in analiz.ortaklar for u in o.uyarilar)]:
        if kod not in nedenler:
            nedenler.append(kod)

    gerekli = bool(nedenler)
    ozet = (f"{len(sonuc.ortaklar)} ortak; grounding kaldı {rapor.kalan_sayisi} "
            f"kısmi {rapor.kismi_sayisi}; uyarı {len(nedenler)}"
            + (" -> insan incelemesi" if gerekli else ""))
    return {
        "extraction": {
            "status": "ok",
            "belge": ad,
            "fields": sonuc.model_dump(),
            "meta": {k: v for k, v in meta.items() if k != "raw"},
        },
        "grounding": {"dogrulandi": rapor.dogrulandi, "kalan_sayisi": rapor.kalan_sayisi,
                      "kismi_sayisi": rapor.kismi_sayisi, **rapor.model_dump()},
        "oran_analizi": analiz.model_dump(),
        "extraction_error": None,
        "insan_incelemesi_gerekli": gerekli,
        "inceleme_nedenleri": nedenler,
        "audit": [audit_entry("document_agent", ozet, llm_used=True, stub=False,
                              model=MODEL, nedenler=nedenler)],
    }


def document_review(state: KYBState) -> dict:
    """Belge aşamasında analist onayı. approve -> akış devam eder, reject -> vaka kapanır.

    interrupt() öncesi kod resume'da yeniden çalışır; yan etkisiz kalmalı.
    """
    grounding = state.get("grounding") or {}
    payload = {
        "stage": "document",
        "case_id": state.get("case_id"),
        "subject": state.get("subject"),
        "nedenler": state.get("inceleme_nedenleri", []),
        "hata": state.get("extraction_error"),
        "extraction": (state.get("extraction") or {}).get("fields"),
        # Analist sadece sorunlu kontrolleri görsün.
        "sorunlu_kontroller": [k for k in grounding.get("kontroller", [])
                               if k["durum"] in ("kaldi", "kismi")],
        "oran_analizi": state.get("oran_analizi"),
        "allowed_actions": sorted(DOCUMENT_ACTIONS),
    }
    decision = interrupt(payload)

    if not isinstance(decision, dict) or decision.get("action") not in DOCUMENT_ACTIONS:
        raise ValueError(f"Geçersiz karar: {decision!r}. action ∈ {sorted(DOCUMENT_ACTIONS)} olmalı.")
    record = {
        "stage": "document",
        "action": decision["action"],
        "reviewer": decision.get("reviewer", "analyst"),
        "note": decision.get("note", ""),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "nedenler": state.get("inceleme_nedenleri", []),
    }
    update = {
        "document_review": record,
        "review_history": [record],
        "audit": [audit_entry("document_review", f"belge {record['action']} ({record['reviewer']})",
                              llm_used=False, decision=record["action"])],
    }
    if record["action"] == "reject":
        update["review"] = record  # supervisor -> END
    return update


# --------------------------------------------------------------------------- agents (stubs)


# --------------------------------------------------------------------------- entity resolution
def entity_resolution_agent(state: KYBState) -> dict:
    """Konu şirketi sicil grafına bağlar, LEI'sini ve zincirdeki çözümsüz tüzel sahipleri eşler.

    Deterministik (kyb.er): LLM yok. Belirsiz eşleşmelerde seçim yapılmaz, adaylar raporlanır.
    Graf erişilemezse akış durmaz; status='hata' yazılır.
    """
    subj = state.get("subject") or {}
    try:
        store = get_store()
        konu = er.resolve_subject(store, subj.get("company_number"), subj.get("name"))
        lei = sahipler = None
        if konu["karar"] == "eslesti":
            lei = er.link_lei(store, {"name": konu["name"]})
            sahipler = [er.resolve_owner(store, le)
                        for le in store.upstream_legal_entities(konu["company_number"])]
    except Exception as e:  # noqa: BLE001 - ör. Neo4j kapalı
        entities = {"status": "hata", "hata": f"{type(e).__name__}: {e}"}
        return {"entities": entities,
                "audit": [audit_entry("entity_resolution_agent",
                                      f"graf erişilemedi: {type(e).__name__}", llm_used=False)]}

    status = {"eslesti": "ok", "belirsiz": "belirsiz", "yok": "bulunamadi"}[konu["karar"]]
    uyarilar = list(konu["uyarilar"])
    if lei and lei["karar"] == "belirsiz":
        uyarilar.append("LEI_BELIRSIZ")
    sahipler = sahipler or []
    eslesen = sum(s["karar"] == "eslesti" for s in sahipler)
    entities = {
        "status": status,
        "subject": konu,
        "lei": lei,
        "owners": sahipler,
        "owners_resolved": eslesen,
        "uyarilar": uyarilar,
        "method": "rules-v1",
    }
    if status == "ok":
        ozet = (f"{konu['company_number']} {konu['name']} ({konu['method']}); "
                f"LEI {lei['karar']}; tüzel sahip {eslesen}/{len(sahipler)} eşleşti")
    else:
        ozet = f"konu şirket {status}"
    if uyarilar:
        ozet += f"; uyarı: {', '.join(uyarilar)}"
    return {"entities": entities,
            "audit": [audit_entry("entity_resolution_agent", ozet, llm_used=False)]}


# --------------------------------------------------------------------------- UBO
def ubo_agent(state: KYBState) -> dict:
    """Sahiplik zincirini graf üzerinde yürür, etkin pay aralıklarını hesaplar (kyb.ubo).

    Konu şirket sicile bağlanamadıysa hesap yapılmaz (status='atlandi'): eksik veriyle
    'faydalanıcı yok' demek yanlış bir risk sinyali üretirdi.
    """
    ent = state.get("entities") or {}
    if ent.get("status") != "ok":
        ubo = {"status": "atlandi", "neden": f"konu şirket sicilde yok (ER: {ent.get('status')})",
               "threshold_pct": UBO_THRESHOLD_PCT, "beneficial_owners": []}
        return {"ubo": ubo,
                "audit": [audit_entry("ubo_agent", "atlandı: konu şirket çözülemedi",
                                      llm_used=False)]}

    konu = ent["subject"]["company_number"]
    # ER'ın UK şirketine bağladığı çözümsüz tüzel sahipler zincirde şirket gibi yürünür.
    cozumlenen = {o["entity_key"]: o["match"]["key"] for o in ent.get("owners") or []
                  if o["karar"] == "eslesti" and o["match"]["label"] == "Company"}
    try:
        store = get_store()
        kenarlar = ubo_hesap.zinciri_cek(store, konu, cozumlenen)
        sirket = store.company(konu) or {}
    except Exception as e:  # noqa: BLE001
        ubo = {"status": "hata", "hata": f"{type(e).__name__}: {e}",
               "threshold_pct": UBO_THRESHOLD_PCT, "beneficial_owners": []}
        return {"ubo": ubo, "audit": [audit_entry("ubo_agent", f"graf hatası: {type(e).__name__}",
                                                  llm_used=False)]}

    sonuc = ubo_hesap.hesapla(konu, kenarlar, UBO_THRESHOLD_PCT, cozumlenen)
    ubo = {"status": "ok", "method": "psc-bands-v1", "edges": len(kenarlar),
           "psc_statements": sirket.get("psc_statements") or [], **sonuc}
    siniflar = Counter(b["sinif"] for b in sonuc["beneficial_owners"])
    ozet = (f"{sonuc['layers']} katman, {len(kenarlar)} kenar; UBO kesin {siniflar['kesin']} "
            f"olası {siniflar['olasi']} kontrol {siniflar['kontrol']}"
            + ("; döngü" if sonuc["has_cycle"] else "")
            + (f"; çözümsüz yabancı {sonuc['unresolved_foreign_entities']}"
               if sonuc["unresolved_foreign_entities"] else "")
            + ("; KESİLDİ" if sonuc["truncated"] else ""))
    return {"ubo": ubo, "audit": [audit_entry("ubo_agent", ozet, llm_used=False)]}


def screening_agent(state: KYBState) -> dict:
    screening = {
        "status": "stub",
        "screened": 3,
        "hits": [
            {"query": "Ayse Yilmaz", "candidate_id": "os-demo-123", "candidate_name": "Ayse Yilmazer",
             "score": 0.82, "list": "demo"},
        ],
        "threshold": 0.9,
    }
    return {"screening": screening,
            "audit": [audit_entry("screening_agent", "1 olası eşleşme (eşik altı)", llm_used=False, stub=True)]}


# --------------------------------------------------------------------------- deterministic risk score
RISK_RULES = [
    # (id, description, points, predicate)
    ("deep_chain", "3 veya daha fazla kurumsal katman",
     20, lambda s: (s.get("ubo") or {}).get("layers", 0) >= 3),
    ("circular", "Döngüsel ortaklık",
     30, lambda s: bool((s.get("ubo") or {}).get("has_cycle"))),
    ("foreign_unresolved", "Çözümlenemeyen yabancı tüzel kişi",
     15, lambda s: (s.get("ubo") or {}).get("unresolved_foreign_entities", 0) > 0),
    ("protected_psc", "Kimliği gizli (super-secure) PSC",
     20, lambda s: (s.get("ubo") or {}).get("protected_pscs", 0) > 0),
    # Sadece UBO hesabı gerçekten yapıldıysa: konu şirket çözülemediyse 'yok' denemez.
    ("no_ubo", "Eşiği aşan gerçek faydalanıcı yok",
     25, lambda s: (s.get("ubo") or {}).get("status") == "ok"
     and not any(b.get("sinif") in ("kesin", "olasi")
                 for b in (s.get("ubo") or {}).get("beneficial_owners", []))),
    ("screening_hit", "Eşik üstü tarama eşleşmesi",
     50, lambda s: any(h["score"] >= (s.get("screening") or {}).get("threshold", 0.9)
                       for h in (s.get("screening") or {}).get("hits", []))),
    ("screening_near", "Eşiğe yakın tarama eşleşmesi (0.75+)",
     10, lambda s: any(0.75 <= h["score"] < (s.get("screening") or {}).get("threshold", 0.9)
                       for h in (s.get("screening") or {}).get("hits", []))),
]
RISK_LEVELS = [(50, "high"), (20, "medium"), (0, "low")]


def risk_scorer(state: KYBState) -> dict:
    fired = [
        {"rule": rid, "description": desc, "points": pts}
        for rid, desc, pts, pred in RISK_RULES
        if pred(state)
    ]
    score = sum(r["points"] for r in fired)
    level = next(lvl for floor, lvl in RISK_LEVELS if score >= floor)
    risk = {"score": score, "level": level, "fired_rules": fired, "model": "rules-v0"}
    return {"risk": risk,
            "audit": [audit_entry("risk_scorer", f"skor {score} ({level})", llm_used=False)]}


# --------------------------------------------------------------------------- report (stub)
def report_agent(state: KYBState) -> dict:
    subj = state.get("subject", {})
    risk = state.get("risk") or {}
    notes = [h.get("note") for h in state.get("review_history", []) if h.get("note")]
    rule_lines = [f"- {r['description']} (+{r['points']})" for r in risk.get("fired_rules", [])]
    ubo = state.get("ubo") or {}
    sinif_adi = {"kesin": "kesin", "olasi": "olası (bant eşiği kapsıyor)",
                 "kontrol": "kontrol yoluyla (yüzde yok)"}
    ubo_lines = []
    for b in ubo.get("beneficial_owners", []):
        satir = f"- {b['name']}: etkin %{b['effective_min']:.1f}–%{b['effective_max']:.1f}"
        if b.get("control_max", 0) > b["effective_max"]:
            satir += f", kontrol zinciriyle %{b['control_min']:.0f}–%{b['control_max']:.0f}"
        satir += f" · {sinif_adi.get(b.get('sinif'), '-')} · {b.get('path_count', 1)} yol"
        if b.get("joint"):
            satir += " · müşterek tutma (yüzdeler toplanamaz)"
        ubo_lines.append(satir)
    if ubo.get("status") == "ok":
        if not ubo_lines:
            ubo_lines.append(f"- Eşiği (%{ubo['threshold_pct']:.0f}) aşan ya da kontrol hakkı "
                             f"olan gerçek kişi bulunamadı ({ubo['persons_seen']} kişi incelendi)")
        ubo_lines += [
            "",
            f"Zincir: {ubo['layers']} kurumsal katman, {ubo['edges']} aktif kontrol kenarı"
            + ("; **döngüsel sahiplik var**" if ubo["has_cycle"] else "")
            + ("; **hesap sınırda kesildi**" if ubo["truncated"] else ""),
            f"Çözümsüz yabancı tüzel sahip: {ubo['unresolved_foreign_entities']}",
            *(f"  - {e.get('name')} ({e.get('country_registered', '?')})"
              for e in ubo.get("foreign_entities", [])[:5]),
        ]
        for t in ubo.get("top_companies", [])[:5]:
            beyan = ", ".join(t["psc_statements"]) or "PSC kaydı/beyanı yok (borsa muafiyeti olabilir)"
            ubo_lines.append(f"Zincir tepesi: {t['company_number']} · {t['name']}: {beyan}")
        if ubo.get("psc_statements"):
            ubo_lines.append(f"Şirketin PSC beyanları: {', '.join(ubo['psc_statements'])}")
    elif ubo:
        ubo_lines.append(f"- UBO hesabı yapılmadı: {ubo.get('neden') or ubo.get('hata')}")
    extraction = state.get("extraction") or {}
    fields = extraction.get("fields") or {}
    doc_review = state.get("document_review")
    if extraction.get("status") == "atlandi":
        belge_lines = ["- Belge verilmedi; vaka sicil verisiyle yürütüldü."]
    else:
        belge_lines = [
            f"- Kaynak: {extraction.get('belge') or '-'} (durum: {extraction.get('status', '-')})",
            f"- Unvan: {fields.get('unvan') or '-'} · Sicil no: {fields.get('sicil_no') or '-'}",
            f"- Ortak sayısı: {len(fields.get('ortaklar') or [])}",
            f"- İnceleme nedenleri: {', '.join(state.get('inceleme_nedenleri') or []) or 'yok'}",
        ]
    if doc_review:
        belge_lines.append(f"- Belge aşaması analist kararı: {doc_review['action']} "
                           f"({doc_review['reviewer']})")
    ent = state.get("entities") or {}
    konu = ent.get("subject") or {}
    if ent.get("status") == "ok":
        baslik = f"{konu['name']} ({konu['company_number']})"
        lei = ent.get("lei") or {}
        lei_satir = (f"{lei['lei']} · {lei['name']} (puan {lei['score']:.2f})"
                     if lei.get("karar") == "eslesti" else
                     f"{lei.get('karar', '-')} ({len(lei.get('candidates') or [])} aday)")
        er_lines = [
            f"- Sicil kaydı: {konu['company_number']} · {konu['name']} "
            f"(yöntem: {konu['method']}, puan {konu['score']:.2f})",
            f"- LEI: {lei_satir}",
            f"- Zincirdeki çözümsüz tüzel sahipler: {len(ent.get('owners') or [])}, "
            f"eşleşen {ent.get('owners_resolved', 0)}",
        ]
    else:
        baslik = f"{subj.get('name', '?')} ({subj.get('company_number', '?')})"
        er_lines = [f"- Konu şirket sicilde bağlanamadı (durum: {ent.get('status', '-')})"
                    + (f": {ent['hata']}" if ent.get("hata") else "")]
        er_lines += [f"  - aday: {a['key']} · {a['name']} ({a['score']:.2f})"
                     for a in konu.get("candidates") or []]
    if ent.get("uyarilar"):
        er_lines.append(f"- Uyarılar: {', '.join(ent['uyarilar'])}")
    lines = [
        f"# KYB Ön Raporu: {baslik}",
        "",
        f"**Risk:** {risk.get('level', '?')} (skor {risk.get('score', '?')})",
        "",
        "## Sicil eşleştirmesi",
        *er_lines,
        "",
        "## Belge çıkarımı",
        *belge_lines,
        "",
        "## Tetiklenen kurallar",
        *(rule_lines or ["- yok"]),
        "",
        "## Gerçek faydalanıcılar",
        *(ubo_lines or ["- tespit edilemedi"]),
    ]
    if notes:
        lines += ["", "## Analist notları (önceki tur)", *[f"- {n}" for n in notes]]
    lines += ["", "_Taslak rapor. Tarama bölümü henüz DEMO verisidir; risk skorundaki "
              "tarama kuralları da demo girdiyle hesaplanır. Nihai karar analiste aittir._"]
    report = {"status": "template", "markdown": "\n".join(lines), "version": state.get("revision_count", 0) + 1}
    return {"report": report,
            "audit": [audit_entry("report_agent", f"rapor v{report['version']} (şablon)",
                                  llm_used=False)]}


# --------------------------------------------------------------------------- human in the loop
def human_review(state: KYBState) -> dict:
    """Pause the graph until an analyst decides. Code before interrupt() re-runs on resume,
    so it must stay side-effect free."""
    payload = {
        "stage": "report",
        "case_id": state.get("case_id"),
        "subject": state.get("subject"),
        "risk": state.get("risk"),
        "report_markdown": (state.get("report") or {}).get("markdown"),
        "allowed_actions": sorted(VALID_ACTIONS),
    }
    decision = interrupt(payload)

    if not isinstance(decision, dict) or decision.get("action") not in VALID_ACTIONS:
        raise ValueError(f"Geçersiz karar: {decision!r}. action ∈ {sorted(VALID_ACTIONS)} olmalı.")
    record = {
        "stage": "report",
        "action": decision["action"],
        "reviewer": decision.get("reviewer", "analyst"),
        "note": decision.get("note", ""),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_version": (state.get("report") or {}).get("version"),
    }
    update = {
        "review_history": [record],
        "audit": [audit_entry("human_review", f"{record['action']} ({record['reviewer']})",
                              llm_used=False, decision=record["action"])],
    }
    if record["action"] == "request_changes":
        # Clear report + review so the supervisor routes back to the report agent.
        update |= {"review": None, "report": None,
                   "revision_count": state.get("revision_count", 0) + 1}
    else:
        update["review"] = record
    return update
