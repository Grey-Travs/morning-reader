"""A manga chapter's structure and its storage.

Two things are being pinned here, and both are placement decisions rather than
algorithms:

* **A manga chapter is a RANGE OF PAGES**, never a string of prose. Its art never
  becomes text, so there is nothing for ``source.json`` to hold.
* **Anything a human decided is a SIBLING of ``read``, never a key inside it.**
  ``jobs._apply_page_result`` merges the page reader's output with
  ``record.update(fields)`` and those fields carry the whole new ``read``, so anything
  stored inside it is destroyed by the next re-read. A test below reproduces exactly
  that merge and asserts the siblings survive it.
"""

from __future__ import annotations

import pytest

from morning.page_build import assemble_spans
from morning.pageread import KIND_BUBBLE, KIND_PAGE_NUMBER, Region, region_hash
from server import pages as pages_mod

Q = "もう無理だって"
A = "……そう?"
LATER = "彼女はホームに立っていた。"


def region(rid: str, text: str, order: int, *, kind: str = KIND_BUBBLE,
           box=(0.1, 0.1, 0.2, 0.2)) -> dict:
    return Region(id=rid, box=box, text=text, kind=kind, order=order).to_dict()


def page(seq: int, *, regions: list[dict] | None = None, status: str = "ok",
         join: str = "", heading: str | None = None, **extra) -> dict:
    return {
        "id": f"p{seq:04d}", "seq": seq, "name": f"page-{seq}.jpg", "status": status,
        "width": 1600, "height": 2400, "join_prev": join,
        "read": {"width": 1600, "height": 2400,
                 "regions": regions if regions is not None else [region("r0", Q, 0)],
                 "order_source": "model",
                 "meta": {"confidence": "high", "heading": heading,
                          "starts_mid_sentence": False, "ends_mid_sentence": False,
                          "ends_mid_word": False, "notes": []}},
        **extra,
    }


# ---- a chapter is a run of pages ---------------------------------------------

class TestSpans:
    def test_a_book_with_no_seam_is_one_chapter_of_every_page(self):
        """The right answer for a single-volume scan, and it needs no input from
        anyone."""
        built = assemble_spans([page(1), page(2), page(3)])

        assert len(built.spans) == 1
        assert built.spans[0].start_seq == 1 and built.spans[0].end_seq == 3
        assert built.spans[0].page_ids == ["p0001", "p0002", "p0003"]

    def test_a_chapter_seam_starts_a_new_one(self):
        built = assemble_spans([page(1), page(2), page(3, join="chapter"), page(4)])

        assert [(s.start_seq, s.end_seq) for s in built.spans] == [(1, 2), (3, 4)]

    def test_a_seam_on_the_first_page_does_not_make_an_empty_chapter(self):
        built = assemble_spans([page(1, join="chapter"), page(2)])
        assert len(built.spans) == 1

    def test_a_printed_heading_names_the_chapter(self):
        built = assemble_spans([page(1, heading="第1話 朝の駅"), page(2)])
        assert built.spans[0].title == "第1話 朝の駅"

    def test_a_chapter_with_no_heading_is_numbered(self):
        built = assemble_spans([page(1), page(2, join="chapter")])
        assert [s.title for s in built.spans] == ["Chapter 1", "Chapter 2"]

    def test_an_unchecked_page_is_still_in_the_book(self):
        """The novel path excludes `needs-check`, because once prose is concatenated
        there is no way to point at the un-reviewed part again. In a manga every line
        stays bolted to its page and its box, so it CAN be pointed at — and excluding
        it would put a hole in the middle of the book instead."""
        built = assemble_spans([page(1), page(2, status="needs-check"), page(3)])
        assert built.spans[0].page_ids == ["p0001", "p0002", "p0003"]

    def test_a_wordless_page_is_still_in_the_book(self):
        """A page with no bubbles reads as zero regions and is frequently the climax.
        Dropping it is a scene the reader never sees."""
        built = assemble_spans([page(1), page(2, regions=[]), page(3)])
        assert len(built.spans[0].page_ids) == 3

    def test_a_page_marked_not_text_is_left_out(self):
        built = assemble_spans([page(1), page(2, status="skipped"), page(3)])
        assert built.spans[0].page_ids == ["p0001", "p0003"]
        assert built.pages_used == 2

    def test_a_missing_page_is_reported(self):
        built = assemble_spans([page(1), page(2, join="gap")])
        assert built.warnings and "may be missing" in built.warnings[0]

    def test_nothing_at_all_builds_nothing(self):
        assert assemble_spans([]).spans == []

    def test_a_span_serialises_for_the_manifest(self):
        span = assemble_spans([page(1), page(2)]).spans[0]
        assert set(span.to_dict()) == {"index", "title", "start_seq", "end_seq",
                                       "page_ids"}


# ---- the human's reading order, stored beside the read -----------------------

