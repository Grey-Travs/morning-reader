"""Tests for the agent driver.

**No test here makes a real model call.** The SDK's ``query`` is replaced with a fake
async generator yielding real SDK message objects, so the control flow under test —
streaming, aborting, retrying, rate limits, truncation — is the real control flow
while costing nothing.

Japanese fixtures are invented for these tests.
"""

from __future__ import annotations

import threading

import pytest
from claude_agent_sdk import (
    AssistantMessage, CLIConnectionError, CLINotFoundError, ProcessError,
    RateLimitEvent, ResultMessage, TextBlock,
)

import morning.translator as translator_mod
from morning.chapters import Chapter
from morning.config import AnthropicConfig, TranslationConfig
from morning.exceptions import RateLimited, TaskAborted
from morning.prompts import NEW_TERMS_DELIMITER
from morning.translator import (
    StreamHooks, Translator, TranslatorError, agent_model, chunk_paragraphs,
    parse_response,
)

JA_A = "電車はまだ来ない。"
EN_A = "The train has not come yet."
LEAK = "彼女は答えなかった。"


# ---- fake SDK ----------------------------------------------------------------

def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)], model="test")


def _result(*, is_error: bool = False, cost: float = 0.0, usage: dict | None = None,
            api_error_status: int | None = None, errors=None) -> ResultMessage:
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="s", total_cost_usd=cost,
        model_usage=usage or {}, api_error_status=api_error_status, errors=errors,
    )


class _Usage(dict):
    """model_usage values are read with .get, so a dict is enough."""


def _fake_query(messages, *, on_call=None):
    """Build a stand-in for the SDK's ``query``.

    ``messages`` is a list of message-lists — one per call — so a test can make the
    first call fail and the second succeed.
    """
    calls = {"n": 0}

    async def fake(prompt, options):
        index = min(calls["n"], len(messages) - 1)
        calls["n"] += 1
        if on_call is not None:
            on_call(prompt, options)
        for message in messages[index]:
            if isinstance(message, BaseException):
                raise message
            yield message

    fake.calls = calls
    return fake


def _translator(**cfg_kw) -> Translator:
    return Translator(AnthropicConfig(**cfg_kw), TranslationConfig())


def _chapter(*paragraphs: str) -> Chapter:
    return Chapter(index=1, title="第1話", paragraphs=list(paragraphs) or [JA_A])


# ---- parsing the response ----------------------------------------------------

def test_prose_and_terms_are_separated():
    raw = (f"{EN_A}\n\n“Late,” he said.\n\n{NEW_TERMS_DELIMITER}\n"
           '[{"source": "佐々木", "english": "Sasaki", "type": "name"}]')
    prose, terms, warnings = parse_response(raw)

    assert prose == f"{EN_A}\n\n“Late,” he said."
    assert terms == [{"source": "佐々木", "english": "Sasaki",
                      "type": "name"}]
    assert warnings == []


def test_a_response_without_the_block_is_still_a_translation():
    prose, terms, warnings = parse_response(EN_A)

    assert prose == EN_A
    assert terms == []
    assert any("no " in w for w in warnings)


def test_malformed_terms_json_does_not_discard_the_translation():
    """A response whose terms block is broken still carries prose the user paid for.
    Throwing it away over a JSON error would be the expensive mistake."""
    prose, terms, warnings = parse_response(f"{EN_A}\n\n{NEW_TERMS_DELIMITER}\n[{{oops]")

    assert prose == EN_A
    assert terms == []
    assert any("could not parse" in w for w in warnings)


@pytest.mark.parametrize("tail", ["", "not json at all", "{}", "null"])
def test_every_malformed_tail_degrades_to_no_terms(tail):
    prose, terms, _ = parse_response(f"{EN_A}\n\n{NEW_TERMS_DELIMITER}\n{tail}")

    assert prose == EN_A and terms == []


def test_non_dict_entries_in_the_array_are_dropped():
    prose, terms, _ = parse_response(
        f"{EN_A}\n\n{NEW_TERMS_DELIMITER}\n"
        '["a string", {"source": "x", "english": "X"}, 42]')

    assert terms == [{"source": "x", "english": "X"}]


def test_model_chatter_is_stripped_from_the_prose():
    prose, _, warnings = parse_response(f"Here is the translation:\n\n{EN_A}")

    assert prose == EN_A
    assert any("model commentary" in w for w in warnings)


