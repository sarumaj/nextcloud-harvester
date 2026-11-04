# SPDX-License-Identifier: AGPL-3.0-or-later
"""Thumbnails of the private library's pictures (library.py): a small JPEG made with Pillow when
the file is written, from its plaintext in memory (pictures up to SOURCE_MOST bytes), then
encrypted like the file. A video gets none: its first frame needs ffmpeg and the plaintext in a
file on the disk, which the library never writes; the page shows an icon."""

import io
import warnings

from PIL import Image, ImageOps

# The longest side of a thumbnail, in pixels, and the most bytes of a picture one is made of.
SIZE = 320
SOURCE_MOST = 32 * 1000**2
# A picture of more pixels is not opened (a decompression bomb: a small file of a huge image).
MAX_PIXELS = 60_000_000
KINDS = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif", "image/tiff")


def wanted(media_type: str) -> bool:
    """Whether a file of this type gets a thumbnail."""
    return media_type in KINDS


def thumbnail(data: bytes) -> bytes | None:
    """A JPEG of the picture at most SIZE pixels on its longer side, turned as its EXIF says;
    None for what Pillow cannot read, or a picture of more than MAX_PIXELS pixels."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as picture:
                if picture.width * picture.height > MAX_PIXELS:
                    return None
                picture.draft("RGB", (SIZE, SIZE))
                small = ImageOps.exif_transpose(picture)
                small.thumbnail((SIZE, SIZE))
                if small.mode != "RGB":
                    small = small.convert("RGBA")
                    flat = Image.new("RGB", small.size, (255, 255, 255))
                    flat.paste(small, mask=small.getchannel("A"))
                    small = flat
                out = io.BytesIO()
                small.save(out, "JPEG", quality=80)
                return out.getvalue()
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return None
