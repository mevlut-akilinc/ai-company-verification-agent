from pathlib import Path

from streamlit.testing.v1 import AppTest

SUBMIT = "FormSubmitter:decision-Kararı kaydet"
APP = str(Path(__file__).resolve().parents[1] / "src" / "kyb" / "ui" / "app.py")


def test_ui_start_case_and_approve():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Soldan bir vaka" in i.value for i in at.info)

    at.sidebar.text_input[0].set_value("00000001")  # sahte graftaki demo şirket
    at.sidebar.button[0].click().run()  # "Vakayı başlat" (belgesiz -> belge atlanır)
    assert not at.exception
    assert at.metric[0].value == "15"   # sahte graf: yabancı tüzel (+15); tarama temiz

    # Reject without a note must be blocked
    decision_radio = [r for r in at.radio if r.label == "Karar"][0]
    decision_radio.set_value("reject")
    at.button(key=SUBMIT).click().run()
    assert any("not yazmalısın" in e.value for e in at.error)

    decision_radio = [r for r in at.radio if r.label == "Karar"][0]
    decision_radio.set_value("approve")
    at.button(key=SUBMIT).click().run()
    assert not at.exception
    assert any("Onayla" in s.value for s in at.success)


def _baslat(number):
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.sidebar.text_input[0].set_value(number)
    at.sidebar.button[0].click().run()
    assert not at.exception
    return at


def test_ui_er_bulunamadi_numara_girilir():
    at = _baslat("99999999")
    assert any("bulunamadi" in w.value for w in at.warning)
    [r for r in at.radio if r.label == "Karar"][0].set_value("enter_number")
    [t for t in at.text_input if t.label.startswith("Şirket numarası (")][0].set_value("12345678")
    at.button(key="FormSubmitter:entity_decision-Kararı kaydet").click().run()
    assert any("bulunamadı" in e.value for e in at.error)  # grafa gitmeden reddedildi

    [t for t in at.text_input if t.label.startswith("Şirket numarası (")][0].set_value("00000001")
    at.button(key="FormSubmitter:entity_decision-Kararı kaydet").click().run()
    assert not at.exception
    assert any(m.value.startswith("### Rapor") for m in at.markdown)


def test_ui_tarama_alarmi_her_alarm_icin_karar(monkeypatch):
    from test_inceleme import _yaptirimli_store

    from kyb.agents import nodes

    monkeypatch.setattr(nodes, "get_store", _yaptirimli_store)
    at = _baslat("00000001")
    assert any(m.value == "### Tarama alarmları" for m in at.markdown)
    at.button(key="FormSubmitter:screening_decision-Kararları kaydet").click().run()
    assert any("Her alarm için karar ver" in e.value for e in at.error)

    [r for r in at.radio if r.label.startswith("A1")][0].set_value("gercek")
    at.button(key="FormSubmitter:screening_decision-Kararları kaydet").click().run()
    assert not at.exception
    assert any("HIGH" in m.value for m in at.markdown)
