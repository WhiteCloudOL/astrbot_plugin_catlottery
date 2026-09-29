"""Fetch bounded QQ avatars and retain a small expiring disk cache."""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import aiohttp
from PIL import Image, ImageOps, UnidentifiedImageError


class AvatarCache:
    """Own the HTTP session, concurrent requests, and avatar cache lifecycle."""

    def __init__(self, directory: Path, logger: Any) -> None:
        self.directory = directory
        self.logger = logger
        self.session: aiohttp.ClientSession | None = None
        self.pending: dict[str, asyncio.Task] = {}
        self.failures: dict[str, float] = {}
        self.semaphore = asyncio.Semaphore(4)
        self.generation = 0

    async def get(self, user_id: str, hours: int = 24) -> Path | None:
        """Read a fresh avatar or share one bounded request with concurrent callers.

        Args:
            user_id: Actual numeric QQ identifier, never a URL or path.
            hours: Cache lifetime from plugin settings.

        Returns:
            Sanitized JPEG path, or None when the avatar is unavailable.

        Raises:
            ValueError: The QQ identifier is invalid.
        """
        if not isinstance(user_id, str) or not re.fullmatch(r"[1-9]\d{4,19}", user_id):
            raise ValueError("QQ 号无效")
        path = self.directory / f"{user_id}.jpg"
        try:
            if path.is_file() and path.stat().st_mtime > time.time() - hours * 3600:
                return path
        except OSError:
            self.logger.debug("QQ avatar cache read failed; attempting refresh.")
        if self.failures.get(user_id, 0) > time.time() - 300:
            return None
        if user_id not in self.pending:
            if len(self.pending) >= 100:
                return None
            self.pending[user_id] = asyncio.create_task(self.fetch(user_id, hours))
            self.pending[user_id].add_done_callback(
                lambda finished: (
                    self.pending.pop(user_id, None)
                    if self.pending.get(user_id) is finished
                    else None
                )
            )
        task = self.pending[user_id]
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self.pending.get(user_id) is task:
                self.pending.pop(user_id, None)

    async def fetch(self, user_id: str, hours: int = 24) -> Path | None:
        """Fetch from the fixed QQ CDN, decode within limits, and replace atomically.

        Args:
            user_id: Validated numeric QQ number.
            hours: Configured lifetime for cache cleanup after insertion.

        Returns:
            Cached avatar, or None with a short failure cooldown.
        """
        generation = self.generation
        temporary: Path | None = None
        try:
            async with self.semaphore:
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=6, connect=3),
                        connector=aiohttp.TCPConnector(limit=4),
                    )
                async with self.session.get(
                    "https://q1.qlogo.cn/g",
                    params={"b": "qq", "nk": user_id, "s": "100"},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise ValueError("Avatar CDN rejected the request")
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        data.extend(chunk)
                        if len(data) > 512 * 1024:
                            raise ValueError("Avatar exceeds the byte limit")
                with await asyncio.to_thread(Image.open, BytesIO(data)) as original:
                    if original.width * original.height > 2_000_000:
                        raise ValueError("Avatar exceeds the pixel limit")
                    await asyncio.to_thread(original.load)
                    with await asyncio.to_thread(
                        ImageOps.exif_transpose, original
                    ) as oriented:
                        with oriented.convert("RGBA") as rgba:
                            with Image.new("RGB", rgba.size, "white") as picture:
                                picture.paste(rgba, (0, 0), rgba)
                                picture.thumbnail((160, 160), Image.Resampling.LANCZOS)
                                self.directory.mkdir(parents=True, exist_ok=True)
                                temporary = (
                                    self.directory / f"{secrets.token_hex(16)}.part"
                                )
                                await asyncio.to_thread(
                                    picture.save, temporary, "JPEG", quality=85
                                )
                if generation != self.generation:
                    return None
                path = self.directory / f"{user_id}.jpg"
                temporary.replace(path)
                await self.cleanup(hours)
                self.failures.pop(user_id, None)
                self.logger.debug("QQ avatar refreshed in the expiring cache.")
                return path
        except (
            aiohttp.ClientError,
            asyncio.TimeoutError,
            TimeoutError,
            OSError,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
        ) as exc:
            if len(self.failures) >= 1000:
                self.failures.pop(next(iter(self.failures)))
            self.failures[user_id] = time.time()
            self.logger.warning(
                "QQ avatar unavailable (%s); keeping the identity fallback.",
                type(exc).__name__,
            )
            return None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    self.logger.debug(
                        "Avatar temporary cleanup deferred to the scheduled sweep."
                    )

    async def cleanup(self, hours: int = 24, *, clear: bool = False) -> int:
        """Remove expired files and enforce a 1000-avatar cache bound.

        Args:
            hours: Current cache lifetime in hours.
            clear: Explicitly remove the cache and invalidate in-flight writes.

        Returns:
            Number of files removed.
        """
        if clear:
            self.generation += 1
        if not self.directory.is_dir():
            self.failures.clear()
            return 0
        removed = 0
        files = sorted(
            self.directory.glob("*.jpg"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        cutoff = time.time() - hours * 3600
        for index, path in enumerate(files):
            if clear or index >= 1000 or path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        for path in self.directory.glob("*.part"):
            if path.stat().st_mtime < time.time() - 3600:
                path.unlink(missing_ok=True)
                removed += 1
        self.failures = {
            key: value
            for key, value in self.failures.items()
            if not clear and value > time.time() - 300
        }
        return removed

    async def close(self) -> None:
        """Cancel pending requests and close the plugin-owned HTTP connection pool."""
        tasks = list(self.pending.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.pending.clear()
        if self.session is not None:
            await self.session.close()
            self.session = None
