"""Typed configuration loaded from a TOML file.

Every tunable is exposed here so behaviour can be changed without touching code.
Read with the 3.11+ stdlib ``tomllib``.

Only what step 1 actually reads is declared. A knob that nothing consumes is worse
than an honest absence — it reads as a setting you can turn on. Night Reader carried
four of those (``temperature``, ``max_output_tokens``, ``include_prev_translation``,
``romanization``) and each one had to be removed later with a note explaining that it
never did anything.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class AnthropicConfig(BaseModel):
    """Model settings. Nothing in step 1 makes a model call; these exist so the job
    events can report what a run *would* use, and so step 2 has one place to read."""

    model: str = "claude-opus-5"
    effort: str = "high"
    thinking: bool = True
    api_retry_count: int = 4


class PathsConfig(BaseModel):
    """Per-project file locations. Overlaid by ``server.projects.project_config``;
    the defaults here are only meaningful when the engine is driven directly."""

    output_dir: Path = Path("chapters")
    state_file: Path = Path("state.json")
    audit_dir: Path = Path("audit")
    glossary_json: Path = Path("glossary.json")
    # A human-readable companion, rewritten whenever the JSON is. The JSON is the
    # source of truth; this exists so a name can be checked at a glance without
    # opening a data file.
    glossary_md: Path = Path("glossary.md")
    glossary_pending: Path = Path("glossary_pending.json")


class TranslationConfig(BaseModel):
    chunk_threshold: int = 12000
    continuity_paragraphs: int = 3
    # How many EXTRA attempts a chapter gets when it fails its checks. One, because a
    # retry is told why the first attempt failed and a second identical prompt tends to
    # produce a second identical answer — past that, the money is better spent on a
    # human looking at it. Zero disables the retry entirely.
    validation_retries: int = 1
    # Per-project framing for the system prompt (genre/tone/audience). Empty means a
    # neutral "Japanese web novel". Set per project so a light novel is not framed as
    # literary fiction.
    style_note: str = ""
    # Per-project free-form instructions appended to every chapter.
    extra_instruction: str = ""
    honorific_note: str = (
        "Keep -san, -kun, -chan, -sama, -sensei and -senpai attached to names; "
        "render them as suffixes rather than translating them into English titles."
    )
    # A project's source mixes Japanese chapters with already-translated English ones.
    # Skip the English (below this fraction of Japanese script) instead of wastefully
    # translating English -> English. See morning/japanese.py for what the fraction
    # counts, and specifically for why Japanese quote marks do NOT count.
    skip_non_source: bool = True
    min_source_fraction: float = 0.15


class ValidationConfig(BaseModel):
    """Post-translation checks. Step 2 consumes these; declared now because the band
    below is the one number in this file that must NOT be carried over blind.

    Night Reader's band is 1.6-3.2, derived from real Korean/English pairs. Japanese
    and Korean are both dense relative to English but they are not equally dense, and
    kanji-heavy narration compresses differently from kana-heavy dialogue. The values
    here are a PLACEHOLDER copied from the Korean band so the shape of the config is
    settled; they are to be re-derived from real Japanese pairs before the validator
    is trusted, and the check itself is kept either way.
    """

    length_ratio_min: float = 1.6
    length_ratio_max: float = 3.2
    # Whether the band above has been re-derived from this language pair yet. The
    # validator reads it and reports "not yet calibrated" rather than failing chapters
    # against a band borrowed from another language.
    ratio_band_calibrated: bool = False
    paragraph_tolerance: int = 2           # absolute floor
    paragraph_tolerance_pct: float = 0.05  # or this fraction of source paragraphs, whichever is larger
    dialogue_tolerance: int = 3


class Config(BaseModel):
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)

    @classmethod
    def load(cls, path: str | Path = "config.toml") -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}. Copy config.example.toml to {path} "
                f"and edit it."
            )
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        return cls.model_validate(data)
