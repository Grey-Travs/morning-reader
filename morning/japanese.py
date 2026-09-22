"""Script detection for Japanese source text — the easy half of the language layer.

This module answers exactly one question: *how much of this text is written in
Japanese script?* That is enough to tell a source chapter from an already-English one
and to reject an empty tab, which is all the spine needs.

The genuinely hard parts of the Japanese layer are NOT here and are not faked:

  * **Source-residue detection** (has the model left untranslated Japanese in its
    English output?) cannot reuse this function. Night Reader's Korean detector keeps
    chat laughter out by CHARACTER RANGE — ㅋㅋ and ㅠㅠ sit outside the composed-syllable
    block, so excluding a range excludes them. Japanese laughter is ``www`` and ``草``:
    Latin and a perfectly ordinary Kanji that also means "grass". A range carve-out
    cannot express that, so residue detection needs a different rule, not a swapped
    regex. It arrives with the validator in step 2.
  * **The length-ratio band** is tuned per language pair and has to be re-derived from
    real Japanese/English pairs. See ``config.ValidationConfig``.

Keeping those out of here on purpose: a half-right residue detector that silently
passes leaked Japanese is worse than not having one.
"""

from __future__ import annotations

import re

# The scripts that make Japanese prose:
#   3041-309F  Hiragana (with ゛゜ and the iteration marks ゝゞ)
#   30A0-30FF  Katakana (with ー and ・)
#   31F0-31FF  Katakana phonetic extensions
#   3400-4DBF  CJK Unified Ideographs Extension A
#   4E00-9FFF  CJK Unified Ideographs — the common Kanji block
#   F900-FAFF  CJK Compatibility Ideographs (legacy encodings round-trip through these)
#   FF66-FF9F  Half-width Katakana (survives from older site exports)
#   3005 3006 3007 303B  々 〆 〇 〻 — iteration/abbreviation marks that are prose, not
#                                      punctuation, and are common in names (佐々木)
#
# Deliberately EXCLUDED: 3000-3003 and 3008-303A, i.e. the CJK punctuation block —
# 、。「」【】〜 and friends. English translations of Japanese novels routinely keep
# 「」 for dialogue (Night Reader's quote handling already treats them as quotes), so
# counting them as "Japanese" would make a finished English chapter look partly
# untranslated and re-queue work that was already done.
_JAPANESE_RE = re.compile(
    "["
    "\u3041-\u309f"
    "\u30a0-\u30ff"
    "\u31f0-\u31ff"
    "\u3400-\u4dbf"
    "\u4e00-\u9fff"
    "\uf900-\ufaff"
    "\uff66-\uff9f"
    "\u3005\u3006\u3007\u303b"
    "]"
)

_WHITESPACE_RE = re.compile(r"\s")


def source_fraction(text: str) -> float:
    """Fraction of non-whitespace characters written in Japanese script.

    Named for the ROLE the number plays (how much of this is the source language),
    not for the language, because this is the number that gets persisted on every
    chapter and page record. Night Reader persisted ``hangul_fraction`` and that key
    is now on ~3,967 records it cannot cheaply rename.

    Whitespace is excluded from the denominator because Japanese prose has almost
    none and English has a lot: counting spaces would make the same passage score
    differently for no reason that has to do with language.
    """
    stripped = _WHITESPACE_RE.sub("", text or "")
    if not stripped:
        return 0.0
    return len(_JAPANESE_RE.findall(stripped)) / len(stripped)


def has_japanese(text: str) -> bool:
    """Whether any Japanese script appears at all. Cheaper than a fraction when the
    caller only needs presence — and not a residue detector; see the module docstring."""
    return bool(_JAPANESE_RE.search(text or ""))