def test_untranslated_japanese_is_never_deleted_here():
    """Deliberately different from the Korean app, which deletes source echoes.
    Residue is FLAGGED by validation instead: a wrong deletion destroys prose, a
    wrong flag costs a glance."""
    prose, _, _ = parse_response(f"{EN_A}\n\n{LEAK}")

    assert LEAK in prose


def test_empty_input_parses_to_nothing():
    assert parse_response("")[0] == ""
    assert parse_response(None)[0] == ""


# ---- chunking ----------------------------------------------------------------

def test_a_short_chapter_is_one_chunk():
    assert len(chunk_paragraphs([JA_A, JA_A], 12000)) == 1


def test_a_long_chapter_is_split_on_paragraph_boundaries():
    """Splitting mid-paragraph would hand the model half a sentence and get half a
    sentence back."""
    paragraphs = ["x" * 500] * 10
    chunks = chunk_paragraphs(paragraphs, 1200)

    assert sum(len(c) for c in chunks) == 10
    assert all(p in paragraphs for chunk in chunks for p in chunk)


def test_a_single_oversized_paragraph_is_not_split():
    chunks = chunk_paragraphs(["x" * 50000], 1200)

    assert chunks == [["x" * 50000]]


def test_no_chunk_is_empty():
    for chunk in chunk_paragraphs(["x" * 500] * 7, 400):
        assert chunk


def test_no_paragraphs_gives_no_chunks():
    assert chunk_paragraphs([], 1000) == []


# ---- options -----------------------------------------------------------------

def test_every_tool_is_blocked():
    """Translation is text in, text out. It has no business reading a file, running a
    command or reaching the network."""
    options = _translator()._options("system")

    assert options.allowed_tools == []
    for tool in ("Bash", "Read", "Write", "WebFetch", "WebSearch"):
        assert tool in options.disallowed_tools


def test_project_settings_are_ignored():
    """A .claude/ directory in the working folder must not be able to change how a
    chapter is translated."""
    assert _translator()._options("system").setting_sources == []


def test_the_system_prompt_replaces_the_default_agent_prompt():
    options = _translator()._options("MY SYSTEM TEXT")

    assert options.system_prompt == "MY SYSTEM TEXT"


def test_an_invalid_effort_falls_back_rather_than_being_sent():
    assert _translator(effort="turbo")._options("s").effort == "high"
    assert _translator(effort="low")._options("s").effort == "low"


def test_thinking_is_switched_by_config():
    assert _translator(thinking=True)._options("s").thinking == {"type": "adaptive"}
    assert _translator(thinking=False)._options("s").thinking == {"type": "disabled"}


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", "opus"),
    ("claude-sonnet-5", "sonnet"),
    ("claude-haiku-4-5-20251001", "haiku"),
    ("some-future-id", "some-future-id"),
    ("", "opus"),
])
def test_the_model_id_maps_to_the_alias_the_cli_expects(model, expected):
    assert agent_model(model) == expected


# ---- the streaming call ------------------------------------------------------

def test_streamed_text_is_returned_and_forwarded_to_the_hooks(monkeypatch):
    chunks: list[str] = []
    monkeypatch.setattr(translator_mod, "query", _fake_query([[
        _assistant("She stood "), _assistant("on the platform."),
        _result(cost=0.01, usage={"m": _Usage(inputTokens=10, outputTokens=20)}),
    ]]))

    text, usage, cost = _translator()._call(
        "sys", "user", hooks=StreamHooks(on_text=chunks.append))

    assert text == "She stood on the platform."
    assert chunks == ["She stood ", "on the platform."]
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 20
    assert cost == 0.01


def test_a_stream_that_never_finishes_is_a_truncation_not_a_translation(monkeypatch):
    """The process died or the connection dropped mid-output. Returning the partial
    text would write half a chapter to disk and mark it done."""
    monkeypatch.setattr(translator_mod, "query",
                        _fake_query([[_assistant("Half a chapt")]]))

    with pytest.raises(TranslatorError, match="incomplete"):
        _translator()._call("sys", "user")


def test_stop_is_honoured_between_streamed_messages(monkeypatch):
    """A threadpool thread cannot be killed from outside, so the only way to end an
    in-flight chapter is to break out of the loop."""
    abort = threading.Event()
    abort.set()
    monkeypatch.setattr(translator_mod, "query",
                        _fake_query([[_assistant("text"), _result()]]))

    with pytest.raises(TaskAborted):
        _translator()._call("sys", "user", hooks=StreamHooks(abort=abort))


