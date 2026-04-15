"""Perceptual image hashing (dHash) for duplicate detection."""

from __future__ import annotations

import io
import logging

from PIL import Image

logger = logging.getLogger(__name__)

DHASH_DEFAULT_SIZE = 8
DHASH_SIMILARITY_THRESHOLD = 10


async def compute_dhash(bot, file_id: str, *, size: int = DHASH_DEFAULT_SIZE) -> int:
    """Download a photo thumbnail and compute its difference hash.

    The image is downloaded into memory (no temp files), resized to
    (size+1) x size grayscale, and adjacent pixel intensities are compared
    to produce a ``size*size``-bit fingerprint.
    """
    tg_file = await bot.get_file(file_id)
    raw = await tg_file.download_as_bytearray()
    img = Image.open(io.BytesIO(raw)).convert("L").resize((size + 1, size))
    pixels = list(img.get_flattened_data())

    hash_value = 0
    for row in range(size):
        for col in range(size):
            left = pixels[row * (size + 1) + col]
            right = pixels[row * (size + 1) + col + 1]
            if left > right:
                hash_value |= 1 << (row * size + col)
    return hash_value


def hamming_distance(hash1: int, hash2: int) -> int:
    """Return the number of differing bits between two integer hashes."""
    return bin(hash1 ^ hash2).count("1")


def find_similar(
    target_hash: int,
    existing_hashes: list[tuple[int, int]],
    *,
    threshold: int = DHASH_SIMILARITY_THRESHOLD,
) -> int | None:
    """Find the first fingerprint ID whose hash is within *threshold* of *target_hash*.

    *existing_hashes* is a list of ``(fingerprint_id, dhash_int)`` tuples.
    Returns the fingerprint ID of the first match, or ``None``.
    """
    for fp_id, h in existing_hashes:
        if hamming_distance(target_hash, h) <= threshold:
            return fp_id
    return None