class TestTheHumanOrder:
    @staticmethod
    def _two() -> dict:
        return page(1, regions=[region("r0", Q, 0), region("r1", A, 1)])

    def test_with_no_saved_order_the_model_s_order_stands(self):
        read, note = pages_mod.effective_read(self._two())
        assert [r.text for r in read.in_order()] == [Q, A]
        assert note == "" and read.order_source == "model"

    def test_a_saved_order_is_applied_and_marked_as_the_human_s(self):
        record = self._two()
        assert pages_mod.set_order(record, ["r1", "r0"])

        read, note = pages_mod.effective_read(record)

        assert [r.text for r in read.in_order()] == [A, Q]
        assert read.order_source == "user" and note == ""

    def test_it_is_stored_as_words_not_as_ids(self):
        """Ids are not an identity: `ocr._region_from` assigns them from the model's
        list position, so a re-read can hand the same bubble a different one."""
        record = self._two()
        pages_mod.set_order(record, ["r1", "r0"])
        assert record["order"]["texts"] == [A, Q]

    def test_it_survives_a_re_read_that_renumbered_everything(self):
        record = self._two()
        pages_mod.set_order(record, ["r1", "r0"])

        # The same two bubbles, detected in the other order — so the ids have swapped.
        record["read"]["regions"] = [region("r0", A, 0), region("r1", Q, 1)]

        read, note = pages_mod.effective_read(record)
        assert [r.text for r in read.in_order()] == [A, Q]
        assert note == ""

    def test_it_survives_the_merge_a_re_read_actually_performs(self):
        """`jobs._apply_page_result` does `record.update(fields)` where fields carry
        the whole new `read`. This is that merge, and the point of the sibling keys."""
        record = self._two()
        pages_mod.set_order(record, ["r1", "r0"])
        pages_mod.set_line(record, "r0", english="No more.", source_hash="x")

        record.update({"read": {"width": 1600, "height": 2400,
                                "regions": [region("r0", Q, 0), region("r1", A, 1)],
                                "order_source": "model", "meta": {}},
                       "status": "ok"})

        assert record["order"]["texts"] == [A, Q]
        assert record["lines"]["r0"]["english"] == "No more."

    def test_a_page_that_now_says_something_else_falls_back_and_says_so(self):
        """Forcing a human's ordering onto bubbles they never saw is worse than having
        no order, because it looks decided."""
        record = self._two()
        pages_mod.set_order(record, ["r1", "r0"])
        record["read"]["regions"] = [region("r0", "まったく違う", 0),
                                     region("r1", A, 1)]

        read, note = pages_mod.effective_read(record)

        assert read.order_source == "model"
        assert "no longer applies" in note

    def test_a_partial_order_is_refused_rather_than_applied(self):
        record = self._two()
        assert not pages_mod.set_order(record, ["r1"])
        assert record.get("order") is None

    def test_an_order_naming_a_region_that_is_not_there_is_refused(self):
        record = self._two()
        assert not pages_mod.set_order(record, ["r0", "r9"])

    def test_clearing_it_returns_the_page_to_the_model_s_order(self):
        record = self._two()
        pages_mod.set_order(record, ["r1", "r0"])

        assert pages_mod.set_order(record, None)

        read, _ = pages_mod.effective_read(record)
        assert [r.text for r in read.in_order()] == [Q, A]
        assert read.order_source == "model"


class TestTheOrderCheck:
    @staticmethod
    def _grid(order: list[str]) -> dict:
        position = {rid: i for i, rid in enumerate(order)}
        return page(1, regions=[
            region("tl", "a", position["tl"], box=(0.05, 0.05, 0.40, 0.40)),
            region("tr", "b", position["tr"], box=(0.55, 0.05, 0.40, 0.40)),
            region("bl", "c", position["bl"], box=(0.05, 0.55, 0.40, 0.40)),
            region("br", "d", position["br"], box=(0.55, 0.55, 0.40, 0.40)),
        ])

    def test_a_backwards_page_is_caught(self):
        """The one failure nobody can see: a model that went left-to-right. Every
        sentence stays fluent English and the page is backwards."""
        check = pages_mod.order_check(self._grid(["tl", "tr", "bl", "br"]))
        assert check["looks_reversed"] is True
        assert check["disagreement"] == 4

    def test_a_correct_page_is_not(self):
        check = pages_mod.order_check(self._grid(["tr", "tl", "br", "bl"]))
        assert check["looks_reversed"] is False
        assert check["disagreement"] == 0

    def test_it_reports_whose_order_is_in_force(self):
        record = self._grid(["tl", "tr", "bl", "br"])
        assert pages_mod.order_check(record)["order_source"] == "model"
        pages_mod.set_order(record, ["tr", "tl", "br", "bl"])
        assert record["order_check"]["order_source"] == "user"
        assert record["order_check"]["looks_reversed"] is False


# ---- what has English on it --------------------------------------------------

