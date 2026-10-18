from __future__ import annotations

from ravens_bot.nicknames import SHORT_FORMS, given_name_forms, name_variants


def test_a_short_given_name_also_reads_in_full() -> None:
    assert name_variants("Matt Hibner") == ("Matthew Hibner",)
    assert name_variants("Mike Hall Jr.") == ("Michael Hall Jr.",)
    assert name_variants("Pat Ricard") == ("Patrick Ricard",)


def test_a_full_given_name_also_reads_short() -> None:
    assert name_variants("Matthew Hibner") == ("Matt Hibner", "Matty Hibner")
    assert name_variants("Cameron Jurgens") == ("Cam Jurgens",)


def test_names_without_another_common_form_have_no_variants() -> None:
    assert name_variants("Lamar Jackson") == ()
    assert name_variants("K'Von Wallace") == ()
    assert name_variants("Hibner") == ()
    assert name_variants("") == ()


def test_formal_names_that_share_a_short_form_stay_distinct() -> None:
    assert "al" in given_name_forms("Albert")
    assert "alan" not in given_name_forms("Albert")
    assert {"alan", "albert"} <= set(given_name_forms("AL"))


def test_every_form_is_written_the_way_names_are_looked_up() -> None:
    for formal, shorts in SHORT_FORMS.items():
        for name in (formal, *shorts):
            assert name.isalpha() and name == name.lower(), name
        assert formal not in shorts