def test_a_rejected_rate_limit_event_becomes_a_rate_limit(monkeypatch):
    from claude_agent_sdk.types import RateLimitInfo as SdkRateLimitInfo

    info = SdkRateLimitInfo(status="rejected", resets_at=1234, raw={})
    monkeypatch.setattr(translator_mod, "query", _fake_query([[
        RateLimitEvent(rate_limit_info=info, uuid="u", session_id="s"), _result(),
    ]]))

    with pytest.raises(RateLimited) as caught:
        _translator()._call("sys", "user")
    assert caught.value.info.resets_at == 1234


def test_a_429_result_becomes_a_rate_limit_carrying_the_earlier_reset_time(monkeypatch):
    """The SDK usually emits a WARNING rate-limit event carrying resets_at before the
    hard 429. Reusing it is what lets the worker sleep until the real reset rather than
    guessing."""
    from claude_agent_sdk.types import RateLimitInfo as SdkRateLimitInfo

    warning = SdkRateLimitInfo(status="allowed_warning", resets_at=999, raw={})
    monkeypatch.setattr(translator_mod, "query", _fake_query([[
        RateLimitEvent(rate_limit_info=warning, uuid="u", session_id="s"),
        _result(is_error=True, api_error_status=429),
    ]]))

    with pytest.raises(RateLimited) as caught:
        _translator()._call("sys", "user")
    assert caught.value.info.resets_at == 999


def test_a_non_rate_limit_agent_error_is_a_translator_error(monkeypatch):
    monkeypatch.setattr(translator_mod, "query", _fake_query([[
        _result(is_error=True, errors=["something went wrong"]),
    ]]))

    with pytest.raises(TranslatorError, match="agent error"):
        _translator()._call("sys", "user")


# ---- retrying ----------------------------------------------------------------

