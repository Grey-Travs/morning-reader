"""Reading one page image into regions.

The property most of these exist for: **the server's measured dimensions win over the
model's.** The model is estimating from what it was shown; the server read the file's
bytes. A wrong page size rescales every box on the page, silently.

No test here makes a real model call. Japanese fixtures are invented.
"""

from __future__ import annotations

import json

import pytest

from morning import ocr
from morning.pageread import (
    GLUE_NONE, GLUE_SPACE, JOIN_PARAGRAPH, JOIN_SENTENCE, KIND_BODY,
    KIND_PAGE_NUMBER, flatten, to_pixels, validate,
)

COL_A = "電車はまだ来ない。"
COL_B = "彼女はホームに立っていた。"
W, H = 1600, 2400


def _answer(regions=None, meta=None, width=W, height=H) -> str:
    """What the model returns: a JSON object with pixel boxes."""
    return json.dumps({
        "width": width, "height": height,
        "regions": regions if regions is not None else [
            {"box": [1180, 200, 300, 1900], "text": COL_A, "kind": "body",
             "order": 0, "join_prev": "paragraph", "join_glue": "none"},
            {"box": [820, 200, 300, 1900], "text": COL_B, "kind": "body",
             "order": 1, "join_prev": "sentence", "join_glue": "none"},
            {"box": [760, 2280, 80, 60], "text": "12", "kind": "page-number",
             "order": 2, "join_prev": "paragraph", "join_glue": "none"},
        ],
        "meta": meta if meta is not None else {
            "confidence": "high", "heading": None,
            "starts_mid_sentence": False, "ends_mid_sentence": True,
            "ends_mid_word": False, "notes": [],
        },
    }, ensure_ascii=False)


# ---- the happy path ------------------------------------------------------------

def test_a_page_parses_into_regions_with_fractional_boxes():
    page = ocr.parse_page_response(_answer(), width=W, height=H)

    assert validate(page) == []
    assert len(page.regions) == 3
    x, y, w, h = page.regions[0].box
    assert all(0 <= v <= 1 for v in (x, y, w, h)), "boxes are fractions, not pixels"
    assert to_pixels(page.regions[0].box, W, H) == (1180, 200, 300, 1900)


def test_the_regions_flatten_to_the_pages_prose():
    """The novel path, end to end from a model answer."""
    page = ocr.parse_page_response(_answer(), width=W, height=H)

    assert flatten(page) == COL_A + COL_B
    assert "12" not in flatten(page), "the page number is not part of the novel"


def test_the_meta_is_carried():
    page = ocr.parse_page_response(_answer(meta={
        "confidence": "medium", "heading": "第1話",
        "starts_mid_sentence": True, "ends_mid_sentence": False,
        "ends_mid_word": True, "notes": ["the gutter clipped a column"],
    }), width=W, height=H)

    assert page.meta.confidence == "medium"
    assert page.meta.heading == "第1話"
    assert page.meta.starts_mid_sentence is True
    assert page.meta.ends_mid_word is True
    assert page.meta.notes == ["the gutter clipped a column"]


# ---- the dimensions the server measured win ------------------------------------

def test_the_measured_size_beats_the_models_estimate():
    """The whole reason morning/images.py exists. The model is estimating from what it
    was shown; a wrong page size rescales every box on the page, silently."""
    page = ocr.parse_page_response(_answer(width=999, height=999), width=W, height=H)

    assert (page.width, page.height) == (W, H)
    # ...and the box is scaled by the REAL size, so it still lands on the artwork.
    assert to_pixels(page.regions[0].box, W, H) == (1180, 200, 300, 1900)


def test_the_models_size_is_used_only_when_nothing_measured_it():
    """An image whose header could not be read is still worth transcribing."""
    page = ocr.parse_page_response(_answer(width=800, height=1200), width=0, height=0)

    assert (page.width, page.height) == (800, 1200)


def test_a_page_with_no_size_at_all_still_keeps_its_text():
    page = ocr.parse_page_response(_answer(width=0, height=0), width=0, height=0)

    assert page.regions[0].text == COL_A
    assert any("dimensions" in p for p in validate(page)), "and is honest about it"


# ---- tolerant parsing ------------------------------------------------------------

@pytest.mark.parametrize("wrapper", [
    "```json\n{body}\n```",
    "```\n{body}\n```",
    "Here is the page:\n\n{body}",
    "{body}\n\nLet me know if you need anything else.",
    "  {body}  ",
])
def test_a_fenced_or_prefaced_answer_still_parses(wrapper):
    """Taking the outermost braces rather than trusting a bare answer is what makes a
    code fence, a preamble or a trailing remark harmless."""
    page = ocr.parse_page_response(wrapper.format(body=_answer()), width=W, height=H)

    assert len(page.regions) == 3


