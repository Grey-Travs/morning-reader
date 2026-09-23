"""Drives the Claude Code CLI to translate a chapter.

Text in, text out. Every tool is blocked — this call has no business reading a file,
running a command or reaching the network, and the block list is explicit rather than
implied so that adding a tool is a visible decision.

Three things here are subtle and each exists because of a specific failure:

* **A stream that ends without a result message is a truncation, not a translation.**
  The process died or the connection dropped mid-output. Returning the partial text
  would write a half chapter to disk and mark it done.
* **Retry only what is worth retrying.** A dropped connection and a slow cold start
  retry with backoff; a rate limit, a config error and a deliberate stop do not. The
  SDK raises a BARE ``Exception`` for a startup timeout, which matched no ``except``
  clause and escaped as an opaque 500, so it is recognised by message.
* **Abort is checked between streamed messages.** A threadpool thread cannot be
  killed from outside, so the only way to end an in-flight chapter is to break out of
  the loop, which closes the generator and tears the subprocess down.

Deliberately NOT carried from the Korean app: it deletes paragraphs it judges to be
untranslated source echoes. This one never does. Residue is detected by
:mod:`morning.sanitize` and FLAGGED for review, because a wrong deletion destroys
prose and a wrong flag costs a glance.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    CLIConnectionError,
    CLINotFoundError,
    ProcessError,
    RateLimitEvent,
    ResultMessage,
    TextBlock,
    query,
)

from .chapters import Chapter
from .config import AnthropicConfig, TranslationConfig
from .exceptions import RateLimited, RateLimitInfo, TaskAborted
from .prompts import (
    NEW_TERMS_DELIMITER, build_system_prompt, build_user_message,
)
from .sanitize import strip_meta

_VALID_EFFORT = {"low", "medium", "high", "xhigh", "max"}

# Tools the agent must never reach for. Translation is text in, text out.
_BLOCKED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebSearch", "WebFetch", "NotebookEdit", "TodoWrite", "Task",
]

# Appended when a chapter is retried after failing its fidelity check.
RETRY_REMINDER = (
    "\n\nIMPORTANT: your previous attempt failed an automated fidelity check. "
    "Translate the section COMPLETELY — omit nothing, condense nothing, add nothing. "
    "Match the source paragraph by paragraph."
)

# The SDK raises a bare Exception when the CLI starts but does not answer its
# `initialize` control request in time. It is almost always a slow cold start — an
# antivirus scanning the node process, a loaded machine — so it retries successfully.
# As a bare Exception it matched no except clause and escaped as an opaque 500.
_STARTUP_TIMEOUT_MARKERS = ("control request timeout", "initialize timeout")

_WS_RE = re.compile(r"\s")


class TranslatorError(RuntimeError):
    """A non-recoverable error from the agent that is not a rate limit."""


def _is_startup_timeout(exc: BaseException) -> bool:
    return any(m in str(exc).lower() for m in _STARTUP_TIMEOUT_MARKERS)


def _emit(fn: Callable | None, *args) -> None:
    """Call a progress hook defensively. A broken or slow callback must never fail a
    translation that otherwise succeeded."""
    if fn is None:
        return
    try:
        fn(*args)
    except Exception:  # noqa: BLE001 — progress reporting is never worth failing over
        pass


@dataclass
class StreamHooks:
    """Live-progress callbacks for one chapter, invoked from the translator's thread.

    Implementations must be non-blocking and must marshal to the event loop
    themselves. They must not raise; ``_emit`` swallows it if they do.

    Text arrives append-only, punctuated by two boundary signals:

    * ``on_chunk(i, n)`` — an oversized chapter is translated in ``n`` calls whose
      prose is CONCATENATED. Everything streamed so far is final: treat this as a
      commit point, not a clear.
    * ``on_reset(reason)`` — discard streamed text. ``"reconnect"`` and ``"restart"``
      abandon only the current chunk's partial output (earlier chunks stand);
      ``"retry"`` means the whole chapter is being redone, so drop everything.
    """

    on_source: Callable[[list[str]], None] | None = None
    on_text: Callable[[str], None] | None = None
    on_reset: Callable[[str], None] | None = None
    on_chunk: Callable[[int, int], None] | None = None
    abort: threading.Event | None = None

    def aborted(self) -> bool:
        return self.abort is not None and self.abort.is_set()

    # Callers use these rather than the raw fields, so an unset hook and a hook that
    # raises are both handled in one place.
    def source(self, paragraphs: list[str]) -> None:
        _emit(self.on_source, paragraphs)

    def text(self, chunk: str) -> None:
        _emit(self.on_text, chunk)

    def reset(self, reason: str) -> None:
        _emit(self.on_reset, reason)

    def chunk(self, i: int, n: int) -> None:
        _emit(self.on_chunk, i, n)


@dataclass
class TranslationResult:
    english: str = ""
    new_terms: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    chunks: int = 1


def agent_model(model: str) -> str:
    """Map a full model id to the alias the CLI expects, robustly to id format."""
    m = (model or "").lower()
    for alias in ("opus", "sonnet", "haiku"):
        if alias in m:
            return alias
    # An unknown id passes through unchanged — the SDK accepts full model ids.
    return model or "opus"


def _accumulate(into: dict, src: dict) -> None:
    for key, value in (src or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            into[key] = into.get(key, 0) + value


def _extract_usage(result) -> dict:
    out = {"input_tokens": 0, "output_tokens": 0,
           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for usage in (getattr(result, "model_usage", None) or {}).values():
        if not isinstance(usage, dict):
            continue
        out["input_tokens"] += usage.get("inputTokens", 0) or 0
        out["output_tokens"] += usage.get("outputTokens", 0) or 0
        out["cache_read_input_tokens"] += usage.get("cacheReadInputTokens", 0) or 0
        out["cache_creation_input_tokens"] += usage.get("cacheCreationInputTokens", 0) or 0
    return out


def parse_response(text: str) -> tuple[str, list[dict], list[str]]:
    """Split the model's output into prose and the new-terms JSON array.

    Model chatter is stripped here so it can never reach a chapter file — but only the
    part ``strip_meta`` considers safe to remove. Untranslated Japanese is deliberately
    left alone: it is FLAGGED by validation, never deleted.

    Degrades rather than raising. A response whose terms block is malformed still
    carries a real translation, and throwing it away over a JSON error would discard
    work the user paid for.
    """
    warnings: list[str] = []
    text = text or ""

    prose_part, found, tail = text.partition(NEW_TERMS_DELIMITER)
    if not found:
        warnings.append("response had no "
                        f"{NEW_TERMS_DELIMITER} block; treating all output as prose")
        tail = ""

    prose, removed, suspicious = strip_meta(prose_part)
    if removed:
        warnings.append(f"stripped {len(removed)} block(s) of model commentary")
    if suspicious:
        warnings.append(f"{len(suspicious)} block(s) mix commentary with real prose "
                        f"and were left for review rather than deleted")

    new_terms: list[dict] = []
    if found:
        start, end = tail.find("["), tail.rfind("]")
        if start != -1 and end > start:
            try:
                parsed = json.loads(tail[start:end + 1])
            except json.JSONDecodeError:
                warnings.append("could not parse the new-terms JSON block")
            else:
                if isinstance(parsed, list):
                    new_terms = [d for d in parsed if isinstance(d, dict)]
                else:
                    warnings.append("the new-terms block was not a JSON array")
        else:
            warnings.append("the new-terms block held no JSON array")

    return prose.strip(), new_terms, warnings


def chunk_paragraphs(paragraphs: list[str], threshold: int) -> list[list[str]]:
    """Split paragraphs into chunks each under the character threshold.

    Split on paragraph boundaries only. Splitting mid-paragraph would hand the model
    half a sentence and get half a sentence back.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        length = len(_WS_RE.sub("", paragraph))
        if current and size + length > threshold:
            chunks.append(current)
            current, size = [], 0
        current.append(paragraph)
        size += length
    if current:
        chunks.append(current)
    return chunks


