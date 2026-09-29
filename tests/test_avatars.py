"""Verify bounded avatar IO, cancellation, cache expiry, and cleanup without a CDN."""

import asyncio
import os
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot_plugin_catlottery.avatars import AvatarCache
from PIL import Image


class Response:
    """Model the streaming HTTP boundary while retaining real image decoding."""

    def __init__(self, data, status=200, gate=None):
        self.data, self.status, self.gate = data, status, gate
        self.content = self
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        return self

    async def __aexit__(self, *args):
        return False

    async def iter_chunked(self, size):
        for index in range(0, len(self.data), size):
            yield self.data[index : index + size]


@pytest.fixture
def avatar_png():
    output = BytesIO()
    with Image.new("RGBA", (200, 200), (240, 160, 180, 128)) as image:
        image.save(output, "PNG")
    return output.getvalue()


async def test_shared_fetch_survives_cancelled_caller_and_preserves_cache(
    tmp_path, avatar_png
):
    cache = AvatarCache(tmp_path, Mock())
    gate = asyncio.Event()
    cache.session = SimpleNamespace(
        closed=False,
        get=Mock(return_value=Response(avatar_png, gate=gate)),
        close=AsyncMock(),
    )
    first = asyncio.create_task(cache.get("4444444"))
    second = asyncio.create_task(cache.get("4444444"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    gate.set()
    path = await second
    assert not cache.pending
    assert await cache.get("4444444") == path
    assert cache.session.get.call_count == 1
    args, kwargs = cache.session.get.call_args
    assert args == ("https://q1.qlogo.cn/g",)
    assert kwargs["allow_redirects"] is False
    assert kwargs["params"]["nk"] == "4444444"
    with Image.open(path) as image:
        assert image.format == "JPEG" and image.size == (160, 160)
        assert not image.getexif()
    await cache.close()
    assert cache.session is None


@pytest.mark.parametrize(
    "data,status",
    [(b"bad", 200), (b"x" * (513 * 1024), 200), (b"", 302)],
    ids=["invalid", "oversized", "redirect"],
)
async def test_failed_images_have_bounded_cooldown_and_identity_fallback(
    tmp_path, data, status
):
    cache = AvatarCache(tmp_path, Mock())
    cache.session = SimpleNamespace(
        closed=False, get=Mock(return_value=Response(data, status)), close=AsyncMock()
    )
    assert await cache.get("4444444") is None
    assert await cache.get("4444444") is None
    assert cache.session.get.call_count == 1
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob("*.jpg"))
    cache.logger.warning.assert_called_once()
    await cache.close()


async def test_clear_invalidates_inflight_writes_and_cleanup_enforces_ttl_and_cap(
    tmp_path, avatar_png
):
    cache = AvatarCache(tmp_path, Mock())
    gate = asyncio.Event()
    cache.session = SimpleNamespace(
        closed=False,
        get=Mock(return_value=Response(avatar_png, gate=gate)),
        close=AsyncMock(),
    )
    request = asyncio.create_task(cache.get("4444444"))
    await cache.session.get.return_value.entered.wait()
    await cache.cleanup(clear=True)
    gate.set()
    assert await request is None
    assert not list(tmp_path.glob("*.jpg"))
    expired = tmp_path / "1111111.jpg"
    expired.write_bytes(b"expired")
    os.utime(expired, (time.time() - 26 * 3600,) * 2)
    for index in range(1001):
        (tmp_path / f"{5000000 + index}.jpg").write_bytes(b"cached")
    stale = tmp_path / "stale.part"
    stale.write_bytes(b"partial")
    os.utime(stale, (time.time() - 7200,) * 2)
    assert await cache.cleanup() == 3
    assert len(list(tmp_path.glob("*.jpg"))) == 1000
    assert not expired.exists() and not stale.exists()
    assert await cache.cleanup(clear=True) == 1000
    await cache.close()


@pytest.mark.parametrize(
    "user", ["../4444444", "https://example.com", "00000", "4444", None]
)
async def test_only_numeric_qq_id_can_reach_avatar_transport(tmp_path, user):
    cache = AvatarCache(tmp_path, Mock())
    with pytest.raises(ValueError):
        await cache.get(user)
    assert cache.session is None
