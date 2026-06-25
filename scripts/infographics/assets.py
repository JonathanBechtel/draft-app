"""Player faces for infographics: fetch -> square-crop -> downscale -> base64.

Embedding the photos as data URIs keeps the output HTML fully self-contained,
so it screenshots identically anywhere with no external requests (the live photo
URLs come straight from the recap data / image_assets_service).
"""

from __future__ import annotations

import base64
import io
import urllib.request
from typing import Iterable

from PIL import Image


def _thumb(raw: bytes, size: int) -> bytes:
    """Center-crop to a square and downscale to a small JPEG (flattened on white)."""
    im = Image.open(io.BytesIO(raw)).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    im = im.crop(
        (
            (w - side) // 2,
            (h - side) // 2,
            (w - side) // 2 + side,
            (h - side) // 2 + side,
        )
    ).resize((size, size), Image.Resampling.LANCZOS)
    bg = Image.new("RGB", (size, size), (255, 255, 255))
    bg.paste(im, mask=im.split()[-1])
    out = io.BytesIO()
    bg.save(out, "JPEG", quality=82)
    return out.getvalue()


def _data_uri(jpeg: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def build_faces(picks: Iterable[dict], size: int = 150) -> dict[int, str]:
    """Map ``player_id`` -> base64 JPEG data URI for every pick with a photo.

    Missing or unreachable photos are simply skipped; templates fall back to a
    blank avatar so one bad URL never breaks the render.
    """
    faces: dict[int, str] = {}
    for p in picks:
        pid, url = p.get("player_id"), p.get("photo_url")
        if not pid or not url or pid in faces:
            continue
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                raw = resp.read()
            faces[pid] = _data_uri(_thumb(raw, size))
        except Exception:
            continue
    return faces
