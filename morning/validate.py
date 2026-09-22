"""Decide whether a finished translation is good enough to be read.

Every check here either FAILS a chapter (it goes to review, and its prose is written
only to the audit copy) or WARNS about it (it is fine, but something is worth a
glance). Nothing here ever edits or deletes prose — that is ``sanitize.strip_meta``'s
job and it is deliberately conservative about it.

## The length-ratio band is not calibrated, and says so

Night Reader's 1.6-3.2 band is derived from real Korean/English pairs. Japanese and
Korean are both dense relative to English, but they are not equally dense, and
kanji-heavy narration compresses differently from kana-heavy dialogue. Carrying the
Korean numbers over and letting them FAIL chapters would reject good translations for
a reason that has nothing to do with them.

So while ``ValidationConfig.ratio_band_calibrated`` is false, the ratio is measured and
reported as a **warning** naming the uncalibrated band; once someone has re-derived it
from real pairs and set the flag, the same check becomes a **failure**. The check is
kept either way — the plan is explicit that it should be — but it does not get to
condemn a chapter on a borrowed number. ``metrics["length_ratio"]`` is recorded on
every chapter from day one, which is exactly the data the re-derivation needs.

## Known gap

There is no character-gender check yet. Japanese omits subjects even more freely than
Korean, so this matters here at least as much; the glossary already pins ``pronoun``
and the prompt already treats that pin as authoritative, so the input exists. Detecting
a contradiction in the OUTPUT is a separate piece of work and is not faked in the
meantime.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .chapters import Chapter, _QUOTE_RE
from .config import ValidationConfig
from .japanese import source_fraction
from .sanitize import source_leak_report, strip_meta
from .textsource import SEP_RE

# Above this fraction of the OUTPUT being Japanese script, the chapter was not
# really translated at all — a different failure from a stray leaked sentence, and
# worth saying differently so the reader is not sent hunting for one bad line.
_WHOLESALE_SOURCE = 0.10

_WS_RE = re.compile(r"\s")


@dataclass
class ValidationResult:
    ok: bool = True
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    # Where the residue is, so the reviewer can see the line rather than be told the
    # chapter has a problem somewhere — and so adding the term to the glossary, which
    # is what resolves a false positive permanently, takes one glance.
    leak_findings: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _paragraphs(text: str) -> list[str]:
    return [p for p in SEP_RE.split(text or "") if p.strip()]


def _nonspace_len(text: str) -> int:
    return len(_WS_RE.sub("", text or ""))


def validate_translation(chapter: Chapter, translation: str, cfg: ValidationConfig,
                         glossary=None) -> ValidationResult:
    """Check one finished translation against its source.

    ``glossary`` is passed through to the residue check, where it acts as the
    allow-list of deliberately kept terms — see :mod:`morning.sanitize`.
    """
    failures: list[str] = []
    warnings: list[str] = []

    source = chapter.metrics
    out_paragraphs = _paragraphs(translation)
    out_dialogue = sum(1 for p in out_paragraphs if _QUOTE_RE.search(p))
    out_chars = _nonspace_len(translation)
    ratio = (out_chars / source.char_count) if source.char_count else 0.0

    metrics = {
        "source_paragraphs": source.paragraph_count,
        "output_paragraphs": len(out_paragraphs),
        "source_dialogue": source.dialogue_count,
        "output_dialogue": out_dialogue,
        "source_chars": source.char_count,
        "output_chars": out_chars,
        "length_ratio": round(ratio, 3),
        "output_source_fraction": round(source_fraction(translation), 4),
    }

    # 0a. Model chatter. `strip_meta` has already removed what it could safely remove;
    #     anything it reports as suspicious is a block where real prose and chatter are
    #     mixed, which it refuses to delete. That needs a human, so it fails.
    _, removed, suspicious = strip_meta(translation)
    if suspicious:
        failures.append(
            f"the model's own commentary is mixed into the prose and could not be "
            f"removed safely (e.g. “{suspicious[0][:80]}”)")
    if removed:
        warnings.append(f"removed {len(removed)} block(s) of model commentary")

    # 0b. Untranslated source. Two signals, deliberately phrased differently: a chapter
    #     that is largely Japanese was not translated at all, whereas a stray run is
    #     one line to look at. Either way the chapter is FLAGGED, never edited.
    leak = source_leak_report(translation, glossary=glossary)
    #     The wholesale test reads the RESIDUAL fraction — what is left once every
    #     deliberately kept term has been subtracted — so it agrees with the run scan
    #     about what counts. Both numbers are recorded, because the raw one is what a
    #     re-derivation of the band would want.
    metrics["residual_source_fraction"] = leak.residual_fraction
    if leak.residual_fraction > _WHOLESALE_SOURCE:
        failures.append("substantial untranslated Japanese remains in the output")
    elif leak.flagged:
        failures.append(leak.summary())

    # 1. Paragraph count. The tolerance scales with chapter length, so minor formatting
    #    merges do not flag while a missing scene still does.
    tolerance = max(cfg.paragraph_tolerance,
                    round(source.paragraph_count * cfg.paragraph_tolerance_pct))
    if abs(len(out_paragraphs) - source.paragraph_count) > tolerance:
        failures.append(
            f"paragraph count {len(out_paragraphs)} vs source "
            f"{source.paragraph_count} (tolerance {tolerance})")

    # 2. Length ratio — the omission and embellishment signal. See the module docstring
    #    for why this warns rather than fails until the band has been re-derived.
    if not source.char_count:
        warnings.append("source has no counted characters; skipping the length check")
    elif ratio < cfg.length_ratio_min or ratio > cfg.length_ratio_max:
        direction = ("below" if ratio < cfg.length_ratio_min else "above")
        limit = (cfg.length_ratio_min if ratio < cfg.length_ratio_min
                 else cfg.length_ratio_max)
        likely = ("likely omission or summarising" if ratio < cfg.length_ratio_min
                  else "likely embellishment")
        message = f"length ratio {ratio:.2f} {direction} {limit} — {likely}"
        if cfg.ratio_band_calibrated:
            failures.append(message)
        else:
            warnings.append(
                message + " (this band has not been re-derived for this language pair "
                          "yet, so it is reported rather than treated as a failure)")

    # 3. Dialogue lines — a secondary signal, so it warns rather than fails.
    if abs(out_dialogue - source.dialogue_count) > cfg.dialogue_tolerance:
        warnings.append(
            f"dialogue lines {out_dialogue} vs source {source.dialogue_count} "
            f"(tolerance {cfg.dialogue_tolerance})")

    # 4. Surface conventions the prompt asks for by name. Warnings only: none of these
    #    makes a translation wrong, and failing a good chapter over a punctuation mark
    #    trains the reader to ignore the review queue.
    warnings.extend(_surface_warnings(translation, out_dialogue))

    return ValidationResult(
        ok=not failures, failures=failures, warnings=warnings, metrics=metrics,
        leak_findings=[asdict(f) for f in leak.findings],
    )


def _surface_warnings(translation: str, out_dialogue: int) -> list[str]:
    out: list[str] = []
    if re.search(r"(?<!\.)\.\.(?!\.)", translation) or re.search(r"\.{4,}", translation):
        out.append("found ellipses that are not exactly three dots")
    if "…" in translation:
        out.append("found the single-character ellipsis … (should be three dots)")
    # 「」 surviving into the output. The prompt asks for these to become English
    # quotation, so their presence means the instruction was not followed — but it is
    # a legitimate house style too, which is why it warns and why
    # `japanese.source_fraction` deliberately does not count them as Japanese.
    if re.search("[「」『』]", translation):
        out.append("Japanese quotation marks 「」 remain in the output")
    if out_dialogue and '"' in translation and not re.search("[“”]", translation):
        out.append("dialogue present but no curly quotes found (straight quotes?)")
    return out
