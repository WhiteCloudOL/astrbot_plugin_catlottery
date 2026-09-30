"""Persist lottery rules, QQ identities, private forms, and delivery retries."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import secrets
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite

CHINA_TZ = timezone(timedelta(hours=8))
FORM_MODE_SECONDS = 20 * 60
logger = logging.getLogger("astrbot.plugin.astrbot_plugin_catlottery")


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


def prize_tiers(item: dict) -> list[dict]:
    """Read ordered awards while preserving the rules of legacy single-prize activities.

    Args:
        item: Current or legacy persisted lottery rules.

    Returns:
        Independent award rules, ordered from highest to lowest priority.
    """
    return copy.deepcopy(
        item.get("prize_tiers")
        or [
            {
                "name": "幸运奖",
                "prize": item["prize"],
                "count": item["winner_count"],
                "image": "",
            }
        ]
    )


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
        ("description", "说明", 1500),
    ):
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValueError(f"{label}必须为不超过 {limit} 字的文本。")
        result[key] = " ".join(value.split()) if key == "title" else value.strip()
    if not result["title"]:
        raise ValueError("请填写抽奖标题。")
    tiers = payload.get("prize_tiers")
    if "prize_tiers" not in payload:
        tiers = [
            {
                "name": "幸运奖",
                "prize": payload.get("prize", ""),
                "count": payload.get("winner_count", 1),
            }
        ]
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 10:
        raise ValueError("奖项需要为列表，请设置 1–10 个奖项。")
    result["prize_tiers"] = []
    names = set()
    for tier in tiers:
        if not isinstance(tier, dict):
            raise ValueError("每个奖项必须包含名称、奖品和名额。")
        name, prize, count = tier.get("name"), tier.get("prize"), tier.get("count")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 30:
            raise ValueError("奖项名称必须为 1–30 字，例如一等奖。")
        name = " ".join(name.split())
        if name in names:
            raise ValueError("奖项名称不能重复。")
        names.add(name)
        if not isinstance(prize, str) or not 1 <= len(prize.strip()) <= 300:
            raise ValueError("每个奖项的奖品必须为 1–300 字。")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= 100
        ):
            raise ValueError("各奖项中奖人数必须为 1–100 的整数。")
        image = tier.get("image", "")
        if not isinstance(image, str) or (
            image and not re.fullmatch(r"[a-f0-9]{32}\.jpg", image)
        ):
            raise ValueError("奖品图片无效，请通过管理页上传。")
        result["prize_tiers"].append(
            {"name": name, "prize": prize.strip(), "count": count, "image": image}
        )
    result["winner_count"] = sum(tier["count"] for tier in result["prize_tiers"])
    if result["winner_count"] > 100:
        raise ValueError("全部奖项的中奖人数合计不能超过 100。")
    result["prize"] = (
        result["prize_tiers"][0]["prize"]
        if len(tiers) == 1
        else "；".join(
            f"{tier['name']}：{tier['prize']} × {tier['count']}"
            for tier in result["prize_tiers"]
        )
    )
    cover = payload.get("cover", "")
    if not isinstance(cover, str) or (
        cover and not re.fullmatch(r"[a-f0-9]{32}\.jpg", cover)
    ):
        raise ValueError("活动封面无效，请通过管理页上传。")
    result["cover"] = cover
    require_correct = payload.get("require_correct", True)
    if not isinstance(require_correct, bool):
        raise ValueError("答题模式开关必须为启用或关闭。")
    result["require_correct"] = require_correct
    for key, label in (
        ("group_success_notify", "群聊参与成功"),
        ("group_pending_notify", "群聊待审核"),
    ):
        notify = payload.get(key, True)
        if not isinstance(notify, bool):
            raise ValueError(f"{label}通知开关必须为启用或关闭")
        result[key] = notify
    result["close_at"] = timestamp(payload.get("close_at"))
    result["draw_at"] = timestamp(payload.get("draw_at"))
    if result["close_at"] <= now:
        raise ValueError("报名截止时间必须晚于当前时间。")
    if result["draw_at"] < result["close_at"]:
        raise ValueError("开奖时间不能早于报名截止时间。")
    schedule = payload.get("announcement_schedule", {"mode": "off"})
    if not isinstance(schedule, dict):
        raise ValueError("群公告通知计划必须为对象")
    mode = schedule.get("mode", "off")
    if not isinstance(mode, str) or mode not in {"off", "once", "repeat"}:
        raise ValueError("群公告通知仅支持关闭、定时一次或循环发送")
    start_at = None if mode == "off" else timestamp(schedule.get("start_at"))
    if start_at is not None and start_at >= result["close_at"]:
        raise ValueError("首次群公告通知必须早于报名截止时间")
    interval = schedule.get("interval_minutes", 60) if mode == "repeat" else 0
    if mode == "repeat" and (
        isinstance(interval, bool)
        or not isinstance(interval, int)
        or not 1 <= interval <= 10080
    ):
        raise ValueError("循环通知间隔必须为 1–10080 分钟的整数")
    result["announcement_schedule"] = {
        "mode": mode,
        "start_at": start_at,
        "interval_minutes": interval,
    }
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
        if not re.fullmatch(r"[1-9][0-9]{4,19}", bot_id) or not re.fullmatch(
            r"[1-9][0-9]{4,19}", group_id
        ):
            raise ValueError("QQ 账号或群号无效。")
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
        if not isinstance(kind, str) or kind not in {"quiz", "text", "image", "mixed"}:
            raise ValueError("问题类型只支持答题、文本资料、图片资料、图文资料。")
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
        if kind == "quiz" and require_correct and not answers:
            raise ValueError("当场答对模式的答题问题需要至少一个正确答案。")
        if options and kind != "quiz":
            raise ValueError("只有答题问题可以设置选项。")
        options = [option.strip() for option in options]
        if len(
            {unicodedata.normalize("NFKC", option).casefold() for option in options}
        ) != len(options):
            raise ValueError("选择题选项不能重复，请检查大小写和全角／半角字符。")
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


def form_progress(entry: dict, question_count: int) -> tuple[int, int]:
    """Count present answers and locate the first missing question.

    Args:
        entry: A participant with index-aligned answer slots.
        question_count: Number of required questions.

    Returns:
        Submitted answer count and the first missing index, or question_count.
    """
    present = {
        index
        for index, answer in enumerate(entry.get("answers", []))
        if index < question_count and answer.get("kind") != "deleted"
    }
    return len(present), next(
        (index for index in range(question_count) if index not in present),
        question_count,
    )


def answer_images(answer: dict) -> list[str]:
    """Read separate attachments, including legacy single-image records.

    Args:
        answer: Saved or submitted answer record.

    Returns:
        Unique attachment filenames in submission order.
    """
    first = (
        answer.get("value", "")
        if answer.get("kind") == "image"
        else answer.get("image", "")
    )
    return list(dict.fromkeys(([first] if first else []) + answer.get("images", [])))


def validate_answer(question: dict, answer: dict, require_correct: bool) -> dict:
    """Normalize one submission without changing its whitespace or trusting review fields.

    Args:
        question: Frozen question rules.
        answer: Text and optional plugin-owned image reference.
        require_correct: Enforce instant quiz correctness when enabled.

    Returns:
        Validated answer with a fresh review mark.

    Raises:
        ValueError: The answer does not satisfy the question rules.
    """
    expected_kind = (
        question["kind"] if question["kind"] in {"image", "mixed"} else "text"
    )
    if not isinstance(answer, dict) or answer.get("kind") != expected_kind:
        raise ValueError("请按当前题目要求发送文本、图片或图文消息。")
    original_text = (
        answer.get("text", "") if expected_kind == "image" else answer.get("value", "")
    )
    filename = (
        answer.get("value", "") if expected_kind == "image" else answer.get("image", "")
    )
    images = answer.get("images", [])
    if (
        not isinstance(images, list)
        or len(images) > 9
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}\.jpg", value)
            for value in images
        )
    ):
        raise ValueError("每题最多保留 9 张有效图片")
    if not isinstance(original_text, str) or len(original_text) > 1000:
        raise ValueError("文本请控制在 1000 字以内，空格和换行也计入长度。")
    if expected_kind != "image" and not original_text.strip():
        raise ValueError("本题需要非空文本，图片可以与文本在同一条消息中发送。")
    if not isinstance(filename, str) or (
        filename and not re.fullmatch(r"[a-f0-9]{32}\.jpg", filename)
    ):
        raise ValueError("图片记录无效。")
    images = list(dict.fromkeys(([filename] if filename else []) + images))
    if len(images) > 9:
        raise ValueError("每题最多保留 9 张图片")
    filename = images[0] if images else ""
    if expected_kind in {"image", "mixed"} and not filename:
        raise ValueError("本题需要一张图片，请按题目要求重新发送。")
    answer = {
        "kind": expected_kind,
        "value": filename if expected_kind == "image" else original_text,
    }
    if expected_kind == "image" and original_text:
        answer["text"] = original_text
    elif expected_kind != "image" and filename:
        answer["image"] = filename
    if len(images) > 1:
        answer["images"] = images
    if question["kind"] == "quiz" and require_correct:
        text = original_text.strip()
        options = question["options"]
        if options:
            choice = unicodedata.normalize("NFKC", text).upper()
            if choice in [chr(65 + n) for n in range(len(options))]:
                text = options[ord(choice) - 65]
            elif choice.isdecimal() and 1 <= int(choice) <= len(options):
                text = options[int(choice) - 1]
            elif unicodedata.normalize("NFKC", text).casefold() not in {
                unicodedata.normalize("NFKC", option).casefold() for option in options
            }:
                raise ValueError("请发送选项字母、数字序号或完整选项文本。")
        normalized = unicodedata.normalize("NFKC", text).strip().casefold()
        if normalized not in {
            unicodedata.normalize("NFKC", x).strip().casefold()
            for x in question["answers"]
        }:
            raise ValueError("答案还不正确，再想一想喵！请重新回答当前题。")
    if not require_correct:
        answer["correct"] = None
    return answer


def review_status(item: dict, entry: dict) -> str:
    """Derive eligibility from submission completion and the frozen answer mode.

    Args:
        item: Lottery rules, with legacy activities defaulting to instant checking.
        entry: A participant's original answers and review marks.

    Returns:
        incomplete, pending, rejected, or approved.
    """
    if entry["status"] != "complete":
        return "incomplete"
    if item.get("require_correct", True) or not item["questions"]:
        return "approved"
    if isinstance(entry.get("review_decision"), bool):
        return "approved" if entry["review_decision"] else "rejected"
    marks = [answer.get("correct") for answer in entry["answers"]]
    if any(mark is False for mark in marks):
        return "rejected"
    return "approved" if all(mark is True for mark in marks) else "pending"


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
        (self.directory / "artwork").mkdir(exist_ok=True)
        self.db = await aiosqlite.connect(
            self.directory / "lotteries.sqlite3", isolation_level=None
        )
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES (1, '{"manager_ids":[],"llm_tools_enabled":true}');
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
        async with self.db.execute("PRAGMA table_info(sessions)") as cursor:
            columns = {row[1] for row in await cursor.fetchall()}
        if "question_index" not in columns:
            await self.db.execute(
                "ALTER TABLE sessions ADD COLUMN question_index INTEGER"
            )
        if "expires_at" not in columns:
            await self.db.execute("ALTER TABLE sessions ADD COLUMN expires_at REAL")
        await self.db.execute(
            "UPDATE sessions SET expires_at=? WHERE expires_at IS NULL",
            (time.time() + FORM_MODE_SECONDS,),
        )

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
                """SELECT lottery_id, count(*),
                    sum(json_extract(body, '$.status')='complete' AND
                        coalesce(json_extract(body, '$.review_status'), 'approved')='approved'),
                    sum(json_extract(body, '$.status')='complete'),
                    coalesce(sum(json_extract(body, '$.review_status')='pending'), 0),
                    coalesce(sum(json_extract(body, '$.review_status')='rejected'), 0)
                    FROM entries GROUP BY lottery_id"""
            ) as cursor:
                counts = {row[0]: tuple(row[1:]) for row in await cursor.fetchall()}
            async with self.db.execute(
                "SELECT lottery_id, count(*) FROM outbox WHERE delivered_at IS NULL GROUP BY lottery_id"
            ) as cursor:
                pending = dict(await cursor.fetchall())
        for item in items:
            item["prize_tiers"] = prize_tiers(item)
            item.setdefault("cover", "")
            item.setdefault("tier_draws", [])
            (
                item["entry_count"],
                item["complete_count"],
                item["submitted_count"],
                item["review_pending_count"],
                item["rejected_count"],
            ) = counts.get(item["id"], (0, 0, 0, 0, 0))
            item["require_correct"] = item.get("require_correct", True)
            item.setdefault("group_success_notify", True)
            item.setdefault("group_pending_notify", True)
            item.setdefault(
                "announcement_schedule",
                {"mode": "off", "start_at": None, "interval_minutes": 0},
            )
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
        item = json.loads(row[0])
        item["prize_tiers"] = prize_tiers(item)
        item.setdefault("cover", "")
        item.setdefault("tier_draws", [])
        item.setdefault("group_success_notify", True)
        item.setdefault("group_pending_notify", True)
        item.setdefault("require_correct", True)
        item.setdefault(
            "announcement_schedule",
            {"mode": "off", "start_at": None, "interval_minutes": 0},
        )
        return item

    async def participation_state(self, lottery_id: str, user_id: str = "") -> dict:
        """Read current counts, notification policy, and only the requested sender's entry.

        Args:
            lottery_id: Exact activity identifier.
            user_id: Trusted event sender, or empty when only aggregate counts are needed.

        Returns:
            Current rules, approved and pending totals, and the sender's own entry.

        Raises:
            ValueError: The activity does not exist.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                raise ValueError("找不到该抽奖，请发送 /抽奖 列表 查看编号")
            item = json.loads(row[0])
            async with self.db.execute(
                "SELECT coalesce(sum(json_extract(body,'$.status')='complete' AND "
                "coalesce(json_extract(body,'$.review_status'),'approved')='approved'),0), "
                "coalesce(sum(json_extract(body,'$.status')='complete' AND "
                "json_extract(body,'$.review_status')='pending'),0) FROM entries WHERE lottery_id=?",
                (lottery_id,),
            ) as cursor:
                approved, pending = await cursor.fetchone()
            entry = None
            if user_id:
                async with self.db.execute(
                    "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                    (lottery_id, user_id),
                ) as cursor:
                    row = await cursor.fetchone()
                entry = json.loads(row[0]) if row else None
        item["prize_tiers"] = prize_tiers(item)
        item.setdefault("cover", "")
        item.setdefault("tier_draws", [])
        item.setdefault("group_success_notify", True)
        item.setdefault("group_pending_notify", True)
        item.setdefault(
            "announcement_schedule",
            {"mode": "off", "start_at": None, "interval_minutes": 0},
        )
        return {"item": item, "entry": entry, "approved": approved, "pending": pending}

    async def settings(self, payload: dict | None = None) -> dict:
        """Read or replace the WebUI-managed operators and tool switch.

        Args:
            payload: Optional settings with operator QQ IDs and a boolean tool switch.

        Returns:
            Validated settings.

        Raises:
            ValueError: An operator identifier is invalid.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT body FROM settings WHERE id=1"
            ) as cursor:
                settings = json.loads((await cursor.fetchone())[0])
            settings.setdefault("llm_tools_enabled", True)
            settings.setdefault("avatar_cache_hours", 24)
            if payload is not None:
                ids = payload.get("manager_ids") if isinstance(payload, dict) else None
                if (
                    not isinstance(ids, list)
                    or len(ids) > 100
                    or any(
                        not isinstance(x, str)
                        or not re.fullmatch(r"[1-9][0-9]{4,19}", x)
                        for x in ids
                    )
                ):
                    raise ValueError("管理员 QQ 号需要用列表填写，最多 100 个。")
                enabled = payload.get(
                    "llm_tools_enabled", settings["llm_tools_enabled"]
                )
                if not isinstance(enabled, bool):
                    raise ValueError("LLM 工具开关必须为启用或关闭。")
                hours = payload.get(
                    "avatar_cache_hours", settings["avatar_cache_hours"]
                )
                if (
                    isinstance(hours, bool)
                    or not isinstance(hours, int)
                    or not 1 <= hours <= 168
                ):
                    raise ValueError("头像缓存时间必须为 1–168 小时的整数")
                settings = {
                    "manager_ids": list(dict.fromkeys(ids)),
                    "llm_tools_enabled": enabled,
                    "avatar_cache_hours": hours,
                }
                await self.db.execute(
                    "UPDATE settings SET body=? WHERE id=1", (json.dumps(settings),)
                )
        return settings

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
            # An unchanged cutoff may already have passed while review is still open.
            if not isinstance(payload, dict):
                raise ValueError("抽奖设置必须为对象。")
            previous = None
            if lottery_id:
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                previous = json.loads(row[0]) if row else None
            now = time.time()
            validation_time = now
            if (
                previous
                and timestamp(payload.get("close_at")) == previous["close_at"]
                and now >= previous["close_at"]
            ):
                validation_time = previous["close_at"] - 1
            rules = validate_lottery(payload, now=validation_time)
            if rules["draw_at"] <= now:
                raise ValueError("开奖时间必须晚于当前时间；立即开奖请使用开奖操作。")
            for filename in [
                rules["cover"],
                *(tier["image"] for tier in rules["prize_tiers"]),
            ]:
                if filename and not (self.directory / "artwork" / filename).is_file():
                    raise ValueError("封面或奖品图片不存在，请重新上传后保存。")
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
                if now >= item["draw_at"]:
                    raise ValueError(
                        "已到开奖时间，不能再修改活动，请刷新查看开奖结果。"
                    )
                async with self.db.execute(
                    "SELECT count(*) FROM entries WHERE lottery_id=?", (lottery_id,)
                ) as cursor:
                    count = (await cursor.fetchone())[0]
                if (count or item.get("tier_draws")) and (
                    rules["prize_tiers"] != prize_tiers(item)
                    or any(
                        rules[k] != item.get(k, True)
                        for k in (
                            "targets",
                            "questions",
                            "require_correct",
                        )
                    )
                ):
                    raise ValueError(
                        "已有报名或奖项开奖后，平台、群、问题、答题模式、奖项顺序、奖品图片和中奖人数不能修改。可修改封面、说明和未来时间。"
                    )
                previous_schedule = item.get(
                    "announcement_schedule",
                    {"mode": "off", "start_at": None, "interval_minutes": 0},
                )
                schedule_changed = rules["announcement_schedule"] != previous_schedule
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
                    "tier_draws": [],
                    "drawn_at": None,
                    "pool_hash": None,
                }
                schedule_changed = True
            schedule = rules["announcement_schedule"]
            if schedule_changed:
                if schedule["mode"] != "off" and schedule["start_at"] <= time.time():
                    raise ValueError("首次群公告通知时间必须晚于当前时间")
                item["announcement_next_at"] = schedule["start_at"]
                item["announcement_last_at"] = None
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                await self.db.execute(
                    "INSERT INTO lotteries VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body"
                    if editing
                    else "INSERT INTO lotteries VALUES (?, ?)",
                    (lottery_id, json.dumps(item, ensure_ascii=False)),
                )
                if not rules["group_success_notify"]:
                    await self.db.execute(
                        "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                        "AND json_extract(body,'$.target.channel')='group' AND "
                        "(json_extract(body,'$.kind')='success' OR "
                        "(json_extract(body,'$.kind')='review' AND json_extract(body,'$.entry.review_status')='approved'))",
                        (lottery_id,),
                    )
                if not rules["group_pending_notify"]:
                    await self.db.execute(
                        "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                        "AND json_extract(body,'$.target.channel')='group' AND "
                        "(json_extract(body,'$.kind')='submitted' OR "
                        "(json_extract(body,'$.kind')='review' AND json_extract(body,'$.entry.review_status')='pending'))",
                        (lottery_id,),
                    )
                if schedule_changed:
                    await self.db.execute(
                        "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                        "AND json_extract(body,'$.scheduled')=1",
                        (lottery_id,),
                    )
                if previous and previous["targets"] != rules["targets"]:
                    await self.db.execute(
                        "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                        "AND json_extract(body,'$.kind') IN ('announcement','participation_guide')",
                        (lottery_id,),
                    )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
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
                "SELECT e.body,s.expires_at FROM entries e LEFT JOIN sessions s "
                "ON s.lottery_id=e.lottery_id AND s.user_id=e.user_id "
                "AND s.bot_id=json_extract(e.body,'$.bot_id') "
                "WHERE e.lottery_id=? AND e.user_id=?",
                (lottery_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
        if not row:
            return None
        entry = json.loads(row[0])
        if row[1] is not None:
            entry["mode_expires_at"] = row[1]
        return entry

    async def enroll(
        self, lottery_id: str, identity: dict, *, restart_existing: bool = False
    ) -> tuple[dict, bool]:
        """Reserve one slot across all groups; finish immediately when no form is needed.

        Args:
            lottery_id: Activity identifier.
            identity: Trusted group, platform, bot, and actual QQ sender.
            restart_existing: Restart an unsuccessful original group entry from question one.

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
                async with self.db.execute(
                    "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                    (lottery_id, identity["user_id"]),
                ) as cursor:
                    row = await cursor.fetchone()
                if (
                    row
                    and restart_existing
                    and item["status"] != "cancelled"
                    and review_status(item, json.loads(row[0])) == "approved"
                ):
                    await self.db.commit()
                    return json.loads(row[0]), False
                if item["status"] != "open" or time.time() >= item["close_at"]:
                    raise ValueError("报名已截止，请查看抽奖详情。")
                if row:
                    entry = json.loads(row[0])
                    if (
                        restart_existing
                        and item["questions"]
                        and review_status(item, entry) != "approved"
                        and all(
                            entry[key] == identity[key]
                            for key in ("platform_id", "bot_id", "group_id")
                        )
                    ):
                        entry.update(
                            answers=[],
                            status="pending",
                            review_status="incomplete",
                            completed_at=None,
                            answer_revision=entry.get("answer_revision", 0) + 1,
                        )
                        entry.pop("last_answer_index", None)
                        for key in ("review_decision", "reviewed_at", "reviewed_by"):
                            entry.pop(key, None)
                        await self.db.execute(
                            "UPDATE entries SET body=? WHERE lottery_id=? AND user_id=?",
                            (
                                json.dumps(entry, ensure_ascii=False),
                                lottery_id,
                                identity["user_id"],
                            ),
                        )
                        await self.db.execute(
                            "INSERT OR REPLACE INTO sessions (bot_id,user_id,lottery_id,question_index,expires_at) VALUES (?,?,?,0,?)",
                            (
                                entry["bot_id"],
                                entry["user_id"],
                                lottery_id,
                                time.time() + FORM_MODE_SECONDS,
                            ),
                        )
                        await self.db.execute(
                            "DELETE FROM outbox WHERE delivered_at IS NULL AND json_extract(body,'$.entry.user_id')=? "
                            "AND (lottery_id=? AND json_extract(body,'$.kind') IN ('success','submitted','review','answer_changed') "
                            "OR json_extract(body,'$.target.bot_id')=? AND json_extract(body,'$.kind') IN ('question','form_timeout'))",
                            (entry["user_id"], lottery_id, entry["bot_id"]),
                        )
                        logger.info(
                            "Lottery %s unsuccessful group entry restarted from question one.",
                            lottery_id,
                        )
                    await self.db.commit()
                    return entry, False
                async with self.db.execute(
                    "SELECT count(*) FROM entries WHERE lottery_id=?", (lottery_id,)
                ) as cursor:
                    if (await cursor.fetchone())[0] >= 10000:
                        raise ValueError("报名人数已达上限。")
                entry = {
                    **identity,
                    "answers": [],
                    "answer_revision": 0,
                    "status": "pending" if item["questions"] else "complete",
                    "joined_at": time.time(),
                    "completed_at": None if item["questions"] else time.time(),
                }
                entry["review_status"] = review_status(item, entry)
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
                else:
                    await self.db.execute(
                        "INSERT OR REPLACE INTO sessions (bot_id,user_id,lottery_id,question_index,expires_at) VALUES (?,?,?,0,?)",
                        (
                            identity["bot_id"],
                            identity["user_id"],
                            lottery_id,
                            time.time() + FORM_MODE_SECONDS,
                        ),
                    )
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
        await self.expire_forms(bot_id=bot_id, user_id=user_id)
        async with self.lock:
            if lottery_id is not None:
                if not lottery_id:
                    await self.db.execute(
                        "DELETE FROM sessions WHERE bot_id=? AND user_id=?",
                        (bot_id, user_id),
                    )
                    await self.db.execute(
                        "DELETE FROM outbox WHERE delivered_at IS NULL AND json_extract(body,'$.kind')='question' "
                        "AND json_extract(body,'$.target.bot_id')=? AND json_extract(body,'$.entry.user_id')=?",
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
                    "INSERT OR REPLACE INTO sessions (bot_id, user_id, lottery_id, question_index,expires_at) VALUES (?, ?, ?, ?,?)",
                    (
                        bot_id,
                        user_id,
                        lottery_id,
                        form_progress(entry, len(item["questions"]))[1],
                        time.time() + FORM_MODE_SECONDS,
                    ),
                )
                await self.db.execute(
                    "DELETE FROM outbox WHERE delivered_at IS NULL AND json_extract(body,'$.kind')='form_timeout' "
                    "AND json_extract(body,'$.target.bot_id')=? AND json_extract(body,'$.entry.user_id')=?",
                    (bot_id, user_id),
                )
            async with self.db.execute(
                "SELECT s.lottery_id, l.body FROM sessions s "
                "JOIN entries e ON e.lottery_id=s.lottery_id AND e.user_id=s.user_id "
                "JOIN lotteries l ON l.id=s.lottery_id "
                "WHERE s.bot_id=? AND s.user_id=? "
                "AND json_extract(e.body,'$.status')='pending' "
                "AND json_extract(l.body,'$.status')='open'",
                (bot_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
            # Older SQLite JSON parsers can round fractional timestamps differently.
            if row is not None and time.time() >= json.loads(row[1])["close_at"]:
                row = None
            if row is None:
                await self.db.execute(
                    "DELETE FROM sessions WHERE bot_id=? AND user_id=?",
                    (bot_id, user_id),
                )
        return row[0] if row else None

    async def expire_forms(
        self, *, bot_id: str = "", user_id: str = "", now: float | None = None
    ) -> int:
        """Exit expired answer modes and durably queue one private reminder per mode.

        Args:
            bot_id: Optional original bot for a sender-scoped expiry check.
            user_id: Optional actual sender paired with bot_id.
            now: Testable UTC deadline; defaults to current time.

        Returns:
            Number of modes expired, limited to 200 per scheduler cycle.
        """
        now = time.time() if now is None else now
        selection = " AND s.bot_id=? AND s.user_id=?" if bot_id and user_id else ""
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT s.bot_id,s.user_id,s.lottery_id,e.body,l.body FROM sessions s "
                    "JOIN entries e ON e.lottery_id=s.lottery_id AND e.user_id=s.user_id "
                    "JOIN lotteries l ON l.id=s.lottery_id WHERE s.expires_at<=?"
                    + selection
                    + " LIMIT 200",
                    (now, bot_id, user_id) if selection else (now,),
                ) as cursor:
                    rows = await cursor.fetchall()
                for row in rows:
                    await self.db.execute(
                        "DELETE FROM sessions WHERE bot_id=? AND user_id=?",
                        (row[0], row[1]),
                    )
                    await self.db.execute(
                        "DELETE FROM outbox WHERE delivered_at IS NULL AND json_extract(body,'$.kind')='question' "
                        "AND json_extract(body,'$.target.bot_id')=? AND json_extract(body,'$.entry.user_id')=?",
                        (row[0], row[1]),
                    )
                    entry, item = json.loads(row[3]), json.loads(row[4])
                    if entry["status"] == "pending":
                        await self.queue(item, "form_timeout", entry=entry)
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        if rows:
            logger.info("Expired %d private lottery answer modes.", len(rows))
        return len(rows)

    async def form_position(self, bot_id: str, user_id: str, direction: int = 0) -> int:
        """Read or move the sender's private question cursor without skipping answers.

        Args:
            bot_id: Trusted robot QQ number.
            user_id: Trusted sender QQ number.
            direction: Zero to read, minus one for previous, or one for next.

        Returns:
            Current zero-based question index.

        Raises:
            ValueError: There is no active form or the requested question is unavailable.
        """
        if direction not in (-1, 0, 1):
            raise ValueError("题目方向无效")
        async with self.lock:
            async with self.db.execute(
                "SELECT s.question_index, e.body, l.body,s.expires_at FROM sessions s "
                "JOIN entries e ON e.lottery_id=s.lottery_id AND e.user_id=s.user_id "
                "JOIN lotteries l ON l.id=s.lottery_id WHERE s.bot_id=? AND s.user_id=?",
                (bot_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                raise ValueError("没有正在回答的题目，请私聊发送 /抽奖 继续")
            entry, item = json.loads(row[1]), json.loads(row[2])
            if (
                entry["status"] != "pending"
                or item["status"] != "open"
                or time.time() >= item["close_at"]
                or time.time() >= row[3]
            ):
                raise ValueError("资料已提交或报名已截止，不能继续回答")
            index = (
                row[0]
                if row[0] is not None
                else form_progress(entry, len(item["questions"]))[1]
            ) + direction
            if (
                not 0 <= index < len(item["questions"])
                or index > form_progress(entry, len(item["questions"]))[1]
            ):
                raise ValueError("没有可切换的题目，请先回答当前题")
            if direction:
                await self.db.execute(
                    "UPDATE sessions SET question_index=? WHERE bot_id=? AND user_id=?",
                    (index, bot_id, user_id),
                )
            return index

    async def queue_question(self, lottery_id: str, bot_id: str, user_id: str) -> None:
        """Queue the active question to the enrolled QQ user's private conversation.

        Args:
            lottery_id: Group-created reservation identifier.
            bot_id: Original robot QQ number.
            user_id: Original group sender QQ number.
        """
        async with self.lock:
            async with self.db.execute(
                "SELECT s.question_index,e.body,l.body FROM sessions s "
                "JOIN entries e ON e.lottery_id=s.lottery_id AND e.user_id=s.user_id "
                "JOIN lotteries l ON l.id=s.lottery_id WHERE s.lottery_id=? AND s.bot_id=? AND s.user_id=?",
                (lottery_id, bot_id, user_id),
            ) as cursor:
                row = await cursor.fetchone()
            if row:
                entry, item = json.loads(row[1]), json.loads(row[2])
                entry["question_index"] = (
                    row[0]
                    if row[0] is not None
                    else form_progress(entry, len(item["questions"]))[1]
                )
                await self.db.execute(
                    "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                    "AND json_extract(body,'$.kind')='question' AND json_extract(body,'$.entry.user_id')=?",
                    (lottery_id, user_id),
                )
                await self.queue(item, "question", entry=entry)

    async def answer(
        self,
        lottery_id: str,
        identity: dict,
        answer: dict,
        index: int,
        *,
        mode_expires_at: float | None = None,
    ) -> tuple[dict, dict]:
        """Accept the active private question and commit completion atomically.

        Args:
            lottery_id: Previously selected activity identifier.
            identity: Actual sender, bot, and platform; group_id must be empty.
            answer: Original text and an optional internal image filename.
            index: Expected question index to reject simultaneous stale submissions.
            mode_expires_at: Original mode deadline to reject uploads from a replaced mode.

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
                    raise ValueError("请用报名时的 QQ 号，私聊报名时联系的我填写。")
                if item["status"] != "open" or time.time() >= item["close_at"]:
                    raise ValueError("报名截止，未完成的资料不会进入开奖名单。")
                async with self.db.execute(
                    "SELECT question_index,lottery_id,expires_at FROM sessions WHERE bot_id=? AND user_id=?",
                    (identity["bot_id"], identity["user_id"]),
                ) as cursor:
                    session = await cursor.fetchone()
                if not session:
                    raise ValueError("当前回答已暂停，请私聊发送 /抽奖 继续 恢复")
                if time.time() >= session[2]:
                    raise ValueError("已退出抽奖作答模式，请私聊发送 /抽奖 继续 恢复")
                if mode_expires_at is not None and mode_expires_at != session[2]:
                    raise ValueError("作答模式已重新开启，请按最新题目重新回答")
                expected = (
                    session[0]
                    if session[0] is not None
                    else form_progress(entry, len(item["questions"]))[1]
                )
                if session[1] != lottery_id:
                    raise ValueError("已切换到另一场活动，请按最新题目卡片回答")
                if (
                    entry["status"] != "pending"
                    or index != expected
                    or not 0 <= index < len(item["questions"])
                    or index > form_progress(entry, len(item["questions"]))[1]
                ):
                    raise ValueError("这题已处理，请按最新的问题卡片作答。")
                question = item["questions"][index]
                answer = validate_answer(
                    question, answer, item.get("require_correct", True)
                )
                discarded = set()
                if index < len(entry["answers"]):
                    old = entry["answers"][index]
                    discarded.update(
                        set(answer_images(old)) - set(answer_images(answer))
                    )
                    entry["answers"][index] = answer
                else:
                    entry["answers"].append(answer)
                await self.db.execute(
                    "UPDATE sessions SET question_index=? WHERE bot_id=? AND user_id=? AND lottery_id=?",
                    (
                        form_progress(entry, len(item["questions"]))[1],
                        identity["bot_id"],
                        identity["user_id"],
                        lottery_id,
                    ),
                )
                await self.db.execute(
                    "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                    "AND json_extract(body,'$.kind')='question' AND json_extract(body,'$.entry.user_id')=?",
                    (lottery_id, identity["user_id"]),
                )
                entry["answer_revision"] = entry.get("answer_revision", 0) + 1
                entry["last_answer_index"] = index
                if form_progress(entry, len(item["questions"]))[0] == len(
                    item["questions"]
                ):
                    entry["status"] = "complete"
                    entry["completed_at"] = time.time()
                    entry["review_status"] = review_status(item, entry)
                    await self.db.execute(
                        "DELETE FROM sessions WHERE bot_id=? AND user_id=?",
                        (identity["bot_id"], identity["user_id"]),
                    )
                    await self.queue(
                        item,
                        "success"
                        if entry["review_status"] == "approved"
                        else "submitted",
                        entry=entry,
                    )
                await self.db.execute(
                    "UPDATE entries SET body=? WHERE lottery_id=? AND user_id=?",
                    (
                        json.dumps(entry, ensure_ascii=False),
                        lottery_id,
                        identity["user_id"],
                    ),
                )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        if discarded:
            await self.cleanup_artwork(discarded)
        return item, entry

    async def review(self, lottery_id: str, payload: dict, reviewer_id: str) -> dict:
        """Mark submitted answers or match text references before the draw freezes eligibility.

        Args:
            lottery_id: Activity identifier.
            payload: mark with user_id, question_index, and correct; or match.
            reviewer_id: Authenticated Dashboard operator for the private audit trail.

        Returns:
            Counts of marked answers, changed participants, and approved participants.

        Raises:
            ValueError: The activity, review mode, participant, or mark is invalid.
        """
        if not isinstance(payload, dict) or payload.get("action") not in (
            "mark",
            "match",
            "bulk",
            "decision",
        ):
            raise ValueError("请选择整份资料审核或文字答案匹配")
        action = payload["action"]
        selected_ids = []
        if action == "bulk":
            selected_ids = payload.get("user_ids")
            correct = payload.get("correct")
            if (
                not isinstance(selected_ids, list)
                or not 1 <= len(selected_ids) <= 100
                or any(
                    not isinstance(value, str)
                    or not re.fullmatch(r"[1-9][0-9]{4,19}", value)
                    for value in selected_ids
                )
                or not isinstance(correct, bool)
            ):
                raise ValueError("请明确选择 1–100 份已提交报名及通过或不通过标记")
            selected_ids = list(dict.fromkeys(selected_ids))
            if "revisions" in payload and not isinstance(payload["revisions"], dict):
                raise ValueError("资料版本无效，请刷新列表后操作")
        if action in {"mark", "decision"}:
            user_id = payload.get("user_id")
            index = payload.get("question_index", 0)
            correct = payload.get("correct")
            if (
                not isinstance(user_id, str)
                or not re.fullmatch(r"[1-9][0-9]{4,19}", user_id)
                or not isinstance(index, int)
                or isinstance(index, bool)
                or "correct" not in payload
                or (correct is not None and not isinstance(correct, bool))
                or action == "decision"
                and not isinstance(correct, bool)
            ):
                raise ValueError("请指定报名 QQ、题目序号及待审核／正确／错误标记。")
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
                if item["status"] != "open" or time.time() >= item["draw_at"]:
                    raise ValueError("开奖时间已到或活动已结束，审核资格已锁定。")
                winner_ids = {winner["user_id"] for winner in item["winners"]}
                if action in {"mark", "decision"} and user_id in winner_ids:
                    raise ValueError(
                        "该报名已获得提前开奖的奖项，资格与答案标记已锁定。"
                    )
                if item.get("require_correct", True):
                    raise ValueError("本场采用当场答对模式，无需后续审核。")
                if action in {"mark", "decision"}:
                    selected_ids = [user_id]
                selection = (
                    " AND user_id IN (" + ",".join("?" for _ in selected_ids) + ")"
                    if selected_ids
                    else ""
                )
                async with self.db.execute(
                    "SELECT body FROM entries WHERE lottery_id=?" + selection,
                    (lottery_id, *selected_ids),
                ) as cursor:
                    entries = [json.loads(row[0]) for row in await cursor.fetchall()]
                if action in {"bulk", "decision"} and (
                    len(entries) != len(selected_ids)
                    or any(
                        entry["status"] != "complete" or entry["user_id"] in winner_ids
                        for entry in entries
                    )
                ):
                    raise ValueError(
                        "所选报名中有未提交、已删除或已中奖者，请刷新列表后重试"
                    )
                if action in {"mark", "decision"}:
                    entries = [
                        entry for entry in entries if entry["user_id"] == user_id
                    ]
                    if not entries or entries[0]["status"] != "complete":
                        raise ValueError("只能审核已经提交全部资料的报名。")
                    if not 0 <= index < len(item["questions"]):
                        raise ValueError("题目序号无效。")
                changed = marked = approved = 0
                for entry in entries:
                    if entry["status"] != "complete" or entry["user_id"] in winner_ids:
                        continue
                    revision = (
                        payload.get("revision")
                        if action == "decision"
                        else payload.get("revisions", {}).get(entry["user_id"])
                        if action == "bulk"
                        else None
                    )
                    if revision is not None and (
                        type(revision) is not int
                        or revision != entry.get("answer_revision", 0)
                    ):
                        raise ValueError("资料已经更新，请刷新后重新审核整份资料")
                    previous = entry.get("review_status", "approved")
                    if action == "match" and isinstance(
                        entry.get("review_decision"), bool
                    ):
                        continue
                    if action in {"decision", "bulk"}:
                        entry.update(
                            review_decision=correct,
                            reviewed_by=reviewer_id,
                            reviewed_at=time.time(),
                        )
                    entry["answer_revision"] = entry.get("answer_revision", 0) + 1
                    for question_index, answer in enumerate(entry["answers"]):
                        if action in {"decision", "bulk"}:
                            continue
                        if action in {"mark", "bulk"}:
                            if action == "mark" and question_index != index:
                                continue
                            result = correct
                        else:
                            question = item["questions"][question_index]
                            if (
                                question["kind"] != "quiz"
                                or not question["answers"]
                                or answer.get("correct") is not None
                                or answer.get("image")
                            ):
                                continue
                            text = answer["value"].strip()
                            options = question["options"]
                            choice = unicodedata.normalize("NFKC", text).upper()
                            if options and choice in [
                                chr(65 + n) for n in range(len(options))
                            ]:
                                text = options[ord(choice) - 65]
                            elif (
                                options
                                and choice.isdecimal()
                                and 1 <= int(choice) <= len(options)
                            ):
                                text = options[int(choice) - 1]
                            result = unicodedata.normalize(
                                "NFKC", text
                            ).strip().casefold() in {
                                unicodedata.normalize("NFKC", value).strip().casefold()
                                for value in question["answers"]
                            }
                        answer.update(
                            correct=result,
                            review_method=action,
                            reviewed_by=reviewer_id,
                            reviewed_at=time.time(),
                        )
                        marked += 1
                    entry["review_status"] = review_status(item, entry)
                    approved += entry["review_status"] == "approved"
                    if previous != entry["review_status"]:
                        changed += 1
                        # Supersede undelivered submission/review cards with the current eligibility.
                        await self.db.execute(
                            "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL AND json_extract(body, '$.entry.user_id')=?",
                            (lottery_id, entry["user_id"]),
                        )
                        await self.queue(item, "review", entry=entry)
                    await self.db.execute(
                        "UPDATE entries SET body=? WHERE lottery_id=? AND user_id=?",
                        (
                            json.dumps(entry, ensure_ascii=False),
                            lottery_id,
                            entry["user_id"],
                        ),
                    )
                await self.db.commit()
                return {
                    "marked_answers": marked,
                    "changed_entries": changed,
                    "approved_entries": approved,
                }
            except BaseException:
                await self.db.rollback()
                raise

    async def manage_answers(
        self, lottery_id: str, payload: dict, reviewer_id: str
    ) -> dict:
        """Edit or delete private answers atomically and invalidate stale eligibility.

        Args:
            lottery_id: Activity being managed.
            payload: Edit, single-answer deletion, or selected participants' data deletion.
            reviewer_id: Authenticated operator retained in the audit metadata.

        Returns:
            Changed participant and queued private notification counts.

        Raises:
            ValueError: Selection, revision, attachment, or frozen eligibility is invalid.
        """
        action = payload.get("action") if isinstance(payload, dict) else None
        if not isinstance(action, str) or action not in {
            "edit_answer",
            "delete_answer",
            "delete_entries",
        }:
            raise ValueError("请选择编辑答案或删除填写资料")
        ids = (
            payload.get("user_ids")
            if action == "delete_entries"
            else [payload.get("user_id")]
        )
        revisions = (
            payload.get("revisions", {})
            if action == "delete_entries"
            else {payload.get("user_id"): payload.get("revision")}
        )
        notify = payload.get("notify")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 100
            or any(
                not isinstance(user, str) or not re.fullmatch(r"[1-9][0-9]{4,19}", user)
                for user in ids
            )
            or len(set(ids)) != len(ids)
            or not isinstance(revisions, dict)
            or any(
                type(revisions.get(user)) is not int or revisions[user] < 0
                for user in ids
            )
            or not isinstance(notify, bool)
        ):
            raise ValueError(
                "请选择 1–100 位参与者，刷新资料后重试，并明确是否通知用户"
            )
        if action != "edit_answer" and payload.get("confirmed") is not True:
            raise ValueError("删除资料前请确认是否通知用户")
        index = payload.get("question_index")
        if action != "delete_entries" and (type(index) is not int or index < 0):
            raise ValueError("题目序号无效")
        discarded = set()
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row:
                    raise ValueError("抽奖不存在")
                item = json.loads(row[0])
                if item["status"] != "open" or time.time() >= item["draw_at"]:
                    raise ValueError("活动已结束或开奖时间已到，资料已锁定")
                winners = {winner["user_id"] for winner in item["winners"]}
                entries = []
                for user in ids:
                    async with self.db.execute(
                        "SELECT body FROM entries WHERE lottery_id=? AND user_id=?",
                        (lottery_id, user),
                    ) as cursor:
                        row = await cursor.fetchone()
                    if not row or user in winners:
                        raise ValueError(
                            "所选参与者已移除或已中奖，无法修改资料，请刷新列表"
                        )
                    entry = json.loads(row[0])
                    if revisions[user] != entry.get("answer_revision", 0):
                        raise ValueError(
                            "资料已被用户或其他管理员更新，请重新打开后操作"
                        )
                    if action != "delete_entries" and not 0 <= index < min(
                        len(entry["answers"]), len(item["questions"])
                    ):
                        raise ValueError("本题尚未填写，无法编辑或删除")
                    if (
                        action == "delete_answer"
                        and entry["answers"][index]["kind"] == "deleted"
                    ):
                        raise ValueError("本题答案已经删除，请刷新资料")
                    if (
                        action == "delete_entries"
                        and not form_progress(entry, len(item["questions"]))[0]
                    ):
                        raise ValueError(
                            "所选参与者中有人尚未填写资料，请刷新并重新选择"
                        )
                    entries.append(entry)
                for entry in entries:
                    slots = (
                        [index]
                        if action != "delete_entries"
                        else range(len(entry["answers"]))
                    )
                    for position in slots:
                        previous = entry["answers"][position]
                        old_images = set(answer_images(previous))
                        if action == "edit_answer":
                            answer = validate_answer(
                                item["questions"][position],
                                payload.get("answer"),
                                item.get("require_correct", True),
                            )
                            images = set(answer_images(answer))
                            if any(
                                not (self.directory / "uploads" / image).is_file()
                                for image in images
                            ):
                                raise ValueError("答案图片已失效，请重新上传")
                            if images - old_images:
                                async with self.db.execute(
                                    "SELECT body FROM entries"
                                ) as cursor:
                                    for row in await cursor.fetchall():
                                        if any(
                                            (images - old_images)
                                            & set(answer_images(value))
                                            for value in json.loads(row[0])["answers"]
                                        ):
                                            raise ValueError(
                                                "该图片已用于其他答案，请为当前用户单独上传"
                                            )
                            answer.update(edited_by=reviewer_id, edited_at=time.time())
                        else:
                            images = set()
                            answer = {
                                "kind": "deleted",
                                "value": "",
                                "deleted_by": reviewer_id,
                                "deleted_at": time.time(),
                            }
                        discarded.update(old_images - images)
                        entry["answers"][position] = answer
                    entry.pop("review_decision", None)
                    entry.pop("reviewed_by", None)
                    entry.pop("reviewed_at", None)
                    # Every edit requires a fresh whole-form review in deferred mode.
                    if not item.get("require_correct", True):
                        for answer in entry["answers"]:
                            if answer["kind"] != "deleted":
                                answer["correct"] = None
                                for key in (
                                    "review_method",
                                    "reviewed_by",
                                    "reviewed_at",
                                ):
                                    answer.pop(key, None)
                    count, _ = form_progress(entry, len(item["questions"]))
                    entry["status"] = (
                        "complete" if count == len(item["questions"]) else "pending"
                    )
                    entry["completed_at"] = (
                        time.time() if entry["status"] == "complete" else None
                    )
                    entry["review_status"] = review_status(item, entry)
                    entry["answer_revision"] = entry.get("answer_revision", 0) + 1
                    await self.db.execute(
                        "UPDATE entries SET body=? WHERE lottery_id=? AND user_id=?",
                        (
                            json.dumps(entry, ensure_ascii=False),
                            lottery_id,
                            entry["user_id"],
                        ),
                    )
                    await self.db.execute(
                        "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL AND json_extract(body,'$.entry.user_id')=?",
                        (lottery_id, entry["user_id"]),
                    )
                    if entry["status"] == "complete":
                        await self.db.execute(
                            "DELETE FROM sessions WHERE lottery_id=? AND user_id=?",
                            (lottery_id, entry["user_id"]),
                        )
                    else:
                        # Pause stale prompts so a reply to the old question cannot fill a deleted slot.
                        await self.db.execute(
                            "DELETE FROM sessions WHERE lottery_id=? AND user_id=?",
                            (lottery_id, entry["user_id"]),
                        )
                    if notify:
                        await self.queue(item, "answer_changed", entry=entry)
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        await self.cleanup_artwork(discarded)
        logger.info(
            "Lottery %s answer action=%s; participants=%d; operator=%s; notify=%s.",
            lottery_id,
            action,
            len(entries),
            reviewer_id,
            notify,
        )
        return {
            "changed_entries": len(entries),
            "private_notices": len(entries) if notify else 0,
            "marked_answers": 0,
        }

    async def restart_forms(
        self,
        lottery_id: str | None = None,
        *,
        bot_id: str = "",
        user_id: str = "",
        platform_id: str = "",
    ) -> dict:
        """Restart incomplete group reservations after friendship or explicit management.

        Args:
            lottery_id: Explicit WebUI activity, or None for a trusted friend-add notice.
            bot_id: Trusted friend's bot account when handling a notice.
            user_id: Trusted QQ account when handling a notice.
            platform_id: Trusted adapter identifier when handling a notice.

        Returns:
            Number of users whose next question was queued.

        Raises:
            ValueError: An explicitly selected activity no longer accepts answers.
        """
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                if lottery_id is not None:
                    async with self.db.execute(
                        "SELECT body FROM lotteries WHERE id=?", (lottery_id,)
                    ) as cursor:
                        row = await cursor.fetchone()
                    if (
                        not row
                        or json.loads(row[0])["status"] != "open"
                        or time.time() >= json.loads(row[0])["close_at"]
                    ):
                        raise ValueError("报名已截止或活动已结束，不能重新发起填写")
                async with self.db.execute(
                    "SELECT e.lottery_id,e.body,l.body FROM entries e JOIN lotteries l ON l.id=e.lottery_id LEFT JOIN sessions s ON s.bot_id=json_extract(e.body,'$.bot_id') AND s.user_id=e.user_id WHERE json_extract(e.body,'$.status')='pending' ORDER BY CASE WHEN s.lottery_id=e.lottery_id THEN 0 ELSE 1 END,json_extract(e.body,'$.joined_at'),e.lottery_id"
                ) as cursor:
                    rows = await cursor.fetchall()
                selected = {}
                for identifier, body, rules in rows:
                    entry, item = json.loads(body), json.loads(rules)
                    if (
                        lottery_id is not None
                        and lottery_id != identifier
                        or item["status"] != "open"
                        or time.time() >= item["close_at"]
                        or bot_id
                        and entry["bot_id"] != bot_id
                        or user_id
                        and entry["user_id"] != user_id
                        or platform_id
                        and entry["platform_id"] != platform_id
                    ):
                        continue
                    key = (entry["bot_id"], entry["user_id"])
                    if key in selected:
                        continue
                    missing = form_progress(entry, len(item["questions"]))[1]
                    if missing >= len(item["questions"]):
                        continue
                    selected[key] = identifier
                    await self.db.execute(
                        "INSERT OR REPLACE INTO sessions (bot_id,user_id,lottery_id,question_index,expires_at) VALUES (?,?,?,?,?)",
                        (*key, identifier, missing, time.time() + FORM_MODE_SECONDS),
                    )
                    # Replace backoff jobs so a new friendship does not inherit failed delivery delays.
                    await self.db.execute(
                        "DELETE FROM outbox WHERE delivered_at IS NULL AND json_extract(body,'$.kind')='question' AND json_extract(body,'$.target.bot_id')=? AND json_extract(body,'$.entry.user_id')=?",
                        key,
                    )
                    await self.queue(
                        item, "question", entry={**entry, "question_index": missing}
                    )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        logger.info(
            "Lottery private forms restarted; activity=%s; users=%d.",
            lottery_id or "friendship",
            len(selected),
        )
        return {"restarted_users": len(selected)}

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
            if any(
                winner["user_id"] == identity["user_id"] for winner in item["winners"]
            ):
                raise ValueError("你已提前中奖，不能退出或重复报名。")
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
        await self.cleanup_artwork(
            {image for answer in entry["answers"] for image in answer_images(answer)}
        )

    async def queue(
        self,
        item: dict,
        kind: str,
        *,
        entry: dict | None = None,
        scheduled: bool = False,
    ) -> None:
        """Enqueue delivery inside an existing lock and transaction.

        Args:
            item: Activity snapshot, frozen for this message.
            kind: Announcement, result, cancellation, or enrollment success.
            entry: The completed user's trusted routing data for success.
            scheduled: Mark automatic reminders so changes can revoke unsent jobs.
        """
        targets = item["targets"]
        if entry is not None:
            targets = [
                {**entry, "channel": "group", "recipient": entry["group_id"]},
                {**entry, "channel": "private", "recipient": entry["user_id"]},
            ]
            if (
                kind in {"question", "answer_changed", "form_timeout"}
                or (
                    not item.get("group_success_notify", True)
                    and (
                        kind == "success"
                        or kind == "review"
                        and entry.get("review_status") == "approved"
                    )
                )
                or (
                    not item.get("group_pending_notify", True)
                    and (
                        kind == "submitted"
                        or kind == "review"
                        and entry.get("review_status") == "pending"
                    )
                )
            ):
                targets = targets[1:]
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
            if scheduled:
                message["scheduled"] = True
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
            if kind == "announcement":
                guide = {
                    **message,
                    "kind": "participation_guide",
                    "depends_on": delivery_id,
                }
                await self.db.execute(
                    "INSERT INTO outbox (id, lottery_id, body) VALUES (?, ?, ?)",
                    (
                        secrets.token_hex(16),
                        item["id"],
                        json.dumps(guide, ensure_ascii=False),
                    ),
                )

    async def schedule_announcements(self, *, now: float | None = None) -> list[str]:
        """Atomically queue due group announcements without replaying missed cycles.

        Args:
            now: Optional Unix clock for scheduling and deterministic tests.

        Returns:
            Activity IDs whose announcement images and separate text were queued.

        Raises:
            sqlite3.Error: The transaction cannot be persisted.
        """
        now = time.time() if now is None else now
        queued = []
        async with self.lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                async with self.db.execute(
                    "SELECT body FROM lotteries WHERE json_extract(body,'$.status')='open' "
                    "AND json_extract(body,'$.announcement_next_at') IS NOT NULL"
                ) as cursor:
                    items = [json.loads(row[0]) for row in await cursor.fetchall()]
                for item in items:
                    # Compare Python-decoded values to preserve the exact deadline on all OSes.
                    if item["announcement_next_at"] > now:
                        continue
                    schedule = item["announcement_schedule"]
                    next_at = None
                    if now < item["close_at"] and schedule["mode"] != "off":
                        async with self.db.execute(
                            "SELECT json_extract(body,'$.target.platform_id'), "
                            "json_extract(body,'$.target.bot_id'), json_extract(body,'$.target.recipient') "
                            "FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                            "AND json_extract(body,'$.kind') IN ('announcement','participation_guide')",
                            (item["id"],),
                        ) as cursor:
                            waiting = {tuple(row) for row in await cursor.fetchall()}
                        targets = [
                            target
                            for target in item["targets"]
                            if (
                                target["platform_id"],
                                target["bot_id"],
                                target["group_id"],
                            )
                            not in waiting
                        ]
                        if targets:
                            await self.queue(
                                {**item, "targets": targets},
                                "announcement",
                                scheduled=True,
                            )
                            item["announcement_last_at"] = now
                            queued.append(item["id"])
                        if schedule["mode"] == "repeat":
                            # Advance from the configured start, never replaying missed ticks.
                            interval = schedule["interval_minutes"] * 60
                            next_at = (
                                schedule["start_at"]
                                + (int((now - schedule["start_at"]) // interval) + 1)
                                * interval
                            )
                            if next_at >= item["close_at"]:
                                next_at = None
                    item["announcement_next_at"] = next_at
                    await self.db.execute(
                        "UPDATE lotteries SET body=? WHERE id=?",
                        (json.dumps(item, ensure_ascii=False), item["id"]),
                    )
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise
        return queued

    async def action(
        self,
        lottery_id: str,
        action: str,
        *,
        due_only: bool = False,
        tier_index: int | None = None,
    ) -> dict:
        """Finalize winners once and persist announcements before attempting network IO.

        Args:
            lottery_id: Activity identifier.
            action: publish, close, draw, draw_tier, or cancel.
            due_only: Require scheduled draw time to have arrived.
            tier_index: Zero-based award index, required only for draw_tier.

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
                if action in {"draw", "draw_tier"}:
                    if due_only and time.time() < item["draw_at"]:
                        raise ValueError("尚未到开奖时间。")
                    tiers = prize_tiers(item)
                    tier_draws = item.setdefault("tier_draws", [])
                    already_drawn = {record["tier_index"] for record in tier_draws}
                    if action == "draw_tier":
                        if (
                            isinstance(tier_index, bool)
                            or not isinstance(tier_index, int)
                            or not 0 <= tier_index < len(tiers)
                        ):
                            raise ValueError("请选择本场抽奖的有效奖项。")
                        if tier_index in already_drawn:
                            raise ValueError("该奖项已经开奖，不能再次抽取。")
                        if time.time() >= item["draw_at"]:
                            raise ValueError(
                                "已到自动开奖时间，请等待全部剩余奖项开奖。"
                            )
                        indexes = [tier_index]
                    else:
                        indexes = [
                            index
                            for index in range(len(tiers))
                            if index not in already_drawn
                        ]
                    async with self.db.execute(
                        "SELECT body FROM entries WHERE lottery_id=? ORDER BY user_id",
                        (lottery_id,),
                    ) as cursor:
                        pool = [json.loads(row[0]) for row in await cursor.fetchall()]
                    pool = [
                        e
                        for e in pool
                        if e["status"] == "complete"
                        and e.get("review_status", "approved") == "approved"
                    ]
                    item["pool_hash"] = hashlib.sha256(
                        "\n".join(e["user_id"] for e in pool).encode()
                    ).hexdigest()
                    winner_ids = {winner["user_id"] for winner in item["winners"]}
                    available = [
                        entry for entry in pool if entry["user_id"] not in winner_ids
                    ]
                    draw_time = time.time()
                    for index in indexes:
                        tier = tiers[index]
                        selected = secrets.SystemRandom().sample(
                            available, min(tier["count"], len(available))
                        )
                        tier_draws.append(
                            {
                                "tier_index": index,
                                "drawn_at": draw_time,
                                "eligible_count": len(available),
                                "winner_count": len(selected),
                                "pool_hash": hashlib.sha256(
                                    "\n".join(
                                        entry["user_id"] for entry in available
                                    ).encode()
                                ).hexdigest(),
                            }
                        )
                        item["winners"].extend(
                            {
                                "user_id": entry["user_id"],
                                "nickname": entry["nickname"],
                                "tier_index": index,
                                "tier_name": tier["name"],
                                "prize": tier["prize"],
                            }
                            for entry in selected
                        )
                        winner_ids.update(entry["user_id"] for entry in selected)
                        available = [
                            entry
                            for entry in available
                            if entry["user_id"] not in winner_ids
                        ]
                    item["winners"].sort(key=lambda winner: winner["tier_index"])
                    item["prize_tiers"] = tiers
                    item["eligible_count"] = len(pool)
                    if len(tier_draws) == len(tiers):
                        item["status"] = "drawn"
                        item["drawn_at"] = draw_time
                        item["close_at"] = min(item["close_at"], draw_time)
                        await self.queue(item, "result")
                    else:
                        await self.queue(item, "tier_result")
                elif action == "cancel":
                    if item.get("tier_draws"):
                        raise ValueError(
                            "已有奖项开奖，不能取消活动。可截止报名或抽取剩余奖项。"
                        )
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
                if action in {"draw", "cancel", "close"} or item["status"] == "drawn":
                    item["announcement_next_at"] = None
                    await self.db.execute(
                        "DELETE FROM outbox WHERE lottery_id=? AND delivered_at IS NULL "
                        "AND json_extract(body,'$.scheduled')=1",
                        (lottery_id,),
                    )
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
                "SELECT o.* FROM outbox o WHERE o.delivered_at IS NULL AND o.next_at<=? "
                "AND (json_extract(o.body,'$.depends_on') IS NULL OR EXISTS "
                "(SELECT 1 FROM outbox parent WHERE parent.id=json_extract(o.body,'$.depends_on') "
                "AND parent.delivered_at IS NOT NULL)) ORDER BY o.rowid LIMIT 40",
                (time.time(),),
            ) as cursor:
                return [dict(row) for row in await cursor.fetchall()]

    async def delivery_done(
        self, delivery_id: str, error: str = "", *, skipped: bool = False
    ) -> None:
        """Record success or back off a failed network attempt.

        Args:
            delivery_id: Outbox message identifier.
            error: A safe error category, empty for successful delivery.
            skipped: Remove an obsolete notice and its dependent text without claiming delivery.
        """
        async with self.lock:
            if skipped:
                await self.db.execute(
                    "DELETE FROM outbox WHERE delivered_at IS NULL AND "
                    "(id=? OR json_extract(body,'$.depends_on')=?)",
                    (delivery_id, delivery_id),
                )
            elif error:
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

    async def manage_entries(
        self,
        lottery_id: str,
        *,
        page: int | None = None,
        page_size: int = 20,
        status: str = "all",
        query: str = "",
        include_deliveries: bool = True,
    ) -> dict:
        """Read private forms and per-target delivery state for an authenticated operator.

        Args:
            lottery_id: Activity identifier.
            page: One-based page, or None for an internal full-record read.
            page_size: Bounded list size, limited to 10, 20, or 50.
            status: Eligibility filter, including incomplete submissions.
            query: Literal nickname, QQ, or origin group substring.
            include_deliveries: Include the latest 200 notification records.

        Returns:
            Enrollment and delivery records with correct answers excluded.
        """
        if page is not None and (
            isinstance(page, bool)
            or not isinstance(page, int)
            or not 1 <= page <= 10000
        ):
            raise ValueError("页码必须为 1–10000 的整数")
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or page_size not in {10, 20, 50}
        ):
            raise ValueError("每页支持 10、20 或 50 份报名")
        if not isinstance(status, str) or status not in {
            "all",
            "pending",
            "approved",
            "rejected",
            "incomplete",
        }:
            raise ValueError("报名筛选条件无效")
        if not isinstance(query, str) or len(query) > 80:
            raise ValueError("搜索内容不能超过 80 字")
        filters, values = "lottery_id=?", [lottery_id]
        if status == "incomplete":
            filters += " AND json_extract(body, '$.status')!='complete'"
        elif status != "all":
            filters += " AND json_extract(body, '$.status')='complete' AND coalesce(json_extract(body, '$.review_status'), 'approved')=?"
            values.append(status)
        if query.strip():
            pattern = (
                "%"
                + query.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
                + "%"
            )
            filters += " AND (user_id LIKE ? ESCAPE '!' OR json_extract(body, '$.nickname') LIKE ? ESCAPE '!' OR json_extract(body, '$.group_id') LIKE ? ESCAPE '!')"
            values.extend([pattern] * 3)
        async with self.lock:
            async with self.db.execute(
                "SELECT count(*) FROM entries WHERE " + filters, values
            ) as cursor:
                total = (await cursor.fetchone())[0]
            if page is not None:
                page = min(page, max(1, (total + page_size - 1) // page_size))
            async with self.db.execute(
                "SELECT count(*), coalesce(sum(json_extract(body,'$.status')!='complete'),0),"
                "coalesce(sum(json_extract(body,'$.status')='complete' AND coalesce(json_extract(body,'$.review_status'),'approved')='approved'),0),"
                "coalesce(sum(json_extract(body,'$.review_status')='pending'),0), coalesce(sum(json_extract(body,'$.review_status')='rejected'),0) FROM entries WHERE lottery_id=?",
                (lottery_id,),
            ) as cursor:
                summary = dict(
                    zip(
                        ("total", "incomplete", "approved", "pending", "rejected"),
                        await cursor.fetchone(),
                    )
                )
            async with self.db.execute(
                "SELECT body FROM entries WHERE "
                + filters
                + " ORDER BY json_extract(body,'$.joined_at'), user_id"
                + (" LIMIT ? OFFSET ?" if page is not None else ""),
                (*values, page_size, (page - 1) * page_size)
                if page is not None
                else values,
            ) as cursor:
                entries = [json.loads(row[0]) for row in await cursor.fetchall()]
            if page is not None:
                for entry in entries:
                    entry["answer_count"] = sum(
                        answer["kind"] != "deleted" for answer in entry["answers"]
                    )
                    entry["marked_count"] = sum(
                        answer.get("correct") is not None for answer in entry["answers"]
                    )
                    entry.pop("answers")
            async with self.db.execute(
                "SELECT id, body, attempts, next_at, delivered_at, error FROM outbox WHERE lottery_id=? ORDER BY rowid DESC LIMIT 200",
                (lottery_id,) if include_deliveries else ("",),
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
        return {
            "entries": entries,
            "deliveries": deliveries,
            "summary": summary,
            "total": total,
            "page": page,
            "page_size": page_size,
        }

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
            item = json.loads(row[0])
            artwork = {
                item.get("cover", ""),
                *(tier.get("image", "") for tier in prize_tiers(item)),
            } - {""}
            async with self.db.execute(
                "SELECT body FROM entries WHERE lottery_id=?", (lottery_id,)
            ) as cursor:
                entries = [json.loads(row[0]) for row in await cursor.fetchall()]
            await self.db.execute("DELETE FROM lotteries WHERE id=?", (lottery_id,))
            async with self.db.execute(
                "SELECT body FROM lotteries UNION ALL SELECT json_extract(body, '$.item') FROM outbox"
            ) as cursor:
                for row in await cursor.fetchall():
                    other = json.loads(row[0])
                    artwork.discard(other.get("cover", ""))
                    artwork.difference_update(
                        tier.get("image", "") for tier in prize_tiers(other)
                    )
            for filename in artwork:
                try:
                    (self.directory / "artwork" / Path(filename).name).unlink(
                        missing_ok=True
                    )
                except OSError:
                    logger.warning("Deleted lottery artwork cleanup deferred.")
        await self.cleanup_artwork(
            {
                image
                for entry in entries
                for answer in entry["answers"]
                for image in answer_images(answer)
            }
        )

    async def cleanup_artwork(self, discarded: set[str] | None = None) -> int:
        """Remove unbound artwork and private attachments after one day.

        Args:
            discarded: Private images just removed from answers, eligible for immediate cleanup.

        Returns:
            Number of stale, unreferenced files removed.
        """
        async with self.lock:
            references = set()
            async with self.db.execute(
                "SELECT body FROM lotteries UNION ALL SELECT json_extract(body, '$.item') FROM outbox"
            ) as cursor:
                for row in await cursor.fetchall():
                    item = json.loads(row[0])
                    references.add(item.get("cover", ""))
                    references.update(
                        tier.get("image", "") for tier in prize_tiers(item)
                    )
            removed = 0
            attachments = set()
            async with self.db.execute("SELECT body FROM entries") as cursor:
                for row in await cursor.fetchall():
                    for answer in json.loads(row[0])["answers"]:
                        attachments.update(answer_images(answer))
            for directory, retained in (
                ("artwork", references),
                ("uploads", attachments),
            ):
                for path in (self.directory / directory).glob("*.jpg"):
                    try:
                        if path.name not in retained and (
                            path.stat().st_mtime < time.time() - 86400
                            or directory == "uploads"
                            and path.name in (discarded or set())
                        ):
                            path.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        logger.warning(
                            "Lottery orphan cleanup failed in %s.", directory
                        )
            return removed
