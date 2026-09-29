"""Persist lottery rules, QQ identities, private forms, and delivery retries."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

CHINA_TZ = timezone(timedelta(hours=8))


def timestamp(value: Any) -> float:
    """Parse a deadline with an explicit UTC+8 default for naive dates.

    Args:
        value: ISO 8601 date or numeric Unix seconds.

    Returns:
        Unix timestamp in seconds.

    Raises:
        ValueError: The date is invalid or out of range.
    """
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result = float(value)
        else:
            date = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            result = (
                date.replace(tzinfo=CHINA_TZ).timestamp()
                if date.tzinfo is None
                else date.timestamp()
            )
        if not 0 < result < 253402214400:
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(
            "时间格式无效，请使用 2026-10-01 20:00 或带时区的 ISO 时间。"
        ) from exc


def date_text(value: float) -> str:
    """Format persisted times for QQ cards and the management page.

    Args:
        value: Unix seconds.

    Returns:
        A UTC+8 display date.
    """
    return datetime.fromtimestamp(value, CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")


def validate_lottery(payload: dict, *, now: float | None = None) -> dict:
    """Validate the complete user-facing lottery contract at every write boundary.

    Args:
        payload: Title, prizes, targets, dates, and private questions.
        now: Optional clock for validation.

    Returns:
        Normalized lottery rules, without runtime state.

    Raises:
        ValueError: A rule, question, identifier, or deadline is invalid.
    """
    now = time.time() if now is None else now
    if not isinstance(payload, dict):
        raise ValueError("抽奖设置必须为对象。")
    result: dict[str, Any] = {}
    for key, label, limit in (
        ("title", "标题", 80),
        ("prize", "奖品", 300),
        ("description", "说明", 1500),
    ):
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValueError(f"{label}必须为不超过 {limit} 字的文本。")
        result[key] = " ".join(value.split()) if key == "title" else value.strip()
    if not result["title"] or not result["prize"]:
        raise ValueError("请填写标题和奖品。")
    count = payload.get("winner_count", 1)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 100:
        raise ValueError("中奖人数必须为 1–100 的整数。")
    result["winner_count"] = count
    result["close_at"] = timestamp(payload.get("close_at"))
    result["draw_at"] = timestamp(payload.get("draw_at"))
    if result["close_at"] <= now:
        raise ValueError("报名截止时间必须晚于当前时间。")
    if result["draw_at"] < result["close_at"]:
        raise ValueError("开奖时间不能早于报名截止时间。")
    targets = payload.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 50:
        raise ValueError("请至少添加一个允许的平台和群，最多 50 个群。")
    result["targets"] = []
    unique = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("群列表格式无效。")
        platform_id = target.get("platform_id", "")
        bot_id = str(target.get("bot_id", ""))
        group_id = str(target.get("group_id", ""))
        if not isinstance(platform_id, str) or not 1 <= len(platform_id) <= 160:
            raise ValueError("请选择有效的 AioCqhttp 平台。")
        if not re.fullmatch(r"[1-9]\d{4,19}", bot_id) or not re.fullmatch(
            r"[1-9]\d{4,19}", group_id
        ):
            raise ValueError("机器人 QQ 号或群号无效。")
        identity = (platform_id, bot_id, group_id)
        if identity in unique:
            continue
        unique.add(identity)
        result["targets"].append(
            {
                "platform_id": platform_id,
                "bot_id": bot_id,
                "group_id": group_id,
                "group_name": str(target.get("group_name", ""))[:100],
            }
        )
    questions = payload.get("questions", [])
    if not isinstance(questions, list) or len(questions) > 20:
        raise ValueError("私聊问题必须为列表，最多 20 项。")
    result["questions"] = []
    for question in questions:
        if not isinstance(question, dict):
            raise ValueError("问题格式无效。")
        kind = question.get("kind")
        prompt = question.get("prompt", "")
        if not isinstance(kind, str) or kind not in {"quiz", "text", "image"}:
            raise ValueError("问题类型只支持答题、文本资料、图片资料。")
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 300:
            raise ValueError("问题内容必须为 1–300 字。")
        options = question.get("options", [])
        answers = question.get("answers", [])
        if (
            not isinstance(options, list)
            or len(options) > 8
            or any(
                not isinstance(x, str) or not 1 <= len(x.strip()) <= 100
                for x in options
            )
        ):
            raise ValueError("答题选项最多 8 个，每项为 1–100 字。")
        if (
            not isinstance(answers, list)
            or len(answers) > 20
            or any(
                not isinstance(x, str) or not 1 <= len(x.strip()) <= 200
                for x in answers
            )
        ):
            raise ValueError("可接受的答案最多 20 个，每项为 1–200 字。")
        if kind == "quiz" and not answers:
            raise ValueError("答题问题需要至少一个正确答案。")
        if options and kind != "quiz":
            raise ValueError("只有答题问题可以设置选项。")
        options = [option.strip() for option in options]
        if options and any(answer.strip() not in options for answer in answers):
            raise ValueError("选择题的正确答案必须填写完整的选项文本。")
        result["questions"].append(
            {
                "kind": kind,
                "prompt": prompt.strip(),
                "options": [x.strip() for x in options],
                "answers": [x.strip() for x in answers] if kind == "quiz" else [],
            }
        )
    return result


class Store:
    """Own a serialized SQLite connection with atomic draws and a durable outbox."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = asyncio.Lock()
        self.db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        """Initialize runtime storage without placing user data in plugin source."""
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "uploads").mkdir(exist_ok=True)
        self.db = await aiosqlite.connect(
            self.directory / "lotteries.sqlite3", isolation_level=None
        )
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES (1, '{"manager_ids":[]}');
            CREATE TABLE IF NOT EXISTS lotteries (id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries (
                lottery_id TEXT NOT NULL REFERENCES lotteries(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY (lottery_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                lottery_id TEXT NOT NULL REFERENCES lotteries(id) ON DELETE CASCADE,
                PRIMARY KEY (bot_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, lottery_id TEXT NOT NULL REFERENCES lotteries(id) ON DELETE CASCADE,
                body TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, delivered_at REAL, error TEXT NOT NULL DEFAULT ''
            );
        """)

    async def close(self) -> None:
        """Flush and close the plugin-owned database connection."""
        if self.db is not None:
            await self.db.close()
            self.db = None

    async def snapshot(self, *, private: bool = False) -> list[dict]:
        """Read activity summaries while keeping private answers out of public views.

        Args:
            private: Include editable questions only for authenticated management.

        Returns:
            Summaries ordered by creation time.
        """
        async with self.lock:
            async with self.db.execute("SELECT body FROM lotteries") as cursor:
                items = [json.loads(row[0]) for row in await cursor.fetchall()]
            async with self.db.execute(
                "SELECT lottery_id, count(*), sum(json_extract(body, '$.status')='complete') FROM entries GROUP BY lottery_id"
            ) as cursor:
                counts = {row[0]: (row[1], row[2]) for row in await cursor.fetchall()}
            async with self.db.execute(
                "SELECT lottery_id, count(*) FROM outbox WHERE delivered_at IS NULL GROUP BY lottery_id"
            ) as cursor:
                pending = dict(await cursor.fetchall())
        for item in items:
            item["entry_count"], item["complete_count"] = counts.get(item["id"], (0, 0))
            item["pending_deliveries"] = pending.get(item["id"], 0)
            item["phase"] = (
                "closed"
                if item["status"] == "open" and time.time() >= item["close_at"]
                else item["status"]
            )
            if not private:
                item["questions"] = [
                    {k: v for k, v in q.items() if k != "answers"}
                    for q in item["questions"]
                ]
                item.pop("creator_id", None)
        return sorted(items, key=lambda x: x["created_at"], reverse=True)

    async def get(self, lottery_id: str) -> dict:
        """Read one lottery's rules.

        Args:
            lottery_id: Exact activity identifier.

        Returns:
            Persisted activity rules and state.

        Raises:
            ValueError: The activity does not exist.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            raise ValueError("找不到该抽奖，请发送 /抽奖 列表 查看编号。")
        return json.loads(row[0])

    async def settings(self, payload: dict | None = None) -> dict:
        """Read or replace the WebUI-managed operator allowlist.

        Args:
            payload: Optional list of extra operator QQ IDs.

        Returns:
            Validated settings.

        Raises:
            ValueError: An operator identifier is invalid.
        """
        async with self.lock:
            if payload is not None:
                ids = payload.get("manager_ids") if isinstance(payload, dict) else None
                if (
                    not isinstance(ids, list)
                    or len(ids) > 100
                    or any(
                        not isinstance(x, str) or not re.fullmatch(r"[1-9]\d{4,19}", x)
                        for x in ids
                    )
                ):
                    raise ValueError("管理员 QQ 号需要用列表填写，最多 100 个。")
                payload = {"manager_ids": list(dict.fromkeys(ids))}
                await self.db.execute(
                    "UPDATE settings SET body=? WHERE id=1", (json.dumps(payload),)
                )
            async with self.db.execute(
                "SELECT body FROM settings WHERE id=1"
            ) as cursor:
                row = await cursor.fetchone()
        return json.loads(row[0])

    async def save(self, payload: dict, creator_id: str, lottery_id: str = "") -> dict:
        """Create rules or edit an activity without changing an enrolled user's contract.

        Args:
            payload: Complete rules, validated again under the write lock.
            creator_id: Trusted operator identifier.
            lottery_id: Existing ID for edits, or empty for creation.

        Returns:
            The saved activity.

        Raises:
            ValueError: The activity is final or enrolled rules would change.
        """
        async with self.lock:
            rules = validate_lottery(payload)
            editing = bool(lottery_id)
            if lottery_id:
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise ValueError("抽奖不存在。")
                item = json.loads(row[0])
                if item["status"] != "open":
                    raise ValueError("已开奖或已取消的抽奖不能编辑。")
                async with self.db.execute(
                    "SELECT count(*) FROM entries WHERE lottery_id=?", (lottery_id,)
                ) as cursor:
                    count = (await cursor.fetchone())[0]
                if count and any(
                    rules[k] != item[k]
                    for k in ("targets", "questions", "prize", "winner_count")
                ):
                    raise ValueError(
                        "已有报名后，平台、群、问题、奖品和中奖人数不能修改。可修改说明和未来时间。"
                    )
                item.update(rules)
            else:
                for _ in range(5):
                    lottery_id = secrets.token_hex(4)
                    async with self.db.execute(
                        "SELECT 1 FROM lotteries WHERE id=?", (lottery_id,)
                    ) as cursor:
                        if await cursor.fetchone() is None:
                            break
                else:
                    raise RuntimeError(
                        "Could not allocate a unique lottery identifier."
                    )
                item = {
                    **rules,
                    "id": lottery_id,
                    "status": "open",
                    "created_at": time.time(),
                    "creator_id": creator_id,
                    "winners": [],
                    "drawn_at": None,
                    "pool_hash": None,
                }
            await self.db.execute(
                "INSERT INTO lotteries VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body"
                if editing
                else "INSERT INTO lotteries VALUES (?, ?)",
                (lottery_id, json.dumps(item, ensure_ascii=False)),
            )
        return item

    async def entry(self, lottery_id: str, user_id: str) -> dict | None:
        """Read enrollment using the actual QQ sender rather than a session ID.

        Args:
            lottery_id: Activity identifier.
            user_id: Trusted event sender QQ number.

        Returns:
            The user's entry or None.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                (lottery_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def enroll(self, lottery_id: str, identity: dict) -> tuple[dict, bool]:
        """Reserve one slot across all groups; finish immediately when no form is needed.

        Args:
            lottery_id: Activity identifier.
            identity: Trusted group, platform, bot, and actual QQ sender.

        Returns:
            The entry and whether it was newly created.

        Raises:
            ValueError: Registration is closed or the target is not allowed.
        """
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    raise ValueError("抽奖不存在。")
                item = json.loads(row[0])
                if not identity.get("group_id") or not any(
                    all(
                        t[k] == identity[k]
                        for k in ("platform_id", "bot_id", "group_id")
                    )
                    for t in item["targets"]
                ):
                    raise ValueError(
                        "只能从该抽奖允许的平台和群发起报名，私聊不能参与。"
                    )
                if item["status"] != "open" or time.time() >= item["close_at"]:
                    raise ValueError("报名已截止，请查看抽奖详情。")
                async with self.db.execute(
                    "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                    (lottery_id, identity["user_id"]),
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    await self.db.commit()
                    return json.loads(row[0]), False
                async with self.db.execute(
                    "SELECT count(*) FROM entries WHERE lottery_id=?", (lottery_id,)
                ) as cursor:
                    if (await cursor.fetchone())[0] >= 10000:
                        raise ValueError("报名人数已达上限。")
                entry = {
                    **identity,
                    "answers": [],
                    "status": "pending" if item["questions"] else "complete",
                    "joined_at": time.time(),
                    "completed_at": None if item["questions"] else time.time(),
                }
                await self.db.execute(
                    "INSERT INTO entries VALUES (?, ?, ?)",
                    (
                        lottery_id,
                        identity["user_id"],
                        json.dumps(entry, ensure_ascii=False),
                    ),
                )
                if entry["status"] == "complete":
                    await self.queue(item, "success", entry=entry)
                await self.db.commit()
                return entry, True
            except BaseException:
                await self.db.rollback()
                raise

    async def private_session(
        self, bot_id: str, user_id: str, lottery_id: str | None = None
    ) -> str | None:
        """Select or read a form after a group-created reservation.

        Args:
            bot_id: Robot QQ number from the event.
            user_id: Actual private sender QQ number.
            lottery_id: Form to select, empty string to clear, or None to read.

        Returns:
            Current form identifier or None.

        Raises:
            ValueError: The sender did not reserve a slot with this robot.
        """
        async with self.lock:
            if lottery_id is not None:
                if not lottery_id:
                    await self.db.execute(
                        "DELETE FROM sessions WHERE bot_id=? AND user_id=?",
                        (bot_id, user_id),
                    )
                    return None
                async with self.db.execute(
                    "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                    (lottery_id, user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                entry = json.loads(row[0]) if row else None
                if (
                    not entry
                    or entry["bot_id"] != bot_id
                    or entry["status"] != "pending"
                ):
                    raise ValueError(
                        "没有待填写的群报名记录。请先在允许的群内发送 /抽奖 参与 编号。"
                    )
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                ) as cursor:
                    item = json.loads((await cursor.fetchone())[0])
                if item["status"] != "open" or time.time() >= item["close_at"]:
                    raise ValueError("报名已截止，不能继续填写。")
                await self.db.execute(
                    "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?)",
                    (bot_id, user_id, lottery_id),
                )
            async with self.db.execute(
                "SELECT lottery_id FROM sessions WHERE bot_id=? AND user_id=?",
                (bot_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
        return row[0] if row else None

    async def answer(
        self, lottery_id: str, identity: dict, answer: dict, index: int
    ) -> tuple[dict, dict]:
        """Accept exactly the next private answer and commit success atomically.

        Args:
            lottery_id: Previously selected activity identifier.
            identity: Actual sender, bot, and platform; group_id must be empty.
            answer: Validated text or an internal image filename.
            index: Expected question index to reject simultaneous stale submissions.

        Returns:
            Updated activity and entry.

        Raises:
            ValueError: The sender, deadline, question type, or answer is invalid.
        """
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    raise ValueError("抽奖不存在。")
                item = json.loads(row[0])
                async with self.db.execute(
                    "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                    (lottery_id, identity["user_id"]),
                ) as cursor:
                    row = await cursor.fetchone()
                entry = json.loads(row[0]) if row else None
                if (
                    identity.get("group_id")
                    or not entry
                    or any(
                        entry[k] != identity[k]
                        for k in ("bot_id", "platform_id", "user_id")
                    )
                ):
                    raise ValueError("请用报名时的 QQ 号私聊同一个机器人填写。")
                if item["status"] != "open" or time.time() >= item["close_at"]:
                    raise ValueError("报名截止，未完成的资料不会进入开奖名单。")
                if entry["status"] != "pending" or index != len(entry["answers"]):
                    raise ValueError("这题已处理，请按最新的问题卡片作答。")
                question = item["questions"][index]
                if answer.get("kind") != (
                    "image" if question["kind"] == "image" else "text"
                ):
                    raise ValueError("请按当前题目要求发送文本或一张图片。")
                if question["kind"] == "image":
                    if not re.fullmatch(r"[a-f0-9]{32}\.jpg", answer.get("value", "")):
                        raise ValueError("图片记录无效。")
                else:
                    text = answer.get("value", "")
                    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1000:
                        raise ValueError("文本请控制在 1–1000 字。")
                    text = text.strip()
                    options = question["options"]
                    if options:
                        choice = text.upper()
                        if choice in [chr(65 + n) for n in range(len(options))]:
                            text = options[ord(choice) - 65]
                        elif choice.isdigit() and 1 <= int(choice) <= len(options):
                            text = options[int(choice) - 1]
                        elif text not in options:
                            raise ValueError("请发送选项字母、数字序号或完整选项文本。")
                    if question["kind"] == "quiz":
                        normalized = (
                            unicodedata.normalize("NFKC", text).strip().casefold()
                        )
                        if normalized not in {
                            unicodedata.normalize("NFKC", x).strip().casefold()
                            for x in question["answers"]
                        }:
                            raise ValueError(
                                "答案还不正确，再想一想喵！请重新回答当前题。"
                            )
                    answer = {"kind": "text", "value": text}
                entry["answers"].append(answer)
                if len(entry["answers"]) == len(item["questions"]):
                    entry["status"] = "complete"
                    entry["completed_at"] = time.time()
                    await self.db.execute(
                        "DELETE FROM sessions WHERE bot_id=? AND user_id=?",
                        (identity["bot_id"], identity["user_id"]),
                    )
                    await self.queue(item, "success", entry=entry)
                await self.db.execute(
                    "UPDATE entries SET body=? WHERE lottery_id=? AND user_id=?",
                    (
                        json.dumps(entry, ensure_ascii=False),
                        lottery_id,
                        identity["user_id"],
                    ),
                )
                await self.db.commit()
                return item, entry
            except BaseException:
                await self.db.rollback()
                raise

    async def withdraw(self, lottery_id: str, identity: dict) -> None:
        """Remove only the actual sender's enrollment before the cutoff.

        Args:
            lottery_id: Activity identifier.
            identity: Trusted group event identity.

        Raises:
            ValueError: The activity is closed or this group is not allowed.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                raise ValueError("抽奖不存在。")
            item = json.loads(row[0])
            if item["status"] != "open" or time.time() >= item["close_at"]:
                raise ValueError("报名已截止，不能退出。")
            if not identity.get("group_id") or not any(
                all(t[k] == identity[k] for k in ("platform_id", "bot_id", "group_id"))
                for t in item["targets"]
            ):
                raise ValueError("请在该抽奖允许的群内退出。")
            async with self.db.execute(
                "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                (lottery_id, identity["user_id"]),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                raise ValueError("你还没有参与该抽奖。")
            entry = json.loads(row[0])
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "DELETE FROM entries WHERE lottery_id=? AND user_id=?",
                    (lottery_id, identity["user_id"]),
                )
                await self.db.execute(
                    "DELETE FROM sessions WHERE lottery_id=? AND user_id=?",
                    (lottery_id, identity["user_id"]),
                )
                await self.db.execute(
                    "DELETE FROM outbox WHERE lottery_id=? AND json_extract(body, '$.entry.user_id')=?",
                    (lottery_id, identity["user_id"]),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        for answer in entry["answers"]:
            if answer["kind"] == "image":
                (self.directory / "uploads" / Path(answer["value"]).name).unlink(
                    missing_ok=True
                )

    async def queue(self, item: dict, kind: str, *, entry: dict | None = None) -> None:
        """Enqueue delivery inside an existing lock and transaction.

        Args:
            item: Activity snapshot, frozen for this message.
            kind: Announcement, result, cancellation, or enrollment success.
            entry: The completed user's trusted routing data for success.
        """
        targets = item["targets"]
        if entry is not None:
            targets = [
                {**entry, "channel": "group", "recipient": entry["group_id"]},
                {**entry, "channel": "private", "recipient": entry["user_id"]},
            ]
        for target in targets:
            message = {
                "kind": kind,
                "item": copy.deepcopy(item),
                "target": {
                    "platform_id": target["platform_id"],
                    "bot_id": target["bot_id"],
                    "channel": target.get("channel", "group"),
                    "recipient": target.get("recipient", target.get("group_id")),
                },
            }
            if entry is not None:
                message["entry"] = {k: v for k, v in entry.items() if k != "answers"}
            # Correct answers and private forms never enter delivery records.
            message["item"]["questions"] = [
                {k: v for k, v in q.items() if k != "answers"}
                for q in message["item"]["questions"]
            ]
            delivery_id = secrets.token_hex(16)
            await self.db.execute(
                "INSERT INTO outbox (id, lottery_id, body) VALUES (?, ?, ?)",
                (delivery_id, item["id"], json.dumps(message, ensure_ascii=False)),
            )

    async def action(
        self, lottery_id: str, action: str, *, due_only: bool = False
    ) -> dict:
        """Finalize winners once and persist announcements before attempting network IO.

        Args:
            lottery_id: Activity identifier.
            action: publish, close, draw, or cancel.
            due_only: Require scheduled draw time to have arrived.

        Returns:
            Saved activity including immutable winners and eligible pool digest.

        Raises:
            ValueError: The operation conflicts with the activity state.
        """
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    raise ValueError("抽奖不存在。")
                item = json.loads(row[0])
                if item["status"] != "open":
                    raise ValueError("该抽奖已经结束，不能重复开奖或操作。")
                if action == "draw":
                    if due_only and time.time() < item["draw_at"]:
                        raise ValueError("尚未到开奖时间。")
                    async with self.db.execute(
                        "SELECT body FROM entries WHERE lottery_id=? ORDER BY user_id",
                        (lottery_id,),
                    ) as cursor:
                        pool = [json.loads(row[0]) for row in await cursor.fetchall()]
                    pool = [e for e in pool if e["status"] == "complete"]
                    item["pool_hash"] = hashlib.sha256(
                        "\n".join(e["user_id"] for e in pool).encode()
                    ).hexdigest()
                    selected = secrets.SystemRandom().sample(
                        pool, min(item["winner_count"], len(pool))
                    )
                    item["winners"] = [
                        {"user_id": e["user_id"], "nickname": e["nickname"]}
                        for e in selected
                    ]
                    item["eligible_count"] = len(pool)
                    item["status"] = "drawn"
                    item["drawn_at"] = time.time()
                    item["close_at"] = min(item["close_at"], item["drawn_at"])
                    await self.queue(item, "result")
                elif action == "cancel":
                    item["status"] = "cancelled"
                    await self.db.execute(
                        "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL",
                        (lottery_id,),
                    )
                    await self.queue(item, "cancelled")
                elif action == "close":
                    if time.time() >= item["close_at"]:
                        raise ValueError("报名已经截止，无需重复操作。")
                    item["close_at"] = min(item["close_at"], time.time())
                    await self.queue(item, "closed")
                elif action == "publish":
                    if time.time() >= item["close_at"]:
                        raise ValueError(
                            "报名已截止，不再发布招募公告。请查看抽奖详情。"
                        )
                    await self.queue(item, "announcement")
                else:
                    raise ValueError("操作类型无效。")
                if action in {"draw", "cancel", "close"}:
                    await self.db.execute(
                        "DELETE FROM sessions WHERE lottery_id=?", (lottery_id,)
                    )
                await self.db.execute(
                    "UPDATE lotteries SET body=? WHERE id=?",
                    (json.dumps(item, ensure_ascii=False), lottery_id),
                )
                await self.db.commit()
                return item
            except BaseException:
                await self.db.rollback()
                raise

    async def deliveries(self) -> list[dict]:
        """Fetch due delivery attempts without dropping offline-platform messages.

        Returns:
            A bounded batch with persisted retry metadata.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT * FROM outbox WHERE delivered_at IS NULL AND next_at<=? ORDER BY rowid LIMIT 40",
                (time.time(),),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def delivery_done(self, delivery_id: str, error: str = "") -> None:
        """Record success or back off a failed network attempt.

        Args:
            delivery_id: Outbox message identifier.
            error: A safe error category, empty for successful delivery.
        """
        async with self.lock:
            if error:
                async with self.db.execute(
                    "SELECT attempts FROM outbox WHERE id=?", (delivery_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if row:
                    delay = min(3600, 30 * 2 ** min(row[0], 7))
                    await self.db.execute(
                        "UPDATE outbox SET attempts=attempts+1, next_at=?, error=? WHERE id=?",
                        (time.time() + delay, error[:200], delivery_id),
                    )
            else:
                await self.db.execute(
                    "UPDATE outbox SET delivered_at=?, error='' WHERE id=?",
                    (time.time(), delivery_id),
                )

    async def manage_entries(self, lottery_id: str) -> dict:
        """Read private forms and per-target delivery state for an authenticated operator.

        Args:
            lottery_id: Activity identifier.

        Returns:
            Enrollment and delivery records with correct answers excluded.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT body FROM entries WHERE lottery_id=? ORDER BY json_extract(body,'$.joined_at')",
                (lottery_id,),
            ) as cursor:
                entries = [json.loads(row[0]) for row in await cursor.fetchall()]
            async with self.db.execute(
                "SELECT id, body, attempts, next_at, delivered_at, error FROM outbox WHERE lottery_id=? ORDER BY rowid DESC LIMIT 200",
                (lottery_id,),
            ) as cursor:
                deliveries = []
                for row in await cursor.fetchall():
                    message = json.loads(row[1])
                    deliveries.append(
                        {
                            "id": row[0],
                            "kind": message["kind"],
                            "target": message["target"],
                            "attempts": row[2],
                            "next_at": row[3],
                            "delivered_at": row[4],
                            "error": row[5],
                        }
                    )
        return {"entries": entries, "deliveries": deliveries}

    async def delete(self, lottery_id: str) -> None:
        """Delete a finished activity and its private attachments.

        Args:
            lottery_id: Activity identifier.

        Raises:
            ValueError: The activity is still open or absent.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if not row or json.loads(row[0])["status"] == "open":
                raise ValueError("请先取消或开奖，再删除记录。")
            async with self.db.execute(
                "SELECT body FROM entries WHERE lottery_id=?", (lottery_id,)
            ) as cursor:
                entries = [json.loads(row[0]) for row in await cursor.fetchall()]
            await self.db.execute("DELETE FROM lotteries WHERE id=?", (lottery_id,))
        for entry in entries:
            for answer in entry["answers"]:
                if answer["kind"] == "image":
                    (self.directory / "uploads" / Path(answer["value"]).name).unlink(
                        missing_ok=True
                    )