@pytest.mark.parametrize("raw", [
    "", "   ", "I could not read that image.", "{ truncated", "[]", "null",
])
def test_an_unusable_answer_is_an_error_not_an_empty_page(raw):
    """An empty page looks exactly like a blank one, and would be built into the novel
    as a missing chapter rather than reported as a failure."""
    with pytest.raises(ocr.PageReadError):
        ocr.parse_page_response(raw, width=W, height=H)


def test_an_empty_region_is_dropped():
    """It contributes nothing to the reading and would only make the order a
    non-permutation."""
    page = ocr.parse_page_response(_answer(regions=[
        {"box": [0, 0, 100, 100], "text": COL_A, "order": 0},
        {"box": [0, 0, 100, 100], "text": "   ", "order": 1},
        {"box": [0, 0, 100, 100], "text": COL_B, "order": 2},
    ]), width=W, height=H)

    assert len(page.regions) == 2
    assert validate(page) == [], "and the order is still a permutation"


def test_a_region_with_no_box_keeps_its_text():
    """Losing transcribed text because a coordinate was missing would be the expensive
    failure; losing the coordinate is the cheap one."""
    page = ocr.parse_page_response(_answer(regions=[
        {"text": COL_A, "order": 0},
        {"box": "over there", "text": COL_B, "order": 1},
    ]), width=W, height=H)

    assert [r.text for r in page.regions] == [COL_A, COL_B]
    assert page.regions[0].box == (0.0, 0.0, 0.0, 0.0)


@pytest.mark.parametrize("field,value,expected", [
    ("kind", "speech-bubble", KIND_BODY),
    ("kind", 42, KIND_BODY),
    ("kind", "page-number", KIND_PAGE_NUMBER),
])
def test_an_unknown_kind_falls_back_to_body(field, value, expected):
    page = ocr.parse_page_response(_answer(regions=[
        {"box": [0, 0, 10, 10], "text": COL_A, "order": 0, field: value},
    ]), width=W, height=H)

    assert page.regions[0].kind == expected


def test_an_unknown_seam_or_glue_falls_back():
    page = ocr.parse_page_response(_answer(regions=[
        {"box": [0, 0, 10, 10], "text": COL_A, "order": 0},
        {"box": [0, 0, 10, 10], "text": COL_B, "order": 1,
         "join_prev": "column", "join_glue": "hyphen"},
    ]), width=W, height=H)

    assert page.regions[1].join_prev == ""
    assert page.regions[1].join_glue == GLUE_NONE
    assert validate(page) == []


@pytest.mark.parametrize("given,expected", [
    ("high", "high"), ("HIGH", "high"), ("  medium ", "medium"),
    ("certain", "low"), ("", "low"), (None, "low"), (7, "low"),
])
def test_confidence_is_clamped_to_the_three_levels(given, expected):
    page = ocr.parse_page_response(_answer(meta={"confidence": given}),
                                   width=W, height=H)

    assert page.meta.confidence == expected


def test_booleans_survive_arriving_as_strings():
    """Models answer "true" as often as true, and losing a seam flag to a type
    mismatch breaks page stitching in a way that looks like bad transcription."""
    page = ocr.parse_page_response(_answer(meta={
        "confidence": "high", "ends_mid_sentence": "true",
        "starts_mid_sentence": "false", "notes": "one note",
    }), width=W, height=H)

    assert page.meta.ends_mid_sentence is True
    assert page.meta.starts_mid_sentence is False
    assert page.meta.notes == ["one note"]


# ---- reading order -----------------------------------------------------------------

def test_a_skipped_order_number_is_renumbered_without_changing_the_sequence():
    """A model gets the SEQUENCE right and the bookkeeping wrong. Renumbering keeps
    its judgement — the hard part — and fixes only the counting."""
    page = ocr.parse_page_response(_answer(regions=[
        {"box": [0, 0, 10, 10], "text": "first", "order": 0},
        {"box": [0, 0, 10, 10], "text": "second", "order": 5},
        {"box": [0, 0, 10, 10], "text": "third", "order": 9},
    ]), width=W, height=H)

    assert [r.order for r in page.in_order()] == [0, 1, 2]
    assert [r.text for r in page.in_order()] == ["first", "second", "third"]
    assert validate(page) == []


def test_duplicate_order_numbers_are_resolved_stably():
    page = ocr.parse_page_response(_answer(regions=[
        {"box": [0, 0, 10, 10], "text": "a", "order": 1},
        {"box": [0, 0, 10, 10], "text": "b", "order": 1},
    ]), width=W, height=H)

    assert [r.text for r in page.in_order()] == ["a", "b"]
    assert validate(page) == []


