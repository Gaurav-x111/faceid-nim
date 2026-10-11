"""Anti-spoof absence tests (master audit, section 5E).

A missing or unloadable ML anti-spoof model must degrade loudly, not
silently: available=False with a non-empty reason (the UI and
`faceid-nim status` report that string), and no invented verdict --
spoof_probability() is None, denies() is False. A scan approval with
no model must come from the other cues + strictness, never from this
object claiming anything.
"""
import numpy as np

from faceid_vision.liveness.passive import PassiveAntiSpoof

BOX = (10, 10, 40, 40)


def _frame():
    return np.zeros((80, 80, 3), dtype=np.uint8)


def test_missing_model_reports_and_abstains():
    anti = PassiveAntiSpoof(None)
    assert anti.available is False
    assert anti.reason.strip(), "an absent model must say why"
    assert anti.spoof_probability(_frame(), BOX) is None
    assert anti.denies(_frame(), BOX) is False


def test_unloadable_model_reports_and_abstains(tmp_path):
    bogus = tmp_path / "not-a-model.onnx"
    bogus.write_bytes(b"this is not onnx")
    anti = PassiveAntiSpoof(bogus)
    assert anti.available is False
    assert anti.reason.strip(), "a broken model must say why"
    assert "not-a-model.onnx" in anti.reason
    assert anti.spoof_probability(_frame(), BOX) is None
    assert anti.denies(_frame(), BOX) is False


def test_denies_never_raises_on_garbage_input():
    anti = PassiveAntiSpoof(None)
    assert anti.denies(np.zeros((4, 4, 3), dtype=np.uint8), (0, 0, 2, 2)) is False
