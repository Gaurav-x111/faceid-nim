"""The fusion rules are policy. They get their own tests, because a
regression here is a silent security downgrade."""
from faceid_vision.fuse import LivenessState, Strictness, permits


def s(deny=(), confirm=(), attention=True):
    st = LivenessState(attention_ok=attention)
    st.add_deny(*deny)
    st.add_confirm(*confirm)
    return st


def test_deny_vetoes_every_mode():
    for mode in (Strictness.LIGHT, Strictness.HEAVY):
        ok, why = permits(s(deny=["glare"], confirm=["blink"]), mode)
        assert not ok and "glare" in why


def test_light_allows_missing_confirmation():
    assert permits(s(), Strictness.LIGHT)[0]


def test_heavy_requires_confirmation():
    assert not permits(s(), Strictness.HEAVY)[0]
    assert permits(s(confirm=["blink"]), Strictness.HEAVY)[0]


def test_attention_required_by_default():
    """Stops the laptop being held up to a sleeping user."""
    assert not permits(s(confirm=["blink"], attention=False), Strictness.LIGHT)[0]
    assert permits(s(attention=False), Strictness.LIGHT, require_attention=False)[0]


def test_off_permits_everything_including_denies():
    assert permits(s(deny=["moire"]), Strictness.OFF)[0]


def test_score_is_zero_when_denied():
    assert s(deny=["glare"], confirm=["blink", "parallax"]).score() == 0.0