class Translator:
    def __init__(self, cfg: AnthropicConfig, tcfg: TranslationConfig):
        self.cfg = cfg
        self.tcfg = tcfg

    # ---- the agent call ----------------------------------------------------
    def _options(self, system_text: str, max_turns: int = 1, *,
                 tools: list[str] | None = None,
                 cwd: str | Path | None = None,
                 add_dirs: list[str | Path] | None = None) -> ClaudeAgentOptions:
        """Build the call's options.

        ``tools`` is what a SPECIFIC call opts into. Reading a page needs ``Read``,
        because the only way to hand the agent an image is a path it opens off disk;
        translating prose needs nothing and gets nothing. Anything not named here stays
        blocked, so the default call is unchanged — text in, text out.

        ``cwd``/``add_dirs`` scope that ``Read`` to one folder, so a call that can open
        a file can only open the pages of the project it was given.
        """
        allowed = list(tools or [])
        return ClaudeAgentOptions(
            system_prompt=system_text,            # replaces the default agent prompt
            allowed_tools=allowed,
            disallowed_tools=[t for t in _BLOCKED_TOOLS if t not in allowed],
            permission_mode="bypassPermissions",   # headless: never prompt
            setting_sources=[],                    # ignore project .claude/ config
            max_turns=max_turns,
            model=agent_model(self.cfg.model),
            effort=(self.cfg.effort if self.cfg.effort in _VALID_EFFORT else "high"),
            thinking={"type": "adaptive"} if self.cfg.thinking else {"type": "disabled"},
            cwd=str(cwd) if cwd else None,
            add_dirs=[str(d) for d in (add_dirs or [])],
        )

    async def _aquery(self, system_text: str, user_text: str, max_turns: int = 1,
                      hooks: StreamHooks | None = None, *,
                      tools: list[str] | None = None,
                      cwd: str | Path | None = None,
                      add_dirs: list[str | Path] | None = None
                      ) -> tuple[str, dict, float]:
        texts: list[str] = []
        usage: dict = {}
        cost = 0.0
        rejected = None
        last_info = None   # the latest rate-limit info seen, including warnings
        got_result = False

        options = self._options(system_text, max_turns, tools=tools, cwd=cwd,
                                add_dirs=add_dirs)
        async for message in query(prompt=user_text, options=options):
            # Cooperative stop. A threadpool thread cannot be killed, so the only way
            # to end an in-flight chapter is to check between messages and break out,
            # which closes the generator and tears the subprocess down.
            if hooks is not None and hooks.aborted():
                raise TaskAborted("stopped by the user")

            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        texts.append(block.text)
                        if hooks is not None:
                            hooks.text(block.text)
            elif isinstance(message, RateLimitEvent):
                last_info = message.rate_limit_info
                if getattr(last_info, "status", None) == "rejected":
                    rejected = last_info
            elif isinstance(message, ResultMessage):
                got_result = True
                cost = message.total_cost_usd or 0.0
                usage = _extract_usage(message)
                if message.is_error:
                    detail = (message.api_error_status or message.errors
                              or message.subtype)
                    # A 429 is a rate limit: raise it as one so the worker rides it out
                    # and resumes, rather than surfacing a raw error. The SDK usually
                    # emits a warning RateLimitEvent carrying resets_at before the hard
                    # 429, so reuse that reset time when there is one.
                    if (str(getattr(message, "api_error_status", "")) == "429"
                            or "429" in str(detail)
                            or "rate limit" in str(detail).lower()):
                        raise RateLimited(RateLimitInfo(
                            resets_at=getattr(last_info, "resets_at", None),
                            message=str(detail)))
                    raise TranslatorError(f"agent error: {detail}")

        if rejected is not None:
            raise RateLimited(RateLimitInfo(
                resets_at=getattr(rejected, "resets_at", None),
                message="the plan's usage window is exhausted"))
        # A stream that ended with no ResultMessage was cut off. Returning the partial
        # text would write half a chapter and mark it finished.
        if not got_result:
            raise TranslatorError(
                "incomplete response: the model run ended before finishing")
        return "".join(texts).strip(), usage, cost

    def _call(self, system_text: str, user_text: str, max_turns: int = 1,
              hooks: StreamHooks | None = None, *,
              tools: list[str] | None = None,
              cwd: str | Path | None = None,
              add_dirs: list[str | Path] | None = None) -> tuple[str, dict, float]:
        """One agent call -> (text, usage, cost). Blocking.

        ``tools``/``cwd``/``add_dirs`` let one call opt into a normally-blocked tool;
        omitting them keeps every tool blocked.
        """
        last: BaseException | None = None
        attempts = max(1, self.cfg.api_retry_count)
        for attempt in range(attempts):
            try:
                return asyncio.run(self._aquery(
                    system_text, user_text, max_turns, hooks,
                    tools=tools, cwd=cwd, add_dirs=add_dirs))
            except (RateLimited, TranslatorError, CLINotFoundError, TaskAborted):
                raise  # never retry a hard limit, a config error, or a deliberate stop
            except (CLIConnectionError, ProcessError) as exc:
                last = exc
                if attempt < attempts - 1:
                    if hooks is not None:
                        hooks.reset("reconnect")
                    time.sleep(min(2 ** attempt, 30))
            except Exception as exc:  # noqa: BLE001
                # A startup timeout is transient — retry it like a dropped connection.
                # Anything else genuinely unknown becomes a TranslatorError rather than
                # escaping bare: callers only handle our own types, so a bare exception
                # surfaced as an opaque 500 with a raw traceback.
                if not _is_startup_timeout(exc):
                    raise TranslatorError(f"{type(exc).__name__}: {exc}") from exc
                last = exc
                if attempt < attempts - 1:
                    if hooks is not None:
                        hooks.reset("restart")
                    time.sleep(min(2 ** attempt, 30))
        raise TranslatorError(
            f"the agent could not be reached after {attempts} attempts: {last}")

    # ---- translating one chapter -------------------------------------------
    def translate_chapter(self, chapter: Chapter, *, glossary_block: str = "",
                          hooks: StreamHooks | None = None,
                          retry_hint: str = "") -> TranslationResult:
        """Translate one chapter, in as many calls as its length requires.

        Chunks are concatenated, and each after the first is given the tail of the
        previous one for continuity — marked as context, because a model that is not
        told will re-translate it and the chapter gains a duplicated paragraph at every
        boundary.
        """
        system_text = build_system_prompt(
            style_note=self.tcfg.style_note,
            honorific_note=self.tcfg.honorific_note,
            glossary_block=glossary_block,
        )
        chunks = chunk_paragraphs(chapter.paragraphs, self.tcfg.chunk_threshold)
        total = len(chunks)

        if hooks is not None:
            hooks.source(chapter.paragraphs)

        english_parts: list[str] = []
        new_terms: list[dict] = []
        warnings: list[str] = []
        usage: dict = {}
        cost = 0.0

        for index, paragraphs in enumerate(chunks, start=1):
            if hooks is not None:
                hooks.chunk(index, total)
            tail = ""
            if english_parts:
                previous = english_parts[-1].split("\n\n")
                tail = "\n\n".join(previous[-self.tcfg.continuity_paragraphs:])

            user_text = build_user_message(
                paragraphs,
                title=chapter.title,
                chunk_index=index,
                chunk_total=total,
                previous_tail=tail,
                extra_instruction=self.tcfg.extra_instruction,
            )
            if retry_hint:
                user_text += retry_hint

            raw, chunk_usage, chunk_cost = self._call(system_text, user_text,
                                                      hooks=hooks)
            prose, terms, chunk_warnings = parse_response(raw)
            english_parts.append(prose)
            new_terms.extend(terms)
            warnings.extend(
                (f"part {index}/{total}: {w}" if total > 1 else w)
                for w in chunk_warnings)
            _accumulate(usage, chunk_usage)
            cost += chunk_cost

        return TranslationResult(
            english="\n\n".join(p for p in english_parts if p.strip()),
            new_terms=new_terms, warnings=warnings, usage=usage,
            cost_usd=round(cost, 6), chunks=total,
        )


__all__ = [
    "RETRY_REMINDER", "StreamHooks", "Translator", "TranslationResult",
    "TranslatorError", "agent_model", "chunk_paragraphs", "parse_response",
]
