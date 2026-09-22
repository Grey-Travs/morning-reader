# Morning Reader

Japanese novels and manga, translated and read locally. A sibling to Night Reader, not
a fork of it.

**Status: step 1 of 5 — the spine.** Project storage, state, locks, atomic writes, the
job queue, legible errors, and Activity, proved end to end with a pasted `.txt` novel.
There is no translator yet; that is step 2.

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
.venv\Scripts\python.exe -m pytest tests/ -q     # 273 tests
cd web && npm test                                # 18 tests
```

The reference app is roughly 29% tests and that is why it survives. Two bugs in this
repo were found by its tests before any of it ran for real: a one-line chapter being
silently dropped by the title-detection, and EUC-JP text being decoded as CP932 because
"the first encoding that does not raise" is the wrong algorithm when two encodings have
overlapping byte validity.

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

## What is deliberately not done yet

* **No translator.** Step 1's one task is `prepare`: segment, measure, classify. It
  makes no model call, which is exactly why it can exercise every branch of the
  worker — skip, stop, refuse, fail, succeed — without spending anything.
* **The length-ratio band is carried over from Korean and marked uncalibrated.**
  `/api/health` reports `ratio_band_calibrated: false`. Re-derive it from real Japanese
  pairs before the validator is trusted.
* **Source-residue detection is not written**, and not faked. Night Reader keeps chat
  laughter out of its detector by character range; Japanese laughter is `www` and `草`,
  which are Latin and an ordinary Kanji. That needs a different rule, not a swapped
  regex, and a half-right one that passes leaked Japanese is worse than none.
* **`width`/`height` are on the contract but nothing obtains them yet.** The
  recommendation on the record: sniff them from the image header bytes server-side —
  JPEG, PNG and WebP all carry dimensions in the first few KB — which needs no Pillow
  and does not trust the client.

## Build order

1. ~~Spine: storage, state, locks, atomic writes, job queue, errors, Activity.~~ ✔
2. Novel pipeline: translate → validate → retry → audit → reader, plus the glossary and
   its pending queue. Google Docs ingestion.
3. Page harness with the region contract; novels flatten regions.
4. Manga: reading order, overlay reader, script view.
5. The Japanese site-export stripper, once real samples exist.
