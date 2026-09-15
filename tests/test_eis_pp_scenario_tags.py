from __future__ import annotations

from eis_pp_scenario_tags import (
    normalize_participation_intent,
    normalize_scenario_tag,
    participation_intent_label,
    scenario_tag_label,
)


def test_normalize_scenario_and_intent():
    assert normalize_scenario_tag("S1_pre_pp") == "S1_pre_pp"
    assert normalize_scenario_tag("S7_republish") == "S7_republish"
    assert normalize_scenario_tag("unknown") == ""
    assert normalize_participation_intent("would_join") == "would_join"
    assert normalize_participation_intent("maybe") == ""
    assert "ПП до извещения" in scenario_tag_label("S1_pre_pp")
    assert "Переиздание" in scenario_tag_label("S7_republish")
    assert participation_intent_label("would_skip") == "Не участвую"