def test_a_missing_order_falls_back_to_list_position():
    page = ocr.parse_page_response(_answer(regions=[
        {"box": [0, 0, 10, 10], "text": "a"},
        {"box": [0, 0, 10, 10], "text": "b"},
    ]), width=W, height=H)

    assert [r.text for r in page.in_order()] == ["a", "b"]


def test_the_prompt_says_reading_order_is_right_to_left():
    """The failure this prevents is invisible unless you read Japanese: a page that is
    backwards, sentence by sentence, because the model defaulted to left-to-right."""
    prompt = ocr.PAGE_SYSTEM_PROMPT

    assert "right-to-left" in prompt.lower() or "RIGHT" in prompt
    assert "vertically" in prompt


def test_the_prompt_asks_for_pixels_and_forbids_inventing_text():
    # Whitespace-normalised, because the prompt is wrapped for reading and a phrase
    # that straddles a line break is not a change in what it says.
    prompt = " ".join(ocr.PAGE_SYSTEM_PROMPT.split())

    assert "PIXELS" in prompt
    assert "never invent" in prompt.lower()
    # The instruction that stops a page being split into one region per LINE, which
    # would make every column read as a series of sentence fragments.
    assert "a line ends because the column ran out" in prompt


def test_the_prompt_lists_every_region_kind_the_contract_knows():
    from morning.pageread import REGION_KINDS

    for kind in REGION_KINDS:
        assert kind in ocr.PAGE_SYSTEM_PROMPT, kind


# ---- the call ------------------------------------------------------------------------

class FakeTranslator:
    def __init__(self, answer: str):
        self.answer = answer
        self.calls: list[dict] = []

    def _call(self, system_text, user_text, max_turns=1, hooks=None, *,
              tools=None, cwd=None, add_dirs=None):
        self.calls.append({"system": system_text, "user": user_text, "tools": tools,
                           "cwd": cwd, "add_dirs": add_dirs})
        return self.answer, {"input_tokens": 100}, 0.03


def test_reading_a_page_scopes_the_read_tool_to_its_own_folder(tmp_path):
    """A call that can open a file can only open the pages of the project it was
    given."""
    image = tmp_path / "pages" / "page-0001.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff")
    translator = FakeTranslator(_answer())

    ocr.read_page(translator, image, width=W, height=H)

    call = translator.calls[0]
    assert call["tools"] == ["Read"]
    assert str(call["cwd"]) == str(image.parent)
    assert [str(d) for d in call["add_dirs"]] == [str(image.parent)]


def test_the_page_read_carries_its_usage_and_cost(tmp_path):
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff")

    page = ocr.read_page(FakeTranslator(_answer()), image, width=W, height=H)

    assert page.usage == {"input_tokens": 100}
    assert page.cost_usd == 0.03


def test_a_read_that_breaks_the_contract_is_kept_but_distrusted(tmp_path):
    """It still holds the text, which is the expensive part. What it has lost is the
    right to be trusted without a human looking."""
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    broken = _answer(regions=[
        {"box": [0, 0, 99999, 99999], "text": COL_A, "order": 0},
    ], meta={"confidence": "high"})

    page = ocr.read_page(FakeTranslator(broken), image, width=W, height=H)

    assert page.regions[0].text == COL_A
    assert page.meta.confidence == "low"
    assert any("not inside the page" in note for note in page.meta.notes)


def test_a_scanning_note_reaches_the_model(tmp_path):
    """The person holding the book knows things the image does not show — that a page
    is cut off, or that two leaves stuck together."""
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    translator = FakeTranslator(_answer())

    ocr.read_page(translator, image, width=W, height=H,
                  hint="the left edge is cut off")

    assert "the left edge is cut off" in translator.calls[0]["user"]


def test_no_note_adds_no_empty_heading(tmp_path):
    image = tmp_path / "page.jpg"
    image.write_bytes(b"\xff\xd8\xff")
    translator = FakeTranslator(_answer())

    ocr.read_page(translator, image, width=W, height=H, hint="   ")

    assert "added a note" not in translator.calls[0]["user"]


def test_only_the_filename_is_sent_not_the_whole_path(tmp_path):
    """The tool is already scoped to the folder, so the model needs the name and
    nothing more — and an absolute path would put the user's directory layout into a
    prompt for no reason."""
    image = tmp_path / "pages" / "page-0001.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8\xff")
    translator = FakeTranslator(_answer())

    ocr.read_page(translator, image, width=W, height=H)

    assert "page-0001.jpg" in translator.calls[0]["user"]
    assert str(tmp_path) not in translator.calls[0]["user"]
