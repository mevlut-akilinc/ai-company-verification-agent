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

doc_stage = (payload or {}).get("stage") == "document"
left, right = st.columns([3, 2])
with left:
    if doc_stage:
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

with right:
    if payload:
        st.markdown("### Belge kararı" if doc_stage else "### Karar")
        with st.form("decision"):
            action = st.radio("Karar", payload["allowed_actions"], format_func=ACTION_LABELS.get)
            reviewer = st.text_input("Analist", value="analist")
            note = st.text_area("Not (Ret ve değişiklik talebinde zorunlu)")
            if st.form_submit_button("Kararı kaydet", type="primary"):
                if action != "approve" and not note.strip():
                    st.error("Bu karar için not yazmalısın.")
                else:
                    resume_case(graph, case_id, {"action": action, "reviewer": reviewer, "note": note})
                    st.rerun()
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
