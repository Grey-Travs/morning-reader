"""Two different problems with a translated chapter, deliberately kept apart.

1. **Meta-leak** — the model narrating its own task into the prose ("Here is the
   translation", "re-reading the glossary"). This is the model's ENGLISH chatter, so
   it is language-independent and transfers from the Korean app almost unchanged. A
   block that matches is **stripped**.

2. **Source residue** — untranslated Japanese left in the English output. A chapter
   that matches is **flagged for review. Nothing is ever deleted.**

The split is load-bearing and the reason is worth repeating: *losing a leak is
recoverable, deleting prose is not.* A wrong strip destroys a paragraph the user paid
for; a wrong flag costs one glance.

## Why residue detection could not be ported

The Korean rule is: flag if two whitespace-separated Hangul words appear, or if there
are eight or more composed Hangul syllables. Chat laughter (ㅋㅋ, ㅠㅠ) is excluded **by
character range** — compatibility jamo sit outside the composed-syllable block.

Both halves fail for Japanese. Japanese laughter is ``www`` (Latin) and ``草`` (an
ordinary Kanji meaning "grass"), so no range separates laughter from prose; and
Japanese has no inter-word spaces, so the two-word rule can never fire.

## What three independent designs and three adversaries established

A judge panel designed this rule three ways — by grammar, by run length, by
allow-list — and three adversaries then attacked the winner with executed inputs.
Every one came back with confirmed failures, and together they falsified the premise
all three shared:

> a kept term is a noun phrase; a leak is a predication

It is not true. A deliberately kept work title is a full Japanese predication. So are
the most commonly kept terms in the whole language — もったいない, しょうがない,
お疲れ様です, いってらっしゃい. Meanwhile a real leak is often *not* clause-final
(何してるの ends in a sentence-final particle) and can be shorter than a kept term.

**Kept and leaked are character-identical. The difference is intent, and intent is not
in the text.** No amount of grammar, length or script analysis can recover it.

## So the rule stops guessing at intent

The app already has a human-curated, per-project record of exactly which Japanese is
deliberately kept: **the glossary**. A term in it is by definition intentional. So the
glossary is subtracted before anything is scored, and what remains is judged by a
short, conservative rule.

That makes false positives **self-healing** rather than something to tune away: a kept
term flags once, the reviewer adds it to the glossary — which they wanted to do
anyway, for spelling consistency — and it never flags again. Tuning weights to
suppress it, which is what the three designs were reduced to, trades directly against
real detection and never converges.

## What this still misses, honestly

* **Noun-only residue is invisible.** An untranslated notice (本日休業) or a list of
  place names has no grammatical machinery and scores nothing. This is the direct
  price of refusing a bare length threshold — the same property that keeps a kept shop
  sign safe. Catching it needs different evidence, not a lower threshold.
* **Very short leaked utterances are missed** (帰りたい, もういい). Lowering the bar to
  catch them flags kept food names and sound effects instead.
* **A kept term's FIRST occurrence flags**, before anyone has added it to the glossary.
  That is the intended cost, and it is one review click.

Every threshold here is stated, and no single signal can flag on its own — asserted at
import, because the design that lost this panel promised exactly that invariant in its
docstring while two of its signals violated it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from .textsource import SEP_RE

# ---------------------------------------------------------------- meta leaks
# The model's own English chatter. Deliberately PRECISE: a false positive here
# DELETES real story prose, so phrases characters actually say ("I apologize",
# "let me check") are never signals.

# ALWAYS signals — notation and phrasing that does not occur in web-novel prose, so
# they mark a block as meta even when it also contains dialogue.
_ALWAYS = re.compile(
    r"""(?ix)
      \bglossary\b
    | the\ narrator(\ here)?\ is
    | the\ (original|source)\ japanese\b
    | i'?ll\ use\ the\ (spelling|reading)
    | re-?reading\ the\ (chapter|source|glossary|names?|passage)
    | romaniz(e|ed|ing|ation)
    | ===\s*new_terms
    | \bas\ an\ ai\b
    | translator'?s?\ note\b
    """
)

# SELF-CORRECTION / FRAMING — the model narrating its task. Only counts when the block
# has NO dialogue quotes, so "'Let me redo my makeup,' she said" is safe. The verbs
# after "let me" are restricted to unambiguous translation-meta; broad story verbs are
# deliberately NOT matched, so real narration ("Let me write you a letter, she
# decided") is never deleted.
_SELF = re.compile(
    r"(?i)\blet'?s?\s+re-?do\b"
    r"|\blet\s+me\s+(?:just\s+|now\s+|simply\s+|carefully\s+|go\s+ahead\s+and\s+)?"
    r"(?:re-?do|re-?read|re-?translate|rewrite|start\s+over)\b"
    r"|\b(?:i'?ll|i\s+will|let\s+me|let'?s)\s+(?:just\s+|now\s+|simply\s+|go\s+ahead\s+and\s+)?"
    r"(?:translate|render|produce|provide|rewrite|give\s+you)\s+"
    r"(?:the\s+|this\s+|your\s+|my\s+|a\s+)?(?:full\s+|whole\s+|entire\s+|rest\s+of\s+the\s+)?"
    r"(?:translat\w*|chapter|text|passage|version|content|section|following)\b"
    # A bare task announcement that is essentially the WHOLE block. Anchored, so it can
    # never fire on a marker trailing real prose — deleting that block would drop the
    # real English before it. Such mixed leaks are caught by the residue flag instead,
    # which routes to review rather than deleting.
    r"|^\s*(?:i'?ll|i\s+will|let\s+me|let'?s)\s+(?:just\s+|now\s+|simply\s+|go\s+ahead\s+and\s+)?"
    r"(?:translate|render)\s*[.!?…]?\s*$"
    r"|\bhere(?:\s+is|'?s)\s+(?:the\s+|your\s+|my\s+)?translat"
    r"|\bbelow\s+is\s+the\s+translat"
    r"|\bthe\s+translation\s+(?:is\s+(?:as\s+follows|below)|follows|begins)"
    r"|\btranslated\s+chapter\s*:"
)

_QUOTE = re.compile("[\"“”「」『』]")
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")


def block_is_meta(block: str) -> bool:
    """Whether a block is leaked model chatter rather than prose.

    A mostly-Japanese block is NOT meta — that is a residue problem, handled
    separately — so the strip can never delete a sound effect or a real passage.
    """
    if _ALWAYS.search(block) or _RULE.match(block):
        return True
    return bool(_SELF.search(block) and not _QUOTE.search(block))


# Roughly, a sentence. Only used to answer one question: if the meta were removed,
# would real prose be left behind?
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+")

# A remnant shorter than this is not prose worth protecting — a stray "Okay." left
# beside a task announcement is not a sentence anyone loses sleep over.
_SUBSTANTIAL_CHARS = 25


def _is_mixed_block(block: str) -> bool:
    """Whether this block holds real prose ALONGSIDE the chatter.

    The distinction that matters, because the two cases deserve opposite treatment. A
    block that is nothing but a task announcement can be deleted. A block where the
    model corrected itself mid-paragraph holds real sentences on either side, and
    deleting it destroys prose the user paid for.
    """
    remainder = [s for s in _SENTENCE_SPLIT.split(block)
                 if s.strip() and not _SELF.search(s) and not _ALWAYS.search(s)]
    return sum(len(s.strip()) for s in remainder) >= _SUBSTANTIAL_CHARS


def strip_meta(text: str) -> tuple[str, list[str], list[str]]:
    """Remove leaked model chatter.

    Returns ``(clean_text, removed_blocks, suspicious_blocks)``.

    Paragraph-granular on purpose: deleting a sentence out of the middle of a
    paragraph would leave prose that reads as if the translator lost their place.

    But paragraph granularity alone is not safe either, and this is the sharp edge.
    When the model corrects itself MID-PARAGRAPH, the block holds real sentences on
    both sides of the chatter — and deleting the block destroys them. So a block that
    still contains substantial prose once the chatter is discounted is **kept and
    reported** rather than removed. It goes to review, where a human decides.

    Unambiguous notation (``===NEW_TERMS===``, "translator's note", a horizontal rule)
    is removed regardless: those never occur inside real prose, so there is nothing to
    protect.

    The rule throughout is the module's rule: losing a leak is recoverable, deleting
    prose is not.
    """
    kept: list[str] = []
    removed: list[str] = []
    suspicious: list[str] = []

    for block in SEP_RE.split(text or ""):
        if not block.strip():
            continue
        if not block_is_meta(block):
            kept.append(block)
            continue
        # Notation that never appears in prose — safe to delete whatever surrounds it.
        if _ALWAYS.search(block) or _RULE.match(block):
            removed.append(block.strip())
            continue
        if _is_mixed_block(block):
            kept.append(block)
            suspicious.append(block.strip())
        else:
            removed.append(block.strip())

    return "\n\n".join(kept), removed, suspicious


# ------------------------------------------------------------ source residue

# Format characters — zero-width joiners, bidi marks, the soft hyphen. Stripped FIRST.
# An adversary defeated every pattern below by interleaving U+200B between characters:
# the runs split, and every lookbehind and lookahead stopped matching.
def _strip_format_chars(text: str) -> str:
    return "".join(c for c in text
                   if unicodedata.category(c) != "Cf" and c != "­")


# Japanese script. Half-width katakana is INCLUDED — it survives in older site
# exports, and a leak arriving in it must not be invisible.
#
# Note there is no NFKC normalisation anywhere in this module. The design that lost
# this panel ran it first, which rewrote ！ to ! and U+3000 to a plain space, so its
# own full-width punctuation patterns stopped matching and it missed real leaks. Both
# forms are simply listed instead.
_SCRIPT = (
    "ぁ-ゟ"      # hiragana
    "ァ-ヺー"  # katakana (without the middle dot — see _GLUE)
    "ㇰ-ㇿ"
    "㐀-䶿一-鿿豈-﫿"  # kanji
    "ｦ-ﾟ"      # half-width katakana
    "々〆〇〻"
)
_SCRIPT_RE = re.compile(f"[{_SCRIPT}]")

# Characters that hold one sentence together without being script themselves. The
# katakana middle dot is glue, NOT script: an adversary showed it welding three
# separate kept terms into one over-long run.
_GLUE = "　、。・！？，．…ー"

_RUN_RE = re.compile(f"[{_SCRIPT}][{_SCRIPT}{_GLUE}]*")

# Sentence-final punctuation, in BOTH the full-width and ASCII forms a model emits.
_HARD_END = re.compile("[。！？.!?]")
# A comma implies a clause boundary, which implies more than a term.
_CLAUSE_MARK = re.compile("[、，]")

# Case-binding particles. の and と are deliberately ABSENT: they are nominal linkers
# (genitive and list marker), and including them is what let a kanji date and a film
# title score as predications in the design this replaces.
_BIND = re.compile("[をがにへ]")
# Topic markers only count between two script characters, so the は inside a native
# word does not count.
_TOPIC = re.compile(f"(?<=[{_SCRIPT}])[はも](?=[{_SCRIPT}])")

# Clause-final predicate morphology.
_PREDICATE = re.compile(
    "(?:だ|です|ます|ました|でした"
    "|である|ない|なかった|たい"
    "|ている|ていた|ていただ"
    ")$"
)
# Sentence-final particles. A real leak often ends in one of these rather than in a
# dictionary-form predicate — 何してるの is a complete utterance that the
# clause-final-only rule of the losing design scored at zero.
_FINAL_PARTICLE = re.compile("[のよねわぞさぜか]$")

_HIRAGANA_RE = re.compile("[ぁ-ゟ]")
_KANJI_RE = re.compile("[㐀-䶿一-鿿豈-﫿]")
_LATIN_RE = re.compile("[A-Za-z]")

# A URL, stopping at Japanese as well as at whitespace. `\S+` is wrong here for the
# reason this whole module exists: Japanese has no spaces, so `\S+` swallowed the
# leaked sentence that followed a URL and hid it completely.
_URL = re.compile(f"(?:https?://|www\\.)[^\\s{_SCRIPT}、。]+")
# Markdown and HTML that is never prose.
_MARKUP = re.compile(r"`[^`]*`|<[^>]{1,120}>|!?\[[^\]]*\]\([^)]*\)")

# Laughter. `www` is Latin so it never forms a run at all; 草 is an ordinary kanji and
# is blanked only when it stands ALONE between non-script characters — 草原 and a name
# containing it are untouched.
_LAUGH_ALONE = re.compile(f"(?<![{_SCRIPT}])草+(?![{_SCRIPT}])")

# Set phrases that English translations keep so often that flagging them on first
# sight would be noise rather than signal. Deliberately SHORT and fixed: the general
# mechanism for "this term is kept on purpose" is the project's glossary, not this
# list, because that one is per-work and maintained by the person reading it.
KEPT_PHRASES = (
    "もったいない",          # mottainai
    "しょうがない",          # shouganai
    "仕方がない",                # shikata ga nai
    "お疲れ様です",          # otsukaresama desu
    "いただきます",          # itadakimasu
    "ごちそうさまでした",  # gochisousama deshita
    "いってらっしゃい",        # itterasshai
    "いってきます",          # ittekimasu
    "おかえりなさい",    # okaerinasai
    "ただいま",                      # tadaima
    "よろしく",                      # yoroshiku
    "おはよう",                      # ohayou
    "ありがとう",                # arigatou
    "すみません",                # sumimasen
    "がんばって",                # ganbatte
)

# How much evidence a run needs before the chapter is flagged.
FLAG_AT = 3

# Every signal's weight. Asserted below to be strictly under FLAG_AT, because the
# design this replaces promised that invariant in its docstring while two of its
# signals broke it — and each of the three adversaries found that independently.
_W_HARD_END = 2       # the run carries a sentence-ending stop
_W_PROSE_SHAPE = 2    # long enough, and mixes kanji with kana: okurigana and particles
_W_PREDICATE = 2      # clause-final predicate morphology
_W_BIND = 1           # a case-binding particle
_W_TOPIC = 1          # a topic marker between two script characters
_W_CLAUSE = 1         # an internal comma
_W_FINAL_PARTICLE = 1  # a sentence-final particle

assert max(_W_HARD_END, _W_PROSE_SHAPE, _W_PREDICATE, _W_BIND, _W_TOPIC,
           _W_CLAUSE, _W_FINAL_PARTICLE) < FLAG_AT, (
    "no single signal may flag on its own: two signals must agree, or one mistaken "
    "pattern becomes a false positive on every chapter that contains it"
)

# A run must be at least this long, and mix kanji with kana, to count as prose-shaped.
# Five rather than four: it keeps four-character kept foods and compounds below the
# bar while admitting the shortest real predications.
_PROSE_MIN_CHARS = 5


@dataclass
class LeakFinding:
    """One stretch of Japanese that looks like prose rather than a kept term."""

    text: str
    line: int
    score: int
    reasons: list[str] = field(default_factory=list)


@dataclass
class LeakReport:
    """What the residue check found. ``flagged`` is the verdict; the rest is why.

    The findings exist so a reviewer can see the offending run rather than being told
    "this chapter has a problem somewhere" — and so that adding the term to the
    glossary, which is what resolves a false positive permanently, takes one glance.
    """

    flagged: bool = False
    findings: list[LeakFinding] = field(default_factory=list)

    def summary(self) -> str:
        if not self.flagged:
            return ""
        worst = max(self.findings, key=lambda f: f.score)
        where = f"line {worst.line}"
        return (f"Untranslated Japanese may remain ({where}): {worst.text[:40]}"
                + ("…" if len(worst.text) > 40 else ""))


def _blank(pattern: re.Pattern, text: str) -> str:
    """Replace every match with spaces of the SAME length.

    Equal length matters: every offset stays aligned, so a later line number is still
    the line the reader will see.
    """
    return pattern.sub(lambda m: " " * len(m.group(0)), text)


def _blank_literal(text: str, needle: str) -> str:
    if not needle:
        return text
    return text.replace(needle, " " * len(needle))


def _score_run(run: str) -> tuple[int, list[str]]:
    core = run.strip("".join(_GLUE) + " ")
    if not core:
        return 0, []

    score, reasons = 0, []
    if _HARD_END.search(run):
        score += _W_HARD_END
        reasons.append("ends a sentence")
    if (len(core) >= _PROSE_MIN_CHARS
            and _KANJI_RE.search(core) and _HIRAGANA_RE.search(core)):
        score += _W_PROSE_SHAPE
        reasons.append("kanji mixed with kana over several characters")
    if _PREDICATE.search(core):
        score += _W_PREDICATE
        reasons.append("ends in a predicate")
    if _BIND.search(core):
        score += _W_BIND
        reasons.append("carries a case particle")
    if _TOPIC.search(core):
        score += _W_TOPIC
        reasons.append("carries a topic marker")
    if _CLAUSE_MARK.search(run):
        score += _W_CLAUSE
        reasons.append("contains a clause break")
    if _FINAL_PARTICLE.search(core):
        score += _W_FINAL_PARTICLE
        reasons.append("ends in a sentence-final particle")
    return score, reasons


def source_leak_report(text, *, glossary=None, extra_terms=()) -> LeakReport:
    """Find untranslated Japanese that reads as prose rather than as a kept term.

    ``glossary`` is a :class:`~morning.glossary.Glossary`. Everything in it — every
    spelling of every entry — is subtracted before scoring, because an entry in the
    glossary is by definition a term the reader chose to keep. That is what makes a
    false positive self-healing instead of something to tune away.

    Never raises and never modifies the text: this decides whether a human should
    look, and nothing more.
    """
    if not isinstance(text, str) or not text:
        return LeakReport()

    cleaned = _strip_format_chars(text)
    cleaned = _blank(_URL, cleaned)
    cleaned = _blank(_MARKUP, cleaned)
    cleaned = _blank(_LAUGH_ALONE, cleaned)

    for phrase in KEPT_PHRASES:
        cleaned = _blank_literal(cleaned, phrase)
    for term in extra_terms or ():
        cleaned = _blank_literal(cleaned, str(term))
    if glossary is not None:
        try:
            for entry in glossary.entries():
                for spelling in entry.spellings():
                    cleaned = _blank_literal(cleaned, spelling)
        except Exception:  # noqa: BLE001 — a bad glossary must not break the check
            pass

    findings: list[LeakFinding] = []
    for lineno, line in enumerate(cleaned.split("\n"), 1):
        if not _SCRIPT_RE.search(line):
            continue
        for match in _RUN_RE.finditer(line):
            score, reasons = _score_run(match.group(0))
            if score >= FLAG_AT:
                findings.append(LeakFinding(text=match.group(0).strip(), line=lineno,
                                            score=score, reasons=reasons))

    return LeakReport(flagged=bool(findings), findings=findings)


def has_source_leak(text, *, glossary=None, extra_terms=()) -> bool:
    """The thin boolean wrapper. Used by validation to FLAG a chapter for review —
    never to delete text."""
    return source_leak_report(text, glossary=glossary,
                              extra_terms=extra_terms).flagged
