from zone_classification import classify_bracket


def test_classify_bracket_from_designation_defaults():
    b = classify_bracket(designation="SMR", zone_id=123, name="Example")
    assert b.bracket_class == "NoTake"
    assert b.bracket_source == "designation"

    b2 = classify_bracket(designation="SMCA", zone_id=124, name="Example")
    assert b2.bracket_class == "LimitedTake"
    assert b2.bracket_source == "designation"


def test_classify_bracket_unknown_designation_is_stable():
    b = classify_bracket(designation="WeirdType", zone_id=999, name="Example")
    assert b.bracket_class == "Other:WEIRDTYPE"
    assert b.bracket_source == "unknown"

