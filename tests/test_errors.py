"""Tests for the failure explainer.

Its whole job is that a user sees something they can act on instead of the words
"Internal Server Error" or an eighty-line traceback. Two properties matter more than
any individual rule and are pinned first: it never raises, and its rules are ordered
specific-before-broad.
"""

from __future__ import annotations

import json

import pytest

from morning.exceptions import RateLimited, RateLimitInfo, TaskAborted, TaskRefused
from server import errors


def _explain(exc: BaseException) -> errors.Explained:
    return errors.explain(exc)


# ---- it never raises ---------------------------------------------------------

def test_an_unrecognised_error_still_explains():
    class SomethingNew(Exception):
        pass

    e = _explain(SomethingNew("never seen before"))

    assert e.code == "unknown"
    assert e.title and e.what and e.fixes
    assert "SomethingNew" in e.detail


def test_an_exception_whose_str_raises_is_still_explained():
    """``str(exc)`` can itself fail — a custom ``__str__``, or a lazily formatted
    message. A module that exists to make failures legible must not add one."""
    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("cannot render me")

    e = _explain(Unprintable())

    assert e.code
    assert "unprintable" in e.detail.lower()


def test_an_exception_with_a_broken_traceback_is_still_explained():
    class Weird(Exception):
        pass

    exc = Weird("x")
    exc.__traceback__ = None
    e = _explain(exc)

    assert e.title
    assert e.trace or e.detail


def test_every_explanation_is_json_serialisable():
    """It is sent to the browser and embedded in job events, so a value that cannot
    be serialised would break the stream rather than the request."""
    for exc in (TaskAborted("x"), RateLimited(), PermissionError(32, "in use"),
                ValueError("bad"), OSError("no space left on device"),
                json.JSONDecodeError("Expecting value", "", 0)):
        json.dumps(errors.as_dict(_explain(exc)))


# ---- the queue's own outcomes come first -------------------------------------

def test_a_stop_is_not_reported_as_a_crash():
    """Misreporting a deliberate Stop as a failure is how a user stops trusting the
    Stop button."""
    e = _explain(TaskAborted("stopped"))

    assert e.code == "stopped"
    assert e.status == 409
    assert "nothing was overwritten" in e.what.lower()


def test_a_refusal_says_nothing_changed():
    e = _explain(TaskRefused("there was nothing to correct"))

    assert e.code == "refused"
    assert e.retryable is False


def test_a_rate_limit_reassures_that_progress_is_kept():
    e = _explain(RateLimited(RateLimitInfo(resets_at=123.0)))

    assert e.code == "rate-limited"
    assert e.status == 429
    assert "nothing is lost" in e.what.lower()
    assert e.action == errors.ACTION_SETTINGS


def test_a_rate_limit_is_recognised_from_its_message_too():
    """Not every provider raises a typed error; some just say so."""
    assert _explain(RuntimeError("You have hit your usage limit")).code == "rate-limited"


# ---- ordering: specific before broad -----------------------------------------

def test_disk_full_is_matched_before_the_generic_file_error():
    """Both are OSError. Telling someone to close a program when their disk is full
    sends them in the wrong direction entirely."""
    e = _explain(OSError(28, "No space left on device"))

    assert e.code == "disk-full"
    assert e.status == 507


def test_a_sharing_violation_is_matched_before_the_generic_file_error():
    e = _explain(PermissionError(32, "The process cannot access the file because it "
                                     "is being used by another process"))

    assert e.code == "file-locked"
    assert e.retryable is True
    assert any("sync" in f.lower() or "antivirus" in f.lower() or "close" in f.lower()
               for f in e.fixes)


def test_a_full_disk_reported_as_a_permission_error_still_says_disk_full():
    """The disk-full rule is tested before the sharing-violation one for exactly this
    overlap — PermissionError carrying an ENOSPC message."""
    e = _explain(PermissionError("[Errno 28] No space left on device"))

    assert e.code == "disk-full"


def test_a_plain_file_error_falls_through_to_the_generic_rule():
    assert _explain(OSError("something went wrong with the disk")).code == "file-error"


def test_corrupt_data_points_at_the_quarantine_copy():
    """The recovery path only helps if the user is told it exists."""
    e = _explain(json.JSONDecodeError("Expecting value", "", 0))

    assert e.code == "corrupt-data"
    assert any("unreadable" in f for f in e.fixes)


def test_a_missing_file_is_a_404_not_a_500():
    assert _explain(FileNotFoundError("source.json")).status == 404


# ---- the Claude CLI rules ----------------------------------------------------

