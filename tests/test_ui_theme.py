"""Unit tests for ``claudetrade.ui.theme``'s pure formatting helpers."""

from __future__ import annotations

from claudetrade.ui import theme


def test_status_style_known_statuses_have_distinct_colors():
    seen_colors = set()
    for status in ["actionable", "approaching", "extended", "triggered", "expired", "rejected"]:
        icon, color, label = theme.status_style(status)
        assert icon
        assert label
        seen_colors.add(color)
    assert len(seen_colors) == 6


def test_status_style_unknown_falls_back_muted():
    _icon, color, label = theme.status_style("something_new")
    assert color == theme.STATUS_MUTED
    assert label == "Something New"


def test_status_style_none_is_unknown():
    _icon, _color, label = theme.status_style(None)
    assert label == "Unknown"


def test_status_label_includes_icon_and_text():
    label = theme.status_label("actionable")
    assert "Actionable" in label


def test_direction_label_uses_diverging_pair():
    assert "LONG" in theme.direction_label("long")
    assert "SHORT" in theme.direction_label("short")
    assert "FLAT" in theme.direction_label("flat")
    assert "FLAT" in theme.direction_label(None)


def test_direction_color_matches_diverging_pair():
    assert theme.direction_color("long") == theme.LONG_COLOR
    assert theme.direction_color("short") == theme.SHORT_COLOR
    assert theme.direction_color("flat") == theme.NEUTRAL_COLOR
    assert theme.direction_color(None) == theme.NEUTRAL_COLOR


def test_direction_label_case_insensitive():
    assert theme.direction_label("LONG") == theme.direction_label("long")


def test_regime_style_known_and_unknown():
    _icon, label = theme.regime_style("bull_quiet")
    assert "Bull" in label
    _icon2, label2 = theme.regime_style(None)
    assert label2 == "Unknown"


def test_badge_color_maps_semantic_names():
    assert theme.badge_color("good") == "green"
    assert theme.badge_color("critical") == "red"
    assert theme.badge_color("accent") == "blue"
    assert theme.badge_color("nonsense") == "gray"


def test_sequential_blue_is_ordered_light_to_dark_hex():
    assert len(theme.SEQUENTIAL_BLUE) == 7
    assert all(c.startswith("#") and len(c) == 7 for c in theme.SEQUENTIAL_BLUE)
