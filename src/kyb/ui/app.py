"""Analyst approval screen (Streamlit).

Run:  kyb ui     (or: streamlit run src/kyb/ui/app.py)
"""

from __future__ import annotations

import uuid

import streamlit as st

from kyb.agents.graph import build_graph, case_state, pending_review, resume_case, start_case

ACTION_LABELS = {
    "approve": "Onayla",
    "reject": "Reddet",
    "request_changes": "Değişiklik iste",
    "select": "Adaylardan seç",
    "enter_number": "Numara girildi",
    "kaydet": "Kaydet",
}
LEVEL_COLORS = {"low": "green", "medium": "orange", "high": "red"}

st.set_page_config(page_title="KYB Analist", layout="wide")


@st.cache_resource
def get_graph():
    # One graph + in-memory checkpointer shared across reruns.
    # (Week 3+: swap InMemorySaver for a persistent SQLite/Postgres checkpointer.)
    return build_graph()


graph = get_graph()
if "cases" not in st.session_state:
    st.session_state.cases = []

# ----------------------------------------------------------------- sidebar: new case
with st.sidebar:
    st.header("Yeni vaka")
    with st.form("new_case", clear_on_submit=True):
        number = st.text_input("Şirket numarası", value="00002065")
        name = st.text_input("Unvan", value="")
        belge = st.text_input("Belge yolu (PDF)", value="")
        if st.form_submit_button("Vakayı başlat", type="primary"):
            case_id = f"case-{uuid.uuid4().hex[:8]}"
            documents = [{"path": belge.strip().strip('"')}] if belge.strip() else []
            start_case(graph, case_id, {"company_number": number, "name": name}, documents)
            st.session_state.cases.insert(0, case_id)
            st.session_state.selected = case_id

    st.divider()
    st.header("Vakalar")
    if st.session_state.cases:
        st.session_state.selected = st.radio(
            "Vaka seç", st.session_state.cases,
            index=st.session_state.cases.index(st.session_state.get("selected", st.session_state.cases[0])),
            format_func=lambda c: f"{c} {'⏳' if pending_review(graph, c) else '✅'}",
            label_visibility="collapsed",
        )
    else:
        st.caption("Henüz vaka yok.")

# ----------------------------------------------------------------- main
st.title("KYB Analist Onay Ekranı")
st.caption("LLM karar vermez · rakamları kod hesaplar · son kararı analist verir")

case_id = st.session_state.get("selected")
if not case_id:
    st.info("Soldan bir vaka başlat.")
    st.stop()

values = case_state(graph, case_id)
payload = pending_review(graph, case_id)
subject = values.get("subject", {})
st.subheader(f"{subject.get('name')} · {subject.get('company_number')}  —  `{case_id}`")

risk = values.get("risk") or {}
c1, c2, c3 = st.columns(3)
c1.metric("Risk skoru", risk.get("score", "–"))
level = risk.get("level", "–")
c2.markdown(f"**Seviye**  \n:{LEVEL_COLORS.get(level, 'gray')}[{level.upper()}]")
c3.metric("Rapor sürümü", (values.get("report") or {}).get("version", "–"))

if risk.get("fired_rules"):
    st.markdown("**Tetiklenen kurallar** (deterministik, `rules-v0`)")
    st.dataframe(risk["fired_rules"], hide_index=True, width="stretch")

stage = (payload or {}).get("stage")
doc_stage = stage == "document"
LIST_LABELS = {"yaptirim": "YAPTIRIM", "pep": "PEP", "diger": "Diğer liste"}
left, right = st.columns([3, 2])
with left:
    if stage == "entity":
        st.markdown("### Sicil eşleştirmesi incelemesi")
        durum = {"belirsiz": "birden çok aday birbirine çok yakın",
                 "bulunamadi": "sicilde eşleşen şirket bulunamadı"}.get(payload["durum"], payload["durum"])
        st.warning(f"ER sonucu: **{payload['durum']}** ({durum}). Seçilen şirket UBO hesabının "
                   "girdisi olduğu için akış analist kararını bekliyor.")
        if payload["adaylar"]:
            st.dataframe(payload["adaylar"], hide_index=True, width="stretch")
        if payload["uyarilar"]:
            st.caption("Uyarılar: " + ", ".join(payload["uyarilar"]))
    elif stage == "screening":
        st.markdown("### Tarama alarmları")
        st.warning("Risk skoru bu kararlara dayanıyor: her alarm için ayrı karar ver. "
                   "Sistem bir alarmı kendi kapatmaz.")
        for h in payload["alarmlar"]:
            with st.container(border=True):
                st.markdown(f"**{h['alarm_id']}** · {h['query']} ({h['rol']}) ~ "
                            f"**{h['candidate_name']}** · {LIST_LABELS.get(h['list'], h['list'])} "
                            f"· skor {h['score']:.2f}")
                st.caption(h["gerekce"])
                if h.get("programs"):
                    st.caption("Programlar: " + "; ".join(h["programs"][:3]))
                st.caption(f"Liste kaydı: {h['candidate_id']} · "
                           f"veri setleri: {', '.join(h.get('datasets') or [])}")
        if payload["bastirilan"]:
            with st.expander(f"Doğum çelişkisiyle bastırılan adaylar ({len(payload['bastirilan'])})"):
                st.dataframe([{k: b[k] for k in ("query", "candidate_name", "list", "score",
                                                 "gerekce")} for b in payload["bastirilan"]],
                             hide_index=True, width="stretch")
    elif doc_stage:
        st.markdown("### Belge incelemesi")
        st.warning("İnceleme nedenleri: " + (", ".join(payload["nedenler"]) or "-"))
        if payload.get("hata"):
            st.error(f"{payload['hata']['kod']}: {payload['hata']['mesaj']}")
        if payload.get("sorunlu_kontroller"):
            st.markdown("**Grounding: dayanağı bulunamayan / kısmi değerler**")
            st.dataframe(payload["sorunlu_kontroller"], hide_index=True, width="stretch")
        if payload.get("oran_analizi"):
            st.markdown("**Pay oranları**")
            st.dataframe(payload["oran_analizi"]["ortaklar"], hide_index=True, width="stretch")
        if payload.get("extraction"):
            with st.expander("Çıkarılan alanlar"):
                st.json(payload["extraction"])
    else:
        st.markdown("### Rapor")
        md = (payload or {}).get("report_markdown") or (values.get("report") or {}).get("markdown")
        st.markdown(md or "_Rapor yok_")