def test_a_startup_timeout_is_reported_as_transient():
    e = _explain(Exception("Control request timeout: initialize"))

    assert e.code == "claude-start-timeout"
    assert e.retryable is True
    assert e.action == errors.ACTION_RETRY


def test_a_missing_cli_tells_the_user_how_to_install_it():
    class CLINotFoundError(Exception):
        pass

    e = _explain(CLINotFoundError("claude code not found"))

    assert e.code == "claude-missing"
    assert any("npm install" in f for f in e.fixes)
    assert e.retryable is False


def test_a_dropped_connection_is_retryable():
    class CLIConnectionError(Exception):
        pass

    assert _explain(CLIConnectionError("lost it")).retryable is True


# ---- programmer errors -------------------------------------------------------

def test_a_shape_mismatch_is_named_as_a_bug_not_the_users_fault():
    e = _explain(KeyError("source_hash"))

    assert e.code == "bad-data"
    assert "bug" in e.what.lower()


# ---- the HTTP wrapper --------------------------------------------------------

def test_a_hand_written_http_message_becomes_the_title_verbatim():
    """Those messages are good copy. The point of wrapping them is only that the
    frontend gets ONE payload shape to parse."""
    e = errors.from_http_detail("That project does not exist.", 404)

    assert e.title == "That project does not exist."
    assert e.code == "not-found"
    assert e.status == 404


def test_an_already_explained_detail_passes_through():
    original = errors.Explained(code="rate-limited", title="Limit reached", status=429)
    back = errors.from_http_detail(errors.as_dict(original), 429)

    assert back.code == "rate-limited"
    assert back.status == 429


def test_an_unexpected_detail_shape_still_produces_an_explanation():
    e = errors.from_http_detail({"unexpected": "shape"}, 400)

    assert e.code == "bad-request"
    assert e.title


def test_statuses_map_to_stable_codes():
    for status, code in ((400, "bad-request"), (403, "forbidden"), (404, "not-found"),
                         (409, "conflict"), (413, "too-large"), (429, "rate-limited")):
        assert errors.from_http_detail("x", status).code == code
    assert errors.from_http_detail("x", 418).code == "request-failed"


def test_only_the_transient_statuses_are_marked_retryable():
    assert errors.from_http_detail("x", 429).retryable is True
    assert errors.from_http_detail("x", 503).retryable is True
    assert errors.from_http_detail("x", 404).retryable is False


# ---- logging -----------------------------------------------------------------

def test_logging_writes_the_traceback_to_a_file_and_never_raises(tmp_path,
                                                                 monkeypatch):
    """Splitting the compact line from the traceback is the point: the console stays
    readable while the detail is still there when something needs diagnosing."""
    monkeypatch.setattr(errors, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(errors, "LOG_FILE", tmp_path / "logs" / "errors.log")

    errors.log_error(_explain(ValueError("something specific")), where="/api/test")

    written = (tmp_path / "logs" / "errors.log").read_text(encoding="utf-8")
    assert "bad-data" in written
    assert "/api/test" in written
    assert "ValueError" in written


def test_logging_survives_an_unwritable_directory(tmp_path, monkeypatch):
    """Logging must never break the response it is describing."""
    blocked = tmp_path / "blocked"
    blocked.write_text("I am a file, not a directory", encoding="utf-8")
    monkeypatch.setattr(errors, "LOG_DIR", blocked)
    monkeypatch.setattr(errors, "LOG_FILE", blocked / "errors.log")

    errors.log_error(_explain(ValueError("x")), where="/api/test")  # must not raise


def test_the_log_is_capped_rather_than_growing_forever(tmp_path, monkeypatch):
    monkeypatch.setattr(errors, "LOG_DIR", tmp_path)
    log = tmp_path / "errors.log"
    monkeypatch.setattr(errors, "LOG_FILE", log)
    monkeypatch.setattr(errors, "_LOG_MAX_BYTES", 2000)
    log.write_text("x" * 5000, encoding="utf-8")

    errors.log_error(_explain(ValueError("x")))

    assert log.stat().st_size < 5000


@pytest.mark.parametrize("exc", [
    TaskAborted("x"), TaskRefused("x"), RateLimited(),
    PermissionError(32, "in use"), OSError(28, "No space left on device"),
    FileNotFoundError("x"), ValueError("x"), RuntimeError("x"),
])
def test_every_explanation_has_a_title_and_a_way_forward(exc):
    """An explanation with no next step is just a nicer-looking dead end."""
    e = _explain(exc)

    assert e.title
    assert e.fixes, f"{type(exc).__name__} left the user with nothing to do"
    assert 400 <= e.status <= 599
