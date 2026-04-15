"""Tests for the dHash image hashing module."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from utils.image_hash import compute_dhash, find_similar, hamming_distance


def _make_solid_image(color: int, size: tuple[int, int] = (100, 100)) -> bytes:
    """Create a solid-color grayscale image and return its bytes."""
    img = Image.new("L", size, color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_gradient_image(
    start: int = 0, end: int = 255, size: tuple[int, int] = (100, 100)
) -> bytes:
    """Create a horizontal gradient image and return its bytes."""
    img = Image.new("L", size)
    for x in range(size[0]):
        val = int(start + (end - start) * x / (size[0] - 1))
        for y in range(size[1]):
            img.putpixel((x, y), val)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def _mock_bot_for_image(image_bytes: bytes) -> MagicMock:
    """Create a mock bot that returns the given image bytes from get_file."""
    bot = MagicMock()
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(image_bytes))
    bot.get_file = AsyncMock(return_value=tg_file)
    return bot


class TestHammingDistance:
    def test_identical(self) -> None:
        assert hamming_distance(0, 0) == 0
        assert hamming_distance(0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF) == 0

    def test_completely_different(self) -> None:
        assert hamming_distance(0, 0xFFFFFFFFFFFFFFFF) == 64

    def test_one_bit_different(self) -> None:
        assert hamming_distance(0, 1) == 1
        assert hamming_distance(0b1010, 0b1011) == 1

    def test_known_value(self) -> None:
        assert hamming_distance(0b11110000, 0b00001111) == 8


class TestComputeDhash:
    @pytest.mark.asyncio
    async def test_identical_images_produce_same_hash(self) -> None:
        img = _make_solid_image(128)
        bot1 = await _mock_bot_for_image(img)
        bot2 = await _mock_bot_for_image(img)
        h1 = await compute_dhash(bot1, "fid1")
        h2 = await compute_dhash(bot2, "fid1")
        assert h1 == h2

    @pytest.mark.asyncio
    async def test_similar_images_low_distance(self) -> None:
        img1 = _make_gradient_image(0, 255)
        img2 = _make_gradient_image(5, 250)
        bot1 = await _mock_bot_for_image(img1)
        bot2 = await _mock_bot_for_image(img2)
        h1 = await compute_dhash(bot1, "fid1")
        h2 = await compute_dhash(bot2, "fid2")
        assert hamming_distance(h1, h2) <= 10

    @pytest.mark.asyncio
    async def test_different_images_high_distance(self) -> None:
        img1 = _make_gradient_image(0, 255)
        img2 = _make_gradient_image(255, 0)
        bot1 = await _mock_bot_for_image(img1)
        bot2 = await _mock_bot_for_image(img2)
        h1 = await compute_dhash(bot1, "fid1")
        h2 = await compute_dhash(bot2, "fid2")
        assert hamming_distance(h1, h2) > 10

    @pytest.mark.asyncio
    async def test_hash_is_64_bit_integer(self) -> None:
        img = _make_gradient_image(0, 200)
        bot = await _mock_bot_for_image(img)
        h = await compute_dhash(bot, "fid")
        assert isinstance(h, int)
        assert 0 <= h < (1 << 64)


class TestFindSimilar:
    def test_exact_match(self) -> None:
        target = 0b1010101010
        existing = [(1, 0b1111111111), (2, 0b1010101010), (3, 0)]
        assert find_similar(target, existing, threshold=0) == 2

    def test_within_threshold(self) -> None:
        target = 0b1010101010
        close = 0b1010101011  # 1 bit different
        existing = [(1, 0b1111111111), (2, close)]
        assert find_similar(target, existing, threshold=1) == 2

    def test_no_match(self) -> None:
        target = 0
        existing = [(1, 0xFFFFFFFFFFFFFFFF)]
        assert find_similar(target, existing, threshold=10) is None

    def test_empty_list(self) -> None:
        assert find_similar(0, [], threshold=10) is None
