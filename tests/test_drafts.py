import pytest

from jobsearch.drafts import BLOCKS, parse_draft


def body(**payload):
    import json
    return json.dumps(payload)


FULL = {"cover_letter": "Hi.", "email": "Subject: x\n\nHi.", "why_fit": "- point"}


def test_a_complete_draft_parses():
    assert parse_draft(body(**FULL)) == FULL


def test_every_block_is_required():
    # A partial draft would render as a card with one empty section, which reads
    # as "written and had nothing to say" rather than "not written".
    for missing in BLOCKS:
        payload = {k: v for k, v in FULL.items() if k != missing}
        with pytest.raises(ValueError, match=missing):
            parse_draft(body(**payload))


def test_an_empty_block_is_rejected_like_a_missing_one():
    with pytest.raises(ValueError, match="cover_letter"):
        parse_draft(body(**{**FULL, "cover_letter": "   "}))


def test_a_non_string_block_is_rejected():
    with pytest.raises(ValueError, match="email"):
        parse_draft(body(**{**FULL, "email": 42}))


def test_unknown_keys_are_rejected_rather_than_silently_dropped():
    with pytest.raises(ValueError, match="unexpected"):
        parse_draft(body(**{**FULL, "linkedin_message": "hi"}))


def test_malformed_json_is_rejected():
    with pytest.raises(ValueError):
        parse_draft("{not json")


def test_an_overlong_block_is_rejected_rather_than_truncated():
    with pytest.raises(ValueError, match="too long"):
        parse_draft(body(**{**FULL, "cover_letter": "x" * 9000}))


# --- rewrite instructions from the dashboard ---

from jobsearch.drafts import MAX_NOTE, parse_note


def test_a_note_parses_and_is_trimmed():
    assert parse_note(body(id=7, note="  shorter please  ")) == (7, "shorter please")


def test_an_empty_note_clears_rather_than_storing_whitespace():
    assert parse_note(body(id=7, note="   ")) == (7, None)
    assert parse_note(body(id=7, note=None)) == (7, None)


def test_a_bad_job_id_is_rejected():
    for bad in ("x", None, True):
        with pytest.raises(ValueError, match="id"):
            parse_note(body(id=bad, note="hi"))


def test_an_overlong_note_is_rejected_rather_than_truncated():
    with pytest.raises(ValueError, match="too long"):
        parse_note(body(id=7, note="x" * (MAX_NOTE + 1)))


def test_a_non_string_note_is_rejected():
    with pytest.raises(ValueError, match="note"):
        parse_note(body(id=7, note=42))