def test_a_dropped_connection_is_retried(monkeypatch):
    monkeypatch.setattr(translator_mod, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    resets: list[str] = []
    monkeypatch.setattr(translator_mod, "query", _fake_query([
        [CLIConnectionError("dropped")],
        [_assistant(EN_A), _result()],
    ]))

    text, _, _ = _translator()._call("sys", "user",
                                     hooks=StreamHooks(on_reset=resets.append))

    assert text == EN_A
    assert resets == ["reconnect"]


def test_a_slow_cold_start_is_retried(monkeypatch):
    """The SDK raises a BARE Exception for this, which matched no except clause and
    escaped as an opaque 500. It is recognised by message."""
    monkeypatch.setattr(translator_mod, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    resets: list[str] = []
    monkeypatch.setattr(translator_mod, "query", _fake_query([
        [Exception("Control request timeout: initialize")],
        [_assistant(EN_A), _result()],
    ]))

    text, _, _ = _translator()._call("sys", "user",
                                     hooks=StreamHooks(on_reset=resets.append))

    assert text == EN_A
    assert resets == ["restart"]


@pytest.mark.parametrize("error", [
    CLINotFoundError("not installed"),
    TaskAborted("stopped"),
])
def test_what_must_not_be_retried_is_not(monkeypatch, error):
    """Retrying a missing CLI wastes the retry budget on something that cannot
    succeed; retrying a deliberate stop ignores the user."""
    fake = _fake_query([[error]])
    monkeypatch.setattr(translator_mod, "query", fake)

    with pytest.raises(type(error)):
        _translator()._call("sys", "user")
    assert fake.calls["n"] == 1


def test_an_unknown_error_becomes_a_translator_error(monkeypatch):
    """Callers only handle our own types, so a bare exception surfaced as an opaque
    500 with a raw traceback."""
    monkeypatch.setattr(translator_mod, "query",
                        _fake_query([[ValueError("something odd")]]))

    with pytest.raises(TranslatorError, match="ValueError"):
        _translator()._call("sys", "user")


def test_giving_up_says_how_many_attempts_were_made(monkeypatch):
    monkeypatch.setattr(translator_mod, "time", type("T", (), {"sleep": staticmethod(lambda s: None)}))
    monkeypatch.setattr(translator_mod, "query",
                        _fake_query([[ProcessError("gone")]]))

    with pytest.raises(TranslatorError, match="after 4 attempts"):
        _translator()._call("sys", "user")


# ---- translating a chapter ---------------------------------------------------

def test_a_short_chapter_is_one_call(monkeypatch):
    fake = _fake_query([[
        _assistant(f"{EN_A}\n\n{NEW_TERMS_DELIMITER}\n[]"),
        _result(cost=0.02, usage={"m": _Usage(inputTokens=5, outputTokens=7)}),
    ]])
    monkeypatch.setattr(translator_mod, "query", fake)

    result = _translator().translate_chapter(_chapter(JA_A))

    assert result.english == EN_A
    assert result.chunks == 1
    assert fake.calls["n"] == 1
    assert result.cost_usd == 0.02
    assert result.usage["input_tokens"] == 5


def test_a_long_chapter_is_several_calls_whose_prose_is_joined(monkeypatch):
    prompts: list[str] = []
    fake = _fake_query(
        [[_assistant("PART."), _result(cost=0.01,
                                       usage={"m": _Usage(inputTokens=3)})]],
        on_call=lambda prompt, options: prompts.append(prompt))
    monkeypatch.setattr(translator_mod, "query", fake)

    translator = Translator(AnthropicConfig(),
                            TranslationConfig(chunk_threshold=600))
    result = translator.translate_chapter(_chapter(*(["x" * 500] * 4)))

    assert result.chunks == 4
    assert fake.calls["n"] == 4
    assert result.english == "PART.\n\nPART.\n\nPART.\n\nPART."
    # Usage and cost accumulate across the calls rather than reporting only the last.
    assert result.cost_usd == 0.04
    assert result.usage["input_tokens"] == 12


def test_later_chunks_are_given_the_previous_tail_as_context(monkeypatch):
    """A model that is not told will re-translate it, and the chapter gains a
    duplicated paragraph at every chunk boundary."""
    prompts: list[str] = []
    monkeypatch.setattr(translator_mod, "query", _fake_query(
        [[_assistant("A finished English paragraph."), _result()]],
        on_call=lambda prompt, options: prompts.append(prompt)))

    translator = Translator(AnthropicConfig(),
                            TranslationConfig(chunk_threshold=600))
    translator.translate_chapter(_chapter(*(["x" * 500] * 3)))

    assert "continuity" not in prompts[0].lower()
    assert "A finished English paragraph." in prompts[1]
    assert "Do not" in prompts[1]


def test_the_hooks_see_the_source_and_the_chunk_boundaries(monkeypatch):
    monkeypatch.setattr(translator_mod, "query",
                        _fake_query([[_assistant("P."), _result()]]))
    sources: list[list[str]] = []
    boundaries: list[tuple[int, int]] = []

    translator = Translator(AnthropicConfig(),
                            TranslationConfig(chunk_threshold=600))
    translator.translate_chapter(
        _chapter(*(["x" * 500] * 3)),
        hooks=StreamHooks(on_source=sources.append,
                          on_chunk=lambda i, n: boundaries.append((i, n))))

    assert sources == [["x" * 500] * 3]
    assert boundaries == [(1, 3), (2, 3), (3, 3)]


def test_the_glossary_block_reaches_the_system_prompt(monkeypatch):
    seen: list = []
    monkeypatch.setattr(translator_mod, "query", _fake_query(
        [[_assistant(EN_A), _result()]],
        on_call=lambda prompt, options: seen.append(options.system_prompt)))

    _translator().translate_chapter(_chapter(JA_A),
                                    glossary_block="佐々木 = Sasaki")

    assert "佐々木 = Sasaki" in seen[0]


def test_a_retry_hint_is_appended_to_the_user_message(monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(translator_mod, "query", _fake_query(
        [[_assistant(EN_A), _result()]],
        on_call=lambda prompt, options: prompts.append(prompt)))

    _translator().translate_chapter(_chapter(JA_A),
                                    retry_hint=translator_mod.RETRY_REMINDER)

    assert "failed an automated fidelity check" in prompts[0]


def test_warnings_from_each_part_say_which_part(monkeypatch):
    monkeypatch.setattr(translator_mod, "query",
                        _fake_query([[_assistant("P."), _result()]]))

    translator = Translator(AnthropicConfig(),
                            TranslationConfig(chunk_threshold=600))
    result = translator.translate_chapter(_chapter(*(["x" * 500] * 2)))

    assert any(w.startswith("part 1/2") for w in result.warnings)


def test_a_broken_hook_never_fails_a_translation(monkeypatch):
    """Progress reporting is never worth failing over."""
    monkeypatch.setattr(translator_mod, "query",
                        _fake_query([[_assistant(EN_A), _result()]]))

    def boom(*a):
        raise RuntimeError("the UI went away")

    result = _translator().translate_chapter(
        _chapter(JA_A),
        hooks=StreamHooks(on_text=boom, on_source=boom, on_chunk=boom))

    assert result.english == EN_A
