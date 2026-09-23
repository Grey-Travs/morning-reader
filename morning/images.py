"""What an uploaded file actually is, and how big it is — from its own bytes.

Two questions, both answered by reading a header rather than by trusting anyone.

**The format.** A ``Content-Type`` header is advisory and a filename is a guess. The
bytes decide, and the extension a page is stored under is derived from what the file
IS, never from what it was called.

**The dimensions.** The page-read contract stores boxes as fractions of the page, so
something has to know the page's pixel size to convert a model's answer into one.
``morning/pageread.py`` left that open with a recommendation; this is it.

The alternatives were Pillow or measuring in the browser. Pillow is a large dependency
for four integers, and it decodes the whole image to get them. Measuring client-side
means the server believes a number the client sent — and a wrong one silently moves
every box on the page. JPEG, PNG and WebP all carry their dimensions within the first
few hundred bytes, so reading them here costs nothing, adds no dependency, and cannot
be lied to.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

# A phone photo of a book page is ~2-8 MB; 25 MB leaves room for a high-resolution
# scan without letting one request balloon the process.
MAX_IMAGE_BYTES = 25 * 1024 * 1024

# Enough of the file to answer both questions. A JPEG's size marker sits after its
# comment and colour-profile segments, which can be large — 64 KB covers what is
# realistic without reading a whole scan into memory just to measure it.
HEADER_BYTES = 64 * 1024

SUPPORTED = ("jpg", "png", "webp")


@dataclass
class ImageInfo:
    """What the bytes say. ``width``/``height`` are 0 when they could not be read —
    the page is still storable, it just cannot anchor an overlay until it is
    re-scanned."""

    kind: str = ""
    width: int = 0
    height: int = 0

    @property
    def ok(self) -> bool:
        return bool(self.kind)

    @property
    def measured(self) -> bool:
        return self.width > 0 and self.height > 0


def sniff_kind(head: bytes) -> str | None:
    """The file extension for a supported image, or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def unsupported_reason(head: bytes) -> str:
    """A human explanation for a rejected upload.

    Worth the specificity: "that file isn't an image" is true of a HEIC photo and
    completely unhelpful, because the person took it with the camera they own and has
    no idea their phone chose a format browsers cannot display.
    """
    # ISO base-media container: HEIC/HEIF (the iPhone default) and friends.
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return ("That looks like an iPhone HEIC photo, which browsers cannot "
                    "display. On your iPhone: Settings → Camera → Formats "
                    "→ Most Compatible, then re-take or re-export the photos as "
                    "JPEG.")
        return "That looks like a video, not a page image. Upload a photo or a scan."
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "GIF is not supported. Save the page as JPEG or PNG."
    if head.startswith(b"%PDF"):
        return ("That is a PDF, not an image. Export its pages as JPEG or PNG and "
                "upload those.")
    if head.startswith(b"BM"):
        return "BMP is not supported. Save the page as JPEG or PNG."
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "TIFF is not supported. Save the page as JPEG or PNG."
    return "That file is not a JPEG, PNG, or WebP image."


# ---- dimensions --------------------------------------------------------------

def _png_size(data: bytes) -> tuple[int, int]:
    """PNG puts IHDR first, always, and its first two fields are the dimensions."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        return 0, 0
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


# JPEG markers that carry no frame header. Everything else in the C0-CF range is a
# Start Of Frame, whichever compression it uses — baseline, progressive, lossless or
# arithmetic — and they all lay the size out the same way.
_JPEG_NON_SOF = {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes) -> tuple[int, int]:
    """Walk the segment chain to the frame header.

    A JPEG's size is not at a fixed offset: EXIF, an embedded thumbnail and a colour
    profile all come first and can run to tens of kilobytes. So the segments are
    walked rather than guessed at, which is also what makes this correct for a photo
    straight off a phone.
    """
    i = 2  # past the SOI marker
    end = len(data)
    while i + 3 < end:
        if data[i] != 0xFF:
            # Fill bytes are legal between segments; skip them rather than giving up.
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2  # standalone markers carry no length
            continue
        if marker == 0xDA:
            break  # start of scan: the image data begins, no header follows
        if i + 3 >= end:
            break
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in _JPEG_NON_SOF:
            if i + 9 > end:
                return 0, 0
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return int(width), int(height)
        if length < 2:
            break  # malformed; a zero-length segment would loop forever
        i += 2 + length
    return 0, 0


def _webp_size(data: bytes) -> tuple[int, int]:
    """WebP has three container variants and they store the size three ways.

    The length needed differs per variant, so it is checked per variant. A single
    blanket minimum rejected a valid lossless file, whose header ends eight bytes
    earlier than a lossy one's — caught by a test built from a minimal chunk.
    """
    if len(data) < 16:
        return 0, 0
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:  # lossy
        if data[23:26] != b"\x9d\x01\x2a":    # the VP8 keyframe start code
            return 0, 0
        width, height = struct.unpack("<HH", data[26:30])
        return int(width & 0x3FFF), int(height & 0x3FFF)
    if chunk == b"VP8L" and len(data) >= 25:  # lossless: 14 bits each, packed
        bits = struct.unpack("<I", data[21:25])[0]
        return int((bits & 0x3FFF) + 1), int(((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X" and len(data) >= 30:  # extended: 24-bit LE, minus one
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return 0, 0


_SIZERS = {"png": _png_size, "jpg": _jpeg_size, "webp": _webp_size}


def inspect(data: bytes) -> ImageInfo:
    """Identify and measure an upload from its bytes. Never raises.

    A file this cannot measure is still returned as a valid image when the format is
    recognised: losing a page because its header was unusual would be worse than
    storing it unmeasured and asking for a re-scan if an overlay is ever needed.
    """
    if not data:
        return ImageInfo()
    kind = sniff_kind(data)
    if kind is None:
        return ImageInfo()
    try:
        width, height = _SIZERS[kind](data)
    except (struct.error, IndexError, ValueError):
        width = height = 0
    # A dimension that cannot be true is the same as one that could not be read.
    # Guessing wrong here moves every box on the page.
    if width <= 0 or height <= 0 or width > 200_000 or height > 200_000:
        width = height = 0
    return ImageInfo(kind=kind, width=width, height=height)