class TestLineCounts:
    @staticmethod
    def _page() -> dict:
        return page(1, regions=[region("r0", Q, 0), region("r1", A, 1)])

    def test_an_untranslated_page_counts_its_lines_and_nothing_else(self):
        counts = pages_mod.line_counts(self._page())
        assert counts == {"lines": 2, "translated": 0, "stale": 0,
                          "untranslated": 2, "undrawable": 0}

    def test_a_translated_line_counts_when_its_hash_still_matches(self):
        record = self._page()
        digest = region_hash(Region(id="r0", box=(0, 0, 0, 0), text=Q))
        pages_mod.set_line(record, "r0", english="No more.", source_hash=digest)

        counts = pages_mod.line_counts(record)
        assert counts["translated"] == 1 and counts["untranslated"] == 1

    def test_a_line_whose_japanese_changed_goes_stale(self):
        record = self._page()
        pages_mod.set_line(record, "r0", english="No more.", source_hash="stale")

        counts = pages_mod.line_counts(record)
        assert counts["stale"] == 1 and counts["translated"] == 0

    def test_a_line_with_no_hash_at_all_is_stale_not_fresh(self):
        """Fails CLOSED, uniquely in this file. Failing open would show paid-for
        English on a bubble whose Japanese has since changed, which reads as correct
        and cannot be spotted by looking at it."""
        record = self._page()
        pages_mod.set_line(record, "r0", english="No more.")
        assert pages_mod.line_counts(record)["stale"] == 1

    def test_a_line_record_of_the_wrong_shape_is_stale_not_fresh(self):
        record = self._page()
        record["lines"] = {"r0": "just a string"}
        assert pages_mod.line_counts(record)["untranslated"] == 2

    def test_an_empty_english_does_not_count_as_translated(self):
        record = self._page()
        pages_mod.set_line(record, "r0", english="   ", source_hash="x")
        assert pages_mod.line_counts(record)["untranslated"] == 2

    def test_page_furniture_is_not_counted(self):
        record = page(1, regions=[region("r0", Q, 0),
                                  region("r1", "42", 1, kind=KIND_PAGE_NUMBER)])
        assert pages_mod.line_counts(record)["lines"] == 1

    def test_a_region_with_nowhere_to_draw_is_counted_separately(self):
        record = page(1, regions=[region("r0", Q, 0, box=(0, 0, 0, 0))])
        counts = pages_mod.line_counts(record)
        assert counts["lines"] == 1 and counts["undrawable"] == 1

    def test_a_novel_page_counts_nothing(self):
        """Prose regions are not translatable lines, so a novel's pages leave every
        manga count at zero and the pages screen shows nothing extra."""
        from morning.pageread import KIND_BODY

        record = page(1, regions=[region("r0", "ふつうの文章", 0, kind=KIND_BODY)])
        assert pages_mod.line_counts(record)["lines"] == 0


class TestTheSummary:
    def test_it_adds_the_pages_up(self):
        doc = {"pages": [page(1), page(2)], "chapters": [], "totals": {}}
        summary = pages_mod.summary(doc)
        assert summary["lines"] == 2 and summary["untranslated"] == 2

    def test_it_counts_the_chapters(self):
        doc = {"pages": [], "chapters": [{"index": 1}, {"index": 2}], "totals": {}}
        assert pages_mod.summary(doc)["chapters"] == 2

    def test_it_counts_pages_that_look_backwards(self):
        record = page(1)
        record["order_check"] = {"looks_reversed": True, "order_source": "model"}
        assert pages_mod.summary({"pages": [record]})["reversed_pages"] == 1

    def test_but_not_ones_the_human_has_already_decided(self):
        """Nagging a page somebody has settled is what `set_join`'s user rule exists to
        prevent, and a warning that fires on decided pages teaches people to ignore it
        on the ones that matter."""
        record = page(1)
        record["order_check"] = {"looks_reversed": True, "order_source": "user"}
        assert pages_mod.summary({"pages": [record]})["reversed_pages"] == 0


class TestTheManifest:
    def test_a_new_manifest_has_a_chapter_list(self):
        assert pages_mod.new_doc()["chapters"] == []

    def test_a_manifest_written_before_step_4_still_loads(self, tmp_path, monkeypatch):
        """Every key step 4 adds defaults to absent. A page already paid for must not
        need re-reading because the app learned a new field."""
        import json

        from server import projects as pj

        monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
        (tmp_path / "abc").mkdir()
        (tmp_path / "abc" / "pages.json").write_text(json.dumps(
            {"version": 1, "next_seq": 2, "pages": [page(1)], "batches": [],
             "build": None, "totals": {"cost_usd": 0.5}}), encoding="utf-8")

        doc = pages_mod.load_pages("abc")

        assert doc["chapters"] == []
        assert doc["totals"]["cost_usd"] == 0.5
        assert pages_mod.summary(doc)["lines"] == 1

    @pytest.mark.parametrize("bad", ["not a list", 7, {"a": 1}])
    def test_a_corrupt_chapter_list_degrades_to_empty(self, bad, tmp_path,
                                                      monkeypatch):
        import json

        from server import projects as pj

        monkeypatch.setattr(pj, "PROJECTS_DIR", tmp_path)
        (tmp_path / "abc").mkdir()
        (tmp_path / "abc" / "pages.json").write_text(
            json.dumps({"pages": [], "chapters": bad}), encoding="utf-8")

        assert pages_mod.load_pages("abc")["chapters"] == []
