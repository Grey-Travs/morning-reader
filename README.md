# Morning Reader

Japanese novels and manga, translated and read locally. A sibling to Night Reader, not
a fork of it.

**Status: step 2 of 5 — the novel pipeline.** Paste or upload a Japanese `.txt`,
translate it, review what the checks flagged, approve the terms it proposed, and read
it. Google Docs ingestion is the one part of step 2 still outstanding.

---

## Why this is a separate app

Night Reader translates Korean web novels and publishes them. It works. The reason
Japanese is a new app rather than a new language inside it is not the language — it is
that `"korean"` is a **persisted value** there, not a label, in three on-disk
contracts: the classifier's return string, the glossary's entry key, and
`hangul_fraction` on every page record. Adding a second language there means migrating
those across 81 projects and ~3,967 chapter records, on a library that is currently
earning money.

So this app has three rules it does not bend:

1. **The field is `source`, never a language name.** `source_hash`, `source_fraction`,
   and the classifications `source | english | empty`. Enforced by
   `tests/test_scope_guards.py`, which fails the build on a language name in anything
   that becomes an identifier or a stored value.
2. **Nothing publishes.** No posting path, no site adapters, no browser extension, no
   outbound HTTP client — the import itself is refused by the same test file. That was
   ~6,000 lines in Night Reader and the source of nearly every incident it ever had.
3. **The page-read contract carries regions with geometry from day one**, for novels
   as well as manga. See below.

## The decision the app hinges on

`morning/pageread.py`. Reading one page image returns regions with positions, not flat
text:

```json
{ "width": 1600, "height": 2400,
  "regions": [ { "id": "r0", "box": [x, y, w, h], "text": "…",
                 "kind": "body", "order": 0,
                 "join_prev": "sentence", "join_glue": "none" } ],
  "order_source": "model",
  "meta": { "confidence": "high", "heading": null,
            "starts_mid_sentence": false, "ends_mid_sentence": true,
            "ends_mid_word": false, "notes": [] } }
```

* **Novels flatten it.** `flatten()` returns exactly the string a flat-text read would
  have produced, so the prose machinery applies unchanged and novels pay nothing for
  carrying regions. `tests/test_pageread.py` asserts that equality directly.
* **Manga keeps it.** `box` drives the overlay, `order` drives the script view.
* **Boxes are fractions of the page, never pixels**, so re-scanning at another
  resolution does not invalidate every stored position.
* **`order` is a proposal.** Right-to-left, panel-aware reading order is genuinely
  hard, so a human can correct it — and `order_source: "user"` means a later read never
  silently reverts that.

Building novels on flat text first would mean rewriting this contract *and* re-reading
every page already processed.

## Running it

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe launch.py
```

It serves on **8100** — Night Reader owns 8000 and the two are meant to run side by
side. `launch.py --reload` restarts the server on a Python change (useful while
developing; a reload would kill an in-flight job, so it is off by default).

## Tests

```bash
.venv\Scripts\python.exe -m pytest tests/ -q     # 592 tests
cd web && npm test                                # 31 tests
```

**No test makes a real model call.** The SDK's `query` is replaced by a fake async
generator yielding real SDK message objects, so the control flow under test —
streaming, aborting, retrying, rate limits, truncation — is the real control flow at
no cost.

The reference app is roughly 29% tests and that is why it survives. Several bugs in
this repo were found by its own tests before any of it ran for real: a one-line
chapter silently dropped by the title detection; EUC-JP decoded as CP932 because "the
first encoding that does not raise" is the wrong algorithm when two encodings have
overlapping byte validity; `strip_meta` deleting a whole paragraph when the model
corrected itself mid-paragraph; and the glossary allow-list applying to one residue
check but not the other, so a reader could add a kept term, watch the finding vanish,
and still have the chapter fail on the same characters.

## Layout

```
morning/          the engine — imports nothing from the web layer
  atomic.py       one correct atomic write (unique temp name PER CALL)
  locks.py        one re-entrant lock per file path
  state.py        per-item status/hash/usage — the resumability contract
  chapters.py     the Chapter contract every ingestion path converges on
  pageread.py     THE REGION CONTRACT
  japanese.py     script detection (the easy half of the language layer)
  textsource.py   paste / .txt ingestion
  glossary.py     entries with VARIANTS, and the human approval gate
  prompts.py      the Japanese prompt; the model answers under `source`
  translator.py   drives the Claude Code CLI; every tool blocked
  sanitize.py     model chatter (strip) vs untranslated source (flag)
  validate.py     the checks that decide read-or-review
  pipeline.py     one chapter end to end
  chapter_files.py  where output lands; everything resolves by INDEX
  config.py       typed config; the ratio band is NOT yet calibrated
server/
  app.py          FastAPI routes
  jobs.py         one worker per project, FIFO, abort, rate-limit ride-out
  tasks.py        what a queued item asks the worker to do
  projects.py     project storage; `kind` is novel | manga from day one
  errors.py       every failure becomes something a reader can act on
  console.py      the same events, rendered to the terminal
web/              Vite + React interface: Library, work page, Activity
```

## Two rules worth knowing before reading the code

**A chapter that validates goes to `chapters/` and is read. A chapter that does not
goes ONLY to its audit copy.** That split is the whole safety property: a questionable
translation must never be mistaken for a finished one. The audit copy is then the only
copy, which is what the reader shows (saying plainly that it is unaccepted) and what
Accept promotes.

**Nothing deletes prose.** Untranslated Japanese is FLAGGED, never removed; model
chatter is removed only when the block holds nothing else. Losing a leak is
recoverable, deleting prose is not.

## What is deliberately not done yet

* **The length-ratio band is carried over from Korean and marked uncalibrated.**
  `/api/health` reports `ratio_band_calibrated: false`, and while it is false an
  out-of-band ratio WARNS instead of failing the chapter. `metrics.length_ratio` is
  recorded on every chapter from day one — that is the data a re-derivation needs.
* **No character-gender check.** The glossary pins `pronoun` and the prompt treats
  that pin as authoritative, so the input exists; detecting a contradiction in the
  output is separate work and is not faked meanwhile.
* **Google Docs ingestion.** The remaining ingestion path from step 2.
* **Source-residue detection misses two shapes on purpose**, and says so in
  `morning/sanitize.py` with passing tests either way. Noun-only residue (`本日休業`)
  and very short utterances are not caught, because a threshold low enough to catch
  them flags every kept shop sign and sound effect. Making the rule stricter later
  should therefore be a decision, not a surprise.
* **`width`/`height` are on the contract but nothing obtains them yet.** The
  recommendation on the record: sniff them from the image header bytes server-side —
  JPEG, PNG and WebP all carry dimensions in the first few KB — which needs no Pillow
  and does not trust the client.

## Build order

1. ~~Spine: storage, state, locks, atomic writes, job queue, errors, Activity.~~ ✔
2. ~~Novel pipeline: translate → validate → retry → audit → reader, plus the glossary
   and its pending queue.~~ ✔ — except Google Docs ingestion.
3. Page harness with the region contract; novels flatten regions.
4. Manga: reading order, overlay reader, script view.
5. The Japanese site-export stripper, once real samples exist.
