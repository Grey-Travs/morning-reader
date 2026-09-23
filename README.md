# Morning Reader

Japanese novels and manga, translated and read locally. A sibling to Night Reader, not
a fork of it.

**Status: step 4 of 5 — complete.** Bring in a Japanese novel by pasting it,
uploading a `.txt`, reading a Google Doc, or photographing the pages; translate it;
review what the checks flagged; approve the terms it proposed; and read it. Manga works
too: photograph the pages, read them into regions, translate a chapter in one call, and
read it with the English over the art.

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
2. **Nothing publishes.** No posting path, no site adapters, no browser extension.
   That was ~6,000 lines in Night Reader and the source of nearly every incident it
   ever had. Enforced three ways, because reading a Google Doc needs an HTTP client
   and an *import* cannot tell "read a document you own" from "post to a site":
   the client is confined to two modules; a test refuses any mutating Docs or Drive
   method name inside them; and the OAuth scopes are read-only, so a write would be
   refused at Google's end even if this code tried.
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

## Reading a stack of photographs

Add a work from *Photographs or scans* and it is created **empty** — a scanned book
has no chapters until its pages have been read. The pages screen is then the work:

1. **Add pages.** Every file is identified and measured from its own **bytes**;
   the content type is advisory and the filename is a guess. JPEG, PNG and WebP all
   carry their dimensions in the first few KB, so `morning/images.py` reads them
   without Pillow and without trusting the client. A file already in the project is
   reported as a duplicate rather than stored twice — re-uploading a folder is
   ordinary, and paying to read the same page again is not.
2. **Read them.** One page per queued item, through the same worker, the same abort,
   and the same rate-limit ride-out as a chapter. A page already read is never read
   again unless you select it.
3. **Check the ones it was unsure about.** They are *excluded from the build* until
   you look. Building with them would put un-reviewed transcription into the novel,
   which is the same mistake as reading an unaccepted translation.
4. **Fix the seams.** How each page follows the one before it — same sentence, new
   paragraph, new chapter, or *a page is missing*. The app proposes; you correct; a
   seam you decided is marked `user` and a later re-read never silently reverts it.
   Order is changed by moving a page, and the whole new order is sent, because the
   server refuses anything that is not a permutation of what it already has.
5. **Build the chapters.** After this the project is an ordinary one, and nothing
   downstream can tell it was ever a stack of photographs.

A page's filename carries a monotonic `seq`, never its position, so reordering
rewrites the manifest only — it never renames a file, which would break image caching
in the browser and race an in-flight read holding a path.

## Reading a manga

A manga forks below the region contract and shares everything above it. Add the work as
*Photographs or scans* with kind **manga**, add the pages, read them — all exactly as a
scanned novel — and then the paths diverge:

* **A manga chapter is a RUN OF PAGES**, recorded in `pages.json`. It never becomes
  prose, so a manga never writes `source.json`. That is enforced rather than
  encouraged: `POST /run` refuses `translate` and `prepare` on a manga *by name*, the
  prose reader 404s pointing at the manga one, and a manga's chapter rows deliberately
  omit the two fields (`class`, `paragraph_count`) the prose UI keys on, so a row
  structurally cannot be offered the wrong button.
* **Its chapters include every non-skipped page**, including ones you have not checked.
  The novel rule excludes those because once prose is concatenated there is no way to
  point at the un-reviewed part again — but in a manga every line stays bolted to its
  page and its box, so it *can* be pointed at, and it is. A wordless action page has no
  text to review at all and is frequently the climax; dropping it would be a scene
  nobody ever sees.
* **The whole chapter's script is ONE call.** A bubble in isolation is frequently
  untranslatable: Japanese omits the subject, and the referent is routinely established
  on the *previous page*. A 20-page chapter is ~150 lines and a few thousand characters,
  while reading those pages already cost 20 vision calls — so translating is a few
  percent of the chapter's spend and buying more context is nearly free.
* **The voice contract is inherited, not forked.** `build_system_prompt` takes a
  swappable `output_contract`, so a manga keeps the glossary block, the `[he]`/`[she]`
  pins, the `[refers to self as 俺]` register tags and the honorific rules. Only the
  output shape changes: one tab-separated `id  speaker  English` record per line. Tab
  rather than JSON because a truncated JSON array yields *nothing*, while every
  complete record before the cut is still usable.
