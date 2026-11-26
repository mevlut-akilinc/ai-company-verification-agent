"""İnceleme kapısı: belirsiz sonuçları insana yönlendirme.

Kural: analistin kararı sonraki adımın hesabını değiştiriyorsa akış durur
(interrupt), değiştirmiyorsa sadece işaretlenir (rapora ve son incelemeye açık konu).

  aşama      ajan                     ne zaman durur            analist ne yapar
  document   document_agent           grounding/oran sorunu     approve / reject
  entity     entity_resolution_agent  ER belirsiz / bulunamadı  aday seç / numara gir / kapat
  screening  screening_agent          eşik üstü tarama alarmı   her alarm: gerçek / yanlış

UBO kesilmesi akışı durdurmaz: analistin yapabileceği bir hesap değişikliği yok;
risk kuralı tetiklenir ve rapora açık konu olarak düşer.

Her aşama aynı kapıdan geçer (inceleme_kapisi): payload üret -> interrupt ->
kararı doğrula -> state güncellemesi. Kapı resume'da baştan çalışır; interrupt()
öncesindeki kod yan etkisiz kalmalı.

Doğrulama graf ÇALIŞMADAN önce yapılır (graph.resume_case -> dogrula). Düğüm içinde
ValueError atmak yetmez: LangGraph resume değerini saklar, sonraki geçerli kararda
interrupt() yine eski geçersiz değeri döndürür ve vaka kilitlenir.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from langgraph.types import interrupt

from kyb.agents import nodes
from kyb.agents.state import KYBState, audit_entry

# Vakayı kapatan kararlar: review dolar, supervisor END der.
KAPATAN = {"reject"}


@dataclass(frozen=True)
class Asama:
    ad: str
    ajan: str
    dugum: str
    eylemler: frozenset[str]
    gerekli: Callable[[KYBState], bool]
    payload: Callable[[KYBState], dict]
    # (state, decision, record) -> state güncellemesi; record'a ayrıntı ekleyebilir.
    uygula: Callable[[KYBState, dict, dict], dict]
    # (state, decision) -> None; geçersizse ValueError. Yan etkisiz olmalı.
    kontrol: Callable[[KYBState, dict], None] = lambda s, d: None


# --------------------------------------------------------------------------- document
def _belge_payload(state: KYBState) -> dict:
    grounding = state.get("grounding") or {}
    return {
        "nedenler": state.get("inceleme_nedenleri", []),
        "hata": state.get("extraction_error"),
        "extraction": (state.get("extraction") or {}).get("fields"),
        # Analist sadece sorunlu kontrolleri görsün.
        "sorunlu_kontroller": [k for k in grounding.get("kontroller", [])
                               if k["durum"] in ("kaldi", "kismi")],
        "oran_analizi": state.get("oran_analizi"),
    }


def _belge_uygula(state: KYBState, decision: dict, record: dict) -> dict:
    record["nedenler"] = state.get("inceleme_nedenleri", [])
    return {"document_review": record}


# --------------------------------------------------------------------------- entity
def _er_payload(state: KYBState) -> dict:
    ent = state.get("entities") or {}
    konu = ent.get("subject") or {}
    return {
        "durum": ent.get("status"),
        "adaylar": konu.get("candidates") or [],
        "uyarilar": ent.get("uyarilar") or [],
    }


def _er_numara(decision: dict) -> str | None:
    from kyb.normalize import normalize_company_number

    return normalize_company_number(decision.get("company_number"))


def _er_kontrol(state: KYBState, decision: dict) -> None:
    """select: adaylardan biri; enter_number: sicilde var olan bir numara."""
    eylem = decision["action"]
    if eylem == "reject":
        return
    numara = _er_numara(decision)
    if not numara:
        raise ValueError(f"{eylem} için geçerli bir company_number gerekli.")
    konu = (state.get("entities") or {}).get("subject") or {}
    if eylem == "select" and numara not in {a["key"] for a in konu.get("candidates") or []}:
        raise ValueError(f"{numara} aday listesinde yok; listedekilerden birini seç "
                         f"ya da enter_number kullan.")
    if nodes.get_store().company(numara) is None:
        raise ValueError(f"{numara} sicilde (grafta) bulunamadı.")


def _er_uygula(state: KYBState, decision: dict, record: dict) -> dict:
    """ER analistin numarasıyla yeniden çalışır; UBO hesabı seçilen şirketle yapılır."""
    eylem = record["action"]
    if eylem == "reject":
        return {}
    numara = _er_numara(decision)
    entities = nodes.er_sonucu(nodes.get_store(), numara, None)
    entities["subject"]["method"] = f"analist_{eylem}"
    entities["onceki_durum"] = (state.get("entities") or {}).get("status")
    record["company_number"] = numara
    return {"entities": entities}


# --------------------------------------------------------------------------- screening
TARAMA_KARARLARI = {"gercek", "yanlis"}


def _tarama_payload(state: KYBState) -> dict:
    sc = state.get("screening") or {}
    return {
        "alarmlar": sc.get("hits", []),
        "bastirilan": sc.get("suppressed", []),
        "esik": sc.get("threshold"),
        "karar_secenekleri": sorted(TARAMA_KARARLARI),
    }


def _tarama_kontrol(state: KYBState, decision: dict) -> None:
    """Her alarm için tek tek karar zorunlu: {"kararlar": {"A1": "gercek", "A2": "yanlis"}}.
    Toplu 'hepsi yanlış' yok; sistem de analist de alarmı gerekçesiz kapatamaz."""
    kararlar = decision.get("kararlar")
    if not isinstance(kararlar, dict):
        raise ValueError("Tarama kararı eksik: kararlar = {alarm_id: gercek|yanlis} olmalı.")
    idler = [h["alarm_id"] for h in (state.get("screening") or {}).get("hits", [])]
    eksik = [i for i in idler if i not in kararlar]
    fazla = [i for i in kararlar if i not in idler]
    gecersiz = {i: v for i, v in kararlar.items() if v not in TARAMA_KARARLARI}
    if eksik or fazla or gecersiz:
        raise ValueError(f"Tarama kararı eksik/geçersiz: eksik={eksik} fazla={fazla} "
                         f"gecersiz={gecersiz}. Değerler: {sorted(TARAMA_KARARLARI)}")


def _tarama_uygula(state: KYBState, decision: dict, record: dict) -> dict:
    sc = state.get("screening") or {}
    kararlar = decision["kararlar"]
    hits = [{**h, "analist_karari": kararlar[h["alarm_id"]]} for h in sc.get("hits", [])]
    record["kararlar"] = dict(kararlar)
    return {"screening": {**sc, "hits": hits}}


# --------------------------------------------------------------------------- kayıt
ASAMALAR = [
    Asama("document", "document_agent", "document_review", frozenset({"approve", "reject"}),
          lambda s: s.get("insan_incelemesi_gerekli", True), _belge_payload, _belge_uygula),
    Asama("entity", "entity_resolution_agent", "entity_review",
          frozenset({"select", "enter_number", "reject"}),
          lambda s: (s.get("entities") or {}).get("status") in ("belirsiz", "bulunamadi"),
          _er_payload, _er_uygula, _er_kontrol),
    Asama("screening", "screening_agent", "screening_review", frozenset({"kaydet"}),
          lambda s: bool((s.get("screening") or {}).get("hits")), _tarama_payload,
          _tarama_uygula, _tarama_kontrol),
]
ASAMA = {a.ad: a for a in ASAMALAR}


def dogrula(stage: str, state: KYBState, decision) -> None:
    """Kararı grafa göndermeden doğrular (ValueError). 'report' son onay aşamasıdır."""
    eylemler = ASAMA[stage].eylemler if stage in ASAMA else nodes.VALID_ACTIONS
    if not isinstance(decision, dict) or decision.get("action") not in eylemler:
        raise ValueError(f"Geçersiz karar: {decision!r}. action ∈ {sorted(eylemler)} olmalı.")
    if stage in ASAMA:
        ASAMA[stage].kontrol(state, decision)


def inceleme_kapisi(asama: Asama) -> Callable[[KYBState], dict]:
    def dugum(state: KYBState) -> dict:
        payload = {
            "stage": asama.ad,
            "case_id": state.get("case_id"),
            "subject": state.get("subject"),
            **asama.payload(state),
            "allowed_actions": sorted(asama.eylemler),
        }
        decision = interrupt(payload)
        dogrula(asama.ad, state, decision)  # resume_case zaten doğruladı; ikinci emniyet
        record = {
            "stage": asama.ad,
            "action": decision["action"],
            "reviewer": decision.get("reviewer", "analyst"),
            "note": decision.get("note", ""),
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        update = asama.uygula(state, decision, record)
        update |= {
            "incelemeler": {asama.ad: record},
            "review_history": [record],
            "audit": [audit_entry(asama.dugum, f"{asama.ad} {record['action']} "
                                  f"({record['reviewer']})", llm_used=False,
                                  decision=record["action"])],
        }
        if record["action"] in KAPATAN:
            update["review"] = record  # supervisor -> END
        return update

    dugum.__name__ = asama.dugum
    return dugum


def yonlendir(asama: Asama) -> Callable[[KYBState], str]:
    """Ajandan sonraki koşullu kenar: gerekiyorsa inceleme düğümü, değilse supervisor."""
    def kenar(state: KYBState) -> str:
        return asama.dugum if asama.gerekli(state) else "supervisor"

    kenar.__name__ = f"{asama.ad}_sonrasi"
    return kenar
