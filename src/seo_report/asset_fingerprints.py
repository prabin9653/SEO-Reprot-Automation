"""Measuring the rendering-asset fingerprints of the render input (R2).

`measure_fingerprints` reads the explicitly supplied logo and font files and returns the R1 `RenderFingerprints`:
the SHA-256 of each file's exact bytes, the caller's template version and the page size. It only reads: no file is
modified, copied, renamed, deleted or created, nothing is bundled, and nothing but the supplied paths is opened.

  logo_path     one file (PNG or any local file; the bytes are hashed, not interpreted)
  font_paths    a mapping {label: path} or a sequence of paths. For a sequence the label is the file name without its
                extension ("DejaVuSans-Bold.ttf" -> "DejaVuSans-Bold"). The label, never the path, is what enters the
                fingerprint, so no machine-specific path can leak into the hash. Labels must be unique and valid for
                `render_input.FontFingerprint`
  template_version  a short label supplied by the caller (validated by R1), for example "r3.0"

The same bytes and template version always give the same fingerprint; changing a file's bytes changes that file's
fingerprint; changing the template version changes the fingerprint. Errors are `AssetFingerprintError` with a fixed
`code` and a message that names the role (logo or a font label), never a path or an operating-system message.
"""

import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import PurePath

from seo_report import render_input as ri

CHUNK = 1024 * 1024
MAX_ASSET_BYTES = 64 * 1024 * 1024


class AssetFingerprintError(ValueError):
    """An asset cannot be fingerprinted. `code` is fixed; `role` names the asset ("logo" or a font label)."""

    def __init__(self, code: str, role: str):
        super().__init__(f"{role}: {code}")
        self.code = code
        self.role = role


def sha256_of_file(path, *, role: str = "file") -> str:
    """Lowercase hexadecimal SHA-256 of the exact bytes of one regular file (read in chunks, read-only)."""
    if isinstance(path, (bytes, bytearray)) or not isinstance(path, (str, os.PathLike)):
        raise AssetFingerprintError("invalid_path", role)
    text = os.fspath(path)
    if not isinstance(text, str) or not text or "\x00" in text:
        raise AssetFingerprintError("invalid_path", role)
    try:
        info = os.stat(text)
    except FileNotFoundError:
        raise AssetFingerprintError("file_not_found", role) from None
    except OSError:
        raise AssetFingerprintError("file_unreadable", role) from None
    if not stat.S_ISREG(info.st_mode):
        raise AssetFingerprintError("not_a_regular_file", role)
    if info.st_size > MAX_ASSET_BYTES:
        raise AssetFingerprintError("file_too_large", role)
    digest, count = hashlib.sha256(), 0
    try:
        with open(text, "rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK), b""):
                digest.update(chunk)
                count += len(chunk)
    except OSError:
        raise AssetFingerprintError("file_unreadable", role) from None
    if count != info.st_size:
        raise AssetFingerprintError("file_changed_while_reading", role)
    return digest.hexdigest()


def _font_entries(font_paths):
    if isinstance(font_paths, Mapping):
        items = list(font_paths.items())
    elif isinstance(font_paths, (list, tuple)):
        items = []
        for path in font_paths:
            if isinstance(path, (bytes, bytearray)) or not isinstance(path, (str, os.PathLike)):
                raise AssetFingerprintError("invalid_path", "font")
            items.append((PurePath(os.fspath(path)).stem, path))
    else:
        raise AssetFingerprintError("invalid_font_list", "font")
    if not items:
        raise AssetFingerprintError("no_fonts", "font")
    labels = [label for label, _ in items]
    if any(not isinstance(label, str) for label in labels):
        raise AssetFingerprintError("invalid_font_label", "font")
    if len(set(labels)) != len(labels):
        raise AssetFingerprintError("duplicate_font_label", "font")
    return items


def measure_fingerprints(*, template_version, logo_path, font_paths, page_size: str = "A4") -> ri.RenderFingerprints:
    """The `RenderFingerprints` for the given template version, logo file and font files."""
    logo = sha256_of_file(logo_path, role="logo")
    fonts = []
    for label, path in _font_entries(font_paths):
        digest = sha256_of_file(path, role=label if isinstance(label, str) else "font")
        try:
            fonts.append(ri.FontFingerprint(label, digest))
        except ri.RenderInputError:
            raise AssetFingerprintError("invalid_font_label", "font") from None
    try:
        return ri.RenderFingerprints(template_version=template_version, logo_sha256=logo, fonts=tuple(fonts),
                                     page_size=page_size)
    except ri.RenderInputError as err:
        # the message of RenderInputError names a field, never a value
        raise AssetFingerprintError("invalid_fingerprint_input", str(err).split(":")[0]) from None