def _resume(decision: dict) -> None:
    try:
        resume_case(graph, case_id, decision)
    except ValueError as e:  # karar grafa gitmeden reddedildi; vaka aynı yerde bekler
        st.error(str(e))
    else:
        st.rerun()


with right:
    if stage == "entity":
        st.markdown("### Sicil kararı")
        with st.form("entity_decision"):
            labels = {"select": "Adaylardan seç", "enter_number": "Şirket numarası gir",
                      "reject": "Vakayı kapat"}
            action = st.radio("Karar", payload["allowed_actions"], format_func=labels.get)
            keys = [a["key"] for a in payload["adaylar"]]
            secilen = st.selectbox("Aday", keys, index=None,
                                   format_func=lambda k: next(f"{a['key']} · {a['name']}"
                                                              for a in payload["adaylar"]
                                                              if a["key"] == k)) if keys else None
            numara = st.text_input("Şirket numarası (numara gir seçiliyse)")
            reviewer = st.text_input("Analist", value="analist")
            note = st.text_area("Not (vakayı kapatırken zorunlu)")
            if st.form_submit_button("Kararı kaydet", type="primary"):
                if action == "reject" and not note.strip():
                    st.error("Bu karar için not yazmalısın.")
                else:
                    _resume({"action": action, "reviewer": reviewer, "note": note,
                             "company_number": secilen if action == "select" else numara})
    elif stage == "screening":
        st.markdown("### Alarm kararları")
        with st.form("screening_decision"):
            kararlar = {}
            for h in payload["alarmlar"]:
                kararlar[h["alarm_id"]] = st.radio(
                    f"{h['alarm_id']} · {h['candidate_name']}", ["gercek", "yanlis"], index=None,
                    format_func={"gercek": "Gerçek eşleşme", "yanlis": "Yanlış alarm"}.get,
                    horizontal=True, key=f"alarm-{case_id}-{h['alarm_id']}")
            reviewer = st.text_input("Analist", value="analist")
            note = st.text_area("Not (gerekçe)")
            if st.form_submit_button("Kararları kaydet", type="primary"):
                eksik = [i for i, v in kararlar.items() if v is None]
                if eksik:
                    st.error(f"Her alarm için karar ver: {', '.join(eksik)}")
                else:
                    _resume({"action": "kaydet", "kararlar": kararlar, "reviewer": reviewer,
                             "note": note})
    elif payload:
        if payload.get("acik_konular"):
            st.markdown("### Açık konular")
            for k in payload["acik_konular"]:
                st.warning(k)
        st.markdown("### Belge kararı" if doc_stage else "### Karar")
        with st.form("decision"):
            action = st.radio("Karar", payload["allowed_actions"], format_func=ACTION_LABELS.get)
            reviewer = st.text_input("Analist", value="analist")
            note = st.text_area("Not (Ret ve değişiklik talebinde zorunlu)")
            if st.form_submit_button("Kararı kaydet", type="primary"):
                if action != "approve" and not note.strip():
                    st.error("Bu karar için not yazmalısın.")
                else:
                    _resume({"action": action, "reviewer": reviewer, "note": note})
    else:
        review = values.get("review")
        if review:
            st.success(f"Karar: **{ACTION_LABELS[review['action']]}** · {review['reviewer']} · {review['at']}")
            if review.get("note"):
                st.write(review["note"])
        else:
            st.warning("Vaka karar beklemiyor (revizyon sınırı aşılmış olabilir).")

with st.expander("Denetim izi (audit trail)"):
    st.dataframe(values.get("audit", []), hide_index=True, width="stretch")
with st.expander("Ham durum (debug)"):
    st.json({k: v for k, v in values.items() if k != "audit"})
