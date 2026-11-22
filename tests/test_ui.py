from pathlib import Path

from streamlit.testing.v1 import AppTest

SUBMIT = "FormSubmitter:decision-Kararı kaydet"
APP = str(Path(__file__).resolve().parents[1] / "src" / "kyb" / "ui" / "app.py")


def test_ui_start_case_and_approve():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    assert any("Soldan bir vaka" in i.value for i in at.info)

    at.sidebar.button[0].click().run()  # "Vakayı başlat"
    assert not at.exception
    assert at.metric[0].value == "45"   # stub case -> rules-v0 score

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