* **Reading order is corrected for free.** `morning/reading_order.py` recovers the
  order from the boxes with a recursive X-Y cut that reads columns *right first*. When
  it disagrees with the model in the specific way that means "this page was read
  left-to-right", the reader says so and one click applies the layout's order — no
  re-read, no bill. That failure is the reason this exists: such a page is backwards
  sentence by sentence and every sentence is still fluent English.
* **Panels are derived, never stored.** They are the leaves of the same cut. Asking the
  model for a `panel` field would have meant a paid re-read of every page already
  transcribed, to obtain something the stored boxes already imply. That trade works
  only because a wrong panel is *presentational* — it groups lines under a heading in
  the script view, and getting it wrong is visible and harmless.

### Two rules that keep paid-for work safe

**Anything a human decided is a SIBLING of `read`, never a key inside it.**
`jobs._apply_page_result` merges a finished read with `record.update(fields)`, and
those fields carry the whole new `read` — so anything stored inside it is destroyed by
the next re-read. The corrected order and the English are therefore siblings.

**A line is fresh only while its Japanese is unchanged.** `region_hash` is the per-line
analogue of `source_hash`: text and nothing else, so a nudged box or a relabelled kind
does not re-bill a line. A line that fails the check is marked stale *and kept* —
nothing here deletes prose. This one check fails **closed**, uniquely in the codebase:
a missing or malformed hash counts as stale, because failing open would show paid-for
English on a bubble whose Japanese has since changed, which reads as correct and cannot
be spotted by looking at it.

Region ids are *not* an identity — `ocr._region_from` assigns `r0..rn` from the model's
list position and the prompt never asks for an id — so a saved reading order is stored
as the region **texts** in their order. That survives a re-read that renumbered
everything. When the page no longer says the same things the order is not forced on;
the model's comes back and the reader says why.

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
.venv\Scripts\python.exe -m pytest tests/ -q     # 1068 tests
cd web && npm test                                # 91 tests
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
  images.py       format and dimensions from the header BYTES, no Pillow
  ocr.py          reading one page image into regions
  page_build.py   pages -> chapters (prose) and -> spans (manga)
  reading_order.py  reading order and panels from the boxes alone — free
  manga.py        one manga chapter end to end: collect, call, parse
  japanese.py     script detection (the easy half of the language layer)
  textsource.py   paste / .txt ingestion
  docs_source.py  Google Doc ingestion, one chapter per tab — READ ONLY
  google_auth.py  the read-only sign-in
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
  pages.py        the page manifest; order lives here, not in filenames
  errors.py       every failure becomes something a reader can act on
  console.py      the same events, rendered to the terminal
web/              Vite + React interface: Library, work page, Pages, Reader,
                  manga Reader + Script, Glossary, Activity
  geometry.js     the browser half of the box maths, pinned against Python
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
* **Source-residue detection misses two shapes on purpose**, and says so in
  `morning/sanitize.py` with passing tests either way. Noun-only residue (`本日休業`)
  and very short utterances are not caught, because a threshold low enough to catch
  them flags every kept shop sign and sound effect. Making the rule stricter later
  should therefore be a decision, not a surprise.
* **HEIC is not accepted.** Browsers cannot display it, so storing one would give a
  page with no thumbnail and no way to check the read against the image. The upload
  says so by name rather than rejecting the file as unreadable.
* **A speaker is attributed by the translation call and gated by a human.** A model's
  suggestion stays visibly a suggestion until someone confirms it, a previous run's
  guesses are never fed back into the prompt as fact, and a speaker the glossary has
  never heard of is called out. A wrong speaker reads perfectly well and quietly turns
  two characters into one.
* **Two identical bubbles on one page share a staleness key.** `region_hash` is over
  the text alone, so if a re-read swaps two regions whose Japanese is character-for-
  character identical, their English swaps with them and neither goes stale. Including
  the box in the hash would catch it — and would also re-bill every line on every page
  whose boxes drifted, which is every re-read. The cheaper failure was chosen
  deliberately.

## Build order

1. ~~Spine: storage, state, locks, atomic writes, job queue, errors, Activity.~~ ✔
2. ~~Novel pipeline: translate → validate → retry → audit → reader, plus the glossary
   and its pending queue. Google Docs ingestion.~~ ✔
3. ~~Page harness with the region contract; novels flatten regions.~~ ✔
4. ~~Manga: reading order, overlay reader, script view.~~ ✔
5. The Japanese site-export stripper, once real samples exist.
