"""Identifying and measuring an upload from its own bytes.

The dimensions matter more than they look. The page-read contract stores every box as
a fraction of the page, so a wrong page size moves every region on it — and the two
alternatives were both worse: Pillow decodes a whole image to read four integers, and
a client-supplied number is one the server cannot check.

Headers are built by hand here rather than shipping binary fixtures. That keeps the
cases precise and legible: each one is a specific shape a real file takes.
"""

from __future__ import annotations

import struct

import pytest

from morning.images import (
    HEADER_BYTES, MAX_IMAGE_BYTES, SUPPORTED, ImageInfo, inspect, sniff_kind,
    unsupported_reason,
)


# ---- builders ----------------------------------------------------------------

def png(width: int, height: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR"
            + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"
            + b"\x00" * 4)


def jpeg(width: int, height: int, *, padding: int = 0,
         marker: bytes = b"\xff\xc0") -> bytes:
    """A JPEG up to its frame header.

    ``padding`` stands in for EXIF, an embedded thumbnail or a colour profile — the
    reason the size is not at a fixed offset and the segments have to be walked.
    """
    out = b"\xff\xd8"
    out += b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    if padding:
        out += b"\xff\xe2" + struct.pack(">H", padding + 2) + b"\x00" * padding
    out += marker + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width)
    out += b"\x03" + b"\x00" * 9
    out += b"\xff\xda" + struct.pack(">H", 12) + b"\x00" * 10
    return out


def webp_lossy(width: int, height: int) -> bytes:
    body = (b"VP8 " + struct.pack("<I", 20) + b"\x00\x00\x00" + b"\x9d\x01\x2a"
            + struct.pack("<HH", width, height) + b"\x00" * 6)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def webp_lossless(width: int, height: int) -> bytes:
    bits = ((width - 1) & 0x3FFF) | (((height - 1) & 0x3FFF) << 14)
    body = (b"VP8L" + struct.pack("<I", 9) + b"\x2f" + struct.pack("<I", bits)
            + b"\x00" * 4)
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


