from wowharness.window import dialog_answer

def test_hardware_changed_is_answered_no():
    assert dialog_answer("Hardware changed.  Reload default settings?") == "&No"

def test_unknown_dialog_has_no_answer():
    assert dialog_answer("Something else broke") is None