def webp_extended(width: int, height: int) -> bytes:
    body = (b"VP8X" + struct.pack("<I", 10) + b"\x00\x00\x00\x00"
            + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little"))
    return b"RIFF" + struct.pack("<I", len(body) + 4) + b"WEBP" + body


# ---- the format ----------------------------------------------------------------

@pytest.mark.parametrize("kind,data", [
    ("png", png(10, 10)),
    ("jpg", jpeg(10, 10)),
    ("webp", webp_lossy(10, 10)),
])
def test_every_supported_format_is_recognised(kind, data):
    assert sniff_kind(data) == kind
    assert kind in SUPPORTED


def test_the_extension_comes_from_the_bytes_not_the_name():
    """A Content-Type header is advisory and a filename is a guess. Deriving the
    stored extension from either is how a .jpg that is really a PDF gets served as an
    image and renders as nothing."""
    assert sniff_kind(png(1, 1)) == "png"
    assert sniff_kind(b"%PDF-1.7 pretending to be a jpg") is None


@pytest.mark.parametrize("data,expected", [
    (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8, "iPhone"),
    (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 8, "video"),
    (b"GIF89a" + b"\x00" * 10, "GIF"),
    (b"%PDF-1.7\n", "PDF"),
    (b"BM" + b"\x00" * 30, "BMP"),
    (b"II*\x00" + b"\x00" * 20, "TIFF"),
    (b"random bytes that are nothing", "JPEG, PNG, or WebP"),
])
def test_a_rejected_upload_is_told_what_it_actually_is(data, expected):
    """"That file isn't an image" is true of a HEIC photo and completely unhelpful:
    the person took it with the camera they own and has no idea their phone chose a
    format browsers cannot display."""
    assert sniff_kind(data) is None
    assert expected in unsupported_reason(data)


def test_the_heic_message_says_how_to_fix_it():
    reason = unsupported_reason(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8)

    assert "Most Compatible" in reason


# ---- the dimensions --------------------------------------------------------------

@pytest.mark.parametrize("label,data,width,height", [
    ("png", png(1600, 2400), 1600, 2400),
    ("jpeg baseline", jpeg(1200, 1800), 1200, 1800),
    ("jpeg progressive", jpeg(800, 600, marker=b"\xff\xc2"), 800, 600),
    ("jpeg lossless", jpeg(640, 480, marker=b"\xff\xc3"), 640, 480),
    ("webp lossy", webp_lossy(640, 480), 640, 480),
    ("webp lossless", webp_lossless(1024, 768), 1024, 768),
    ("webp extended", webp_extended(2000, 3000), 2000, 3000),
])
def test_dimensions_are_read_from_the_header(label, data, width, height):
    info = inspect(data)

    assert (info.kind, info.width, info.height) == (info.kind, width, height), label
    assert info.measured


def test_a_phone_photo_with_a_large_exif_block_is_still_measured():
    """A JPEG's size is not at a fixed offset. EXIF, an embedded thumbnail and a
    colour profile all come first and can run to tens of kilobytes, which is exactly
    what a photo straight off a phone looks like."""
    info = inspect(jpeg(3024, 4032, padding=40000))

    assert (info.width, info.height) == (3024, 4032)


def test_a_minimal_lossless_webp_is_measured():
    """Its header ends eight bytes earlier than a lossy one's. A single blanket
    minimum length rejected it — found by this test, which is why the length check is
    per variant."""
    data = webp_lossless(1024, 768)

    assert len(data) < 30
    assert inspect(data).measured


@pytest.mark.parametrize("label,data", [
    ("truncated png", png(10, 10)[:12]),
    ("truncated jpeg", jpeg(10, 10)[:6]),
    ("truncated webp", webp_lossy(10, 10)[:20]),
    ("jpeg with no frame header", b"\xff\xd8\xff\xe0" + struct.pack(">H", 16)
                                 + b"JFIF\x00" + b"\x00" * 9),
])
def test_an_unmeasurable_image_is_still_a_valid_image(label, data):
    """Losing a page because its header was unusual would be worse than storing it
    unmeasured. It can still hold text; it just cannot anchor an overlay until it is
    re-scanned."""
    info = inspect(data)

    assert info.ok, label
    assert not info.measured


def test_an_impossible_dimension_is_treated_as_unmeasured():
    """Guessing wrong here moves every box on the page, so a number that cannot be
    true is the same as one that could not be read."""
    assert not inspect(png(0, 100)).measured
    assert not inspect(png(500_000, 500_000)).measured


def test_a_malformed_jpeg_segment_chain_does_not_loop_forever():
    """A zero-length segment would advance the cursor by nothing."""
    data = b"\xff\xd8" + b"\xff\xe1" + struct.pack(">H", 0) + b"\x00" * 100

    assert inspect(data).ok
    assert not inspect(data).measured


@pytest.mark.parametrize("data", [b"", None])
def test_nothing_inspects_to_nothing(data):
    info = inspect(data or b"")

    assert info == ImageInfo()
    assert not info.ok


def test_inspect_never_raises_on_arbitrary_bytes():
    """It runs on whatever someone uploaded. A crash here is a 500 on a file the user
    could simply have been told about."""
    import os

    for _ in range(200):
        inspect(os.urandom(64))
    for prefix in (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"RIFF\x00\x00\x00\x00WEBP"):
        for _ in range(50):
            inspect(prefix + os.urandom(40))


# ---- the limits ------------------------------------------------------------------

def test_the_size_cap_allows_a_real_scan_but_not_a_runaway():
    """A phone photo of a book page is 2-8 MB; a high-resolution scan is larger."""
    assert 10 * 1024 * 1024 < MAX_IMAGE_BYTES <= 50 * 1024 * 1024


def test_the_header_slice_is_large_enough_for_a_real_photo():
    """It has to cover a JPEG's EXIF and colour profile, or the frame header falls
    outside what was read and the page stores as unmeasured."""
    assert HEADER_BYTES >= 32 * 1024
    assert inspect(jpeg(3024, 4032, padding=40000)[:HEADER_BYTES]).measured
