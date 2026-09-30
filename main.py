"""AstrBot handlers, OneBot routing, authenticated Pages, and scheduled drawing."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
import time
from collections import OrderedDict
from functools import wraps
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp
from aiocqhttp.exceptions import Error as OneBotError
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, file_response, json_response, request
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import (
    get_astrbot_plugin_data_path,
    get_astrbot_temp_path,
)
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError

from .avatars import AvatarCache
from .cards import FONT_PATH, LOGO_PATH, CardSection, render_announcement, render_pages
from .storage import (
    Store,
    answer_images,
    date_text,
    form_progress,
    prize_tiers,
    validate_lottery,
)

PLUGIN_NAME = "astrbot_plugin_catlottery"
TOOL_NAMES = (
    "catlottery_list",
    "catlottery_info",
    "catlottery_join",
    "catlottery_status",
    "catlottery_withdraw",
    "catlottery_fill",
    "catlottery_create",
    "catlottery_manage",
    "catlottery_notifications",
)
HELP = [
    (
        "群聊 · 参加与查看",
        "/抽奖 列表 · 找到想参加的活动\n/抽奖 详情 编号 · 查看奖品与规则\n/抽奖 参与 编号 · 为自己报名\n/抽奖 状态 编号 · 查看状态与估算中奖率\n/抽奖 退出 编号 · 截止前退出报名",
    ),
    (
        "私聊 · 补充报名资料",
        "请先添加我为好友，再私聊回答\n作答模式持续 20 分钟，到期用 /抽奖 继续 恢复\n首条回答后收集 5 秒，可补充文字和多张图片\n等我确认保存并发送下一题后，再回答下一题\n/抽奖 上一题 · 查看并修改上一题答案\n/抽奖 下一题 · 前往已回答题的下一题\n/抽奖 取消回答 · 暂停并保留已保存资料\n/抽奖 继续 · 恢复回答\n/抽奖 待办 · 查看并切换多场待办\n/抽奖 回答 内容 · 可附图片，保留空格与换行",
    ),
    (
        "管理员 · 管理抽奖",
        "/抽奖 创建 · 快速创建无资料活动\n参数：标题 | 奖品 | 人数 | 截止 | 开奖\n多奖项、封面与奖品图片请在管理页设置\n/抽奖 发布 编号 · 向所有配置群发布\n/抽奖 截止 编号 确认 · 停止报名与填写\n/抽奖 开奖 编号 确认 · 抽出全部剩余奖项\n/抽奖 取消 编号 确认 · 取消尚未开奖的活动",
    ),
]
NETWORK_ERRORS = (
    OneBotError,
    asyncio.TimeoutError,
    TimeoutError,
    ConnectionError,
    OSError,
)


def question_sections(
    item: dict, entry: dict, index: int, directory: Path | None = None
) -> list[CardSection]:
    """Compose a private question with only available navigation commands.

    Args:
        item: Activity rules with public question prompts.
        entry: Sender's reservation and saved progress.
        index: Current zero-based question cursor.
        directory: Private uploads directory for the sender's own previous attachment.

    Returns:
        Question, input instructions, navigation, and deadline blocks.
    """
    question = item["questions"][index]
    prompt = question["prompt"]
    if question["options"]:
        prompt += "\n" + "\n".join(
            f"{chr(65 + n)} · {option}" for n, option in enumerate(question["options"])
        )
    instruction = {
        "image": "发送图片，可附带文字；每题最多 9 张，每张不超过 8 MB",
        "mixed": "发送 1–1000 字文字和图片，两者都必填\n每题最多 9 张图片，每张不超过 8 MB",
        "text": "发送 1–1000 字文本，可附带最多 9 张图片，每张不超过 8 MB",
        "quiz": "发送文本答案（1–1000 字），可附带最多 9 张图片",
    }[question["kind"]]
    if question["options"]:
        instruction = "发送选项字母、数字序号或完整选项文本"
    if item.get("require_correct", True) and question["kind"] == "quiz":
        instruction += "\n答题当场核对，答错需重答当前题"
    elif not item.get("require_correct", True):
        instruction += "\n回答后直接进入下一题，提交全部资料后等待管理员审核"
    instruction += "\n直接回复或引用题目回复即可，均提交到当前题\n首条回答后收集 5 秒，期间可补充文字和图片\n结束后确认保存再发下一题，图片分别保留\n保留空格与换行；以斜杠开头的文本用 /抽奖 回答 内容 提交"
    navigation = []
    if index > 0:
        navigation.append("/抽奖 上一题")
    if index < form_progress(entry, len(item["questions"]))[1] and index + 1 < len(
        item["questions"]
    ):
        navigation.append("/抽奖 下一题")
    navigation.append("/抽奖 取消回答")
    if (
        index < len(entry.get("answers", []))
        and entry["answers"][index]["kind"] != "deleted"
    ):
        instruction += "\n这题已有回答，重新发送会替换原答案"
    sections: list[CardSection] = [
        ("当前问题", prompt),
        ("如何回答", instruction),
        ("题目操作", "   ·   ".join(navigation)),
        (
            "资料截止 · 北京时间",
            date_text(item["close_at"]) + "\n未完成或审核未通过的报名不参与开奖",
        ),
    ]
    if entry.get("mode_expires_at"):
        sections.insert(
            2,
            (
                "抽奖作答模式 · 20 分钟",
                f"本轮至 {date_text(entry['mode_expires_at'])}（北京时间）\n到期保留答案并退出，私聊发送 /抽奖 继续 可恢复",
            ),
        )
    previous = (
        index
        if index < len(entry.get("answers", []))
        and entry["answers"][index]["kind"] != "deleted"
        else entry.get("last_answer_index", index - 1)
    )
    if 0 <= previous <= index and previous < len(entry.get("answers", [])):
        answer = entry["answers"][previous]
        if answer.get("kind") != "deleted":
            text = (
                answer.get("text", "")
                if answer["kind"] == "image"
                else answer.get("value", "")
            )
            images = answer_images(answer)
            label = (
                f"本题已保存的回答 · 第 {previous + 1} 题"
                if previous == index
                else f"上次回答 · 第 {previous + 1} 题"
            )
            text += (
                "\n重新发送可替换本题答案"
                if previous == index
                else "\n可用 /抽奖 上一题 返回查看并修改"
            )
            if images and directory is not None:
                for offset, filename in enumerate(images):
                    sections.insert(
                        1 + offset,
                        (
                            label if offset == 0 else f"上次回答 · 图片 {offset + 1}",
                            text if offset == 0 else "",
                            directory / filename,
                        ),
                    )
            else:
                sections.insert(1, (label, text))
    return sections


def prize_sections(
    item: dict, directory: Path, *, results: bool = False
) -> list[CardSection]:
    """Build the same award and image blocks for rules, announcements, and results.

    Args:
        item: Current or legacy lottery snapshot.
        directory: Plugin-owned artwork directory.
        results: Include saved winner assignments and pending award status.

    Returns:
        Public sections without private submissions or quiz references.
    """
    sections = []
    drawn = {record["tier_index"] for record in item.get("tier_draws", [])}
    for index, tier in enumerate(prize_tiers(item)):
        text = f"{tier['prize']}\n名额 {tier['count']} 位"
        if results:
            winners = [
                winner
                for winner in item["winners"]
                if winner.get("tier_index", 0) == index
            ]
            if index in drawn or item["status"] == "drawn":
                text += "\n" + (
                    "\n".join(
                        f"{winner['nickname']} · QQ {winner['user_id']}"
                        for winner in winners
                    )
                    or "暂无符合资格的中奖者"
                )
            else:
                text += "\n尚未开奖，仍按原定时间揭晓"
        section = (tier["name"], text)
        if tier.get("image"):
            section += (directory / tier["image"],)
        sections.append(section)
    return sections


def entry_text(entry: dict | None, question_count: int) -> str:
    """Describe form completion separately from approved lottery eligibility.

    Args:
        entry: The actual sender's enrollment, or None.
        question_count: Total questions in the activity.

    Returns:
        A public status without private answers.
    """
    if entry is None:
        return "尚未参与"
    if entry["status"] != "complete":
        return f"待填写 · 已完成 {form_progress(entry, question_count)[0]}/{question_count} 项"
    return {
        "approved": "参与成功",
        "pending": "资料已提交 · 等待审核",
        "rejected": "审核未通过 · 暂无开奖资格",
    }[entry.get("review_status", "approved")]


def web_boundary(handler):
    """Protect all management endpoints with shared authentication and fault handling.

    Args:
        handler: Plugin-owned management endpoint.

    Returns:
        Handler that never exposes storage errors or caches private submissions.
    """

    @wraps(handler)
    async def wrapped(self, *args, **kwargs):
        if not request.username:
            self.logger.warning("Unauthenticated lottery management request rejected.")
            return error_response("请登录 AstrBot 后打开管理页面。", status_code=403)
        try:
            response = await handler(self, *args, **kwargs)
        except (sqlite3.Error, OSError, RuntimeError, json.JSONDecodeError):
            self.logger.exception(
                "Lottery management endpoint %s failed.", handler.__name__
            )
            response = error_response(
                "服务暂时不可用，请检查 AstrBot 日志后重试；开奖结果不会重新抽取。",
                status_code=503,
            )
        response.headers["Cache-Control"] = "no-store"
        return response

    return wrapped


def tool_boundary(handler):
    """Keep every LLM tool truthful when its transport or persistent storage fails.

    Args:
        handler: Registered tool implementation with its original detailed schema.

    Returns:
        Tool preserving its signature and returning a safe error for infrastructure faults.
    """

    @wraps(handler)
    async def wrapped(self, event, *args, **kwargs):
        try:
            if not (await self.store.settings())["llm_tools_enabled"]:
                self.logger.debug(
                    "Disabled lottery tool %s rejected.", handler.__name__
                )
                await self.card(
                    event,
                    "LLM 工具已关闭",
                    [
                        (
                            "使用提示",
                            "管理员已关闭抽奖工具调用。可以使用 /抽奖 指令操作。",
                        )
                    ],
                )
                return "本插件的 LLM 工具已关闭，本次没有执行任何查询或操作。请使用 /抽奖 指令。"
            return await handler(self, event, *args, **kwargs)
        except NETWORK_ERRORS:
            self.logger.warning("Lottery tool %s could not reach QQ.", handler.__name__)
            return "QQ 连接暂时失败。请查询实际状态后再操作，不要推断操作成功，也不要自动重复创建或开奖。"
        except (sqlite3.Error, RuntimeError, json.JSONDecodeError):
            self.logger.exception("Lottery tool %s storage failure.", handler.__name__)
            await self.error_card(
                event,
                "服务暂时不可用",
                [("操作提示", "请检查 AstrBot 日志并稍后查询报名或抽奖状态。")],
            )
            return "存储暂时不可用，请查询实际状态后再操作。不能推断操作已成功。"

    return wrapped


class CatLottery(Star):
    """Coordinate shared lotteries with actual QQ sender identities."""

    def __init__(self, context: Context):
        super().__init__(context)
        self.store = Store(Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME)
        self.worker: asyncio.Task | None = None
        self.sender: asyncio.Task | None = None
        self.wake = asyncio.Event()
        self.delivery_lock = asyncio.Lock()
        self.tool_switch_lock = asyncio.Lock()
        self.avatars = AvatarCache(self.store.directory / "avatars", self.logger)
        self.image_session: aiohttp.ClientSession | None = None
        self.image_slots = asyncio.Semaphore(4)
        self.private_receipts: OrderedDict[tuple[str, ...], float] = OrderedDict()
        self.private_windows: dict[tuple[str, ...], dict] = {}

    async def initialize(self) -> None:
        """Open storage, register plugin-only APIs, and recover pending scheduled work."""
        if not FONT_PATH.is_file() or not LOGO_PATH.is_file():
            raise RuntimeError(
                "A bundled font or logo is missing; reinstall the plugin."
            )
        await self.store.open()
        try:
            await self.apply_tool_switch()
        except BaseException:
            await self.store.close()
            raise
        for route, handler, methods, description in (
            ("state", self.web_state, ["GET"], "Lottery management state"),
            (
                "platforms",
                self.web_platforms,
                ["GET"],
                "Enabled AioCqhttp platforms and groups",
            ),
            (
                "settings",
                self.web_settings,
                ["POST"],
                "Save lottery operators and LLM tool switch",
            ),
            ("lotteries", self.web_save, ["POST"], "Create or edit lottery rules"),
            (
                "lotteries/<lottery_id>",
                self.web_detail,
                ["GET"],
                "Private forms and delivery records",
            ),
            (
                "lotteries/<lottery_id>/action",
                self.web_action,
                ["POST"],
                "Operate a lottery",
            ),
            (
                "lotteries/<lottery_id>/review",
                self.web_review,
                ["POST"],
                "Review submitted answers and lottery eligibility",
            ),
            (
                "images/<filename>",
                self.web_image,
                ["GET"],
                "Download a private form image",
            ),
            (
                "answer-images",
                self.web_upload_answer_image,
                ["POST"],
                "Upload private answer image",
            ),
            (
                "artwork",
                self.web_upload_artwork,
                ["POST"],
                "Upload an award image or lottery cover",
            ),
            (
                "artwork/<filename>",
                self.web_artwork,
                ["GET"],
                "Preview an award image or lottery cover",
            ),
            (
                "lotteries/<lottery_id>/entries",
                self.web_entries,
                ["GET"],
                "Search paginated enrollment summaries",
            ),
            (
                "lotteries/<lottery_id>/entries/<user_id>",
                self.web_entry,
                ["GET"],
                "Read one participant's private answers",
            ),
            ("avatars/<user_id>", self.web_avatar, ["GET"], "Read a cached QQ avatar"),
            (
                "avatars/clear",
                self.web_clear_avatars,
                ["POST"],
                "Clear the expiring avatar cache",
            ),
        ):
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, methods, description
            )
        self.worker = asyncio.create_task(self.scheduler(), name="catlottery-scheduler")
        self.sender = asyncio.create_task(
            self.delivery_loop(), name="catlottery-delivery"
        )
        self.logger.info("CatLottery initialized; persisted schedules are active.")

    async def apply_tool_switch(self) -> None:
        """Synchronize the persistent switch with this plugin's registered tools.

        Raises:
            RuntimeError: A registered lottery tool could not be found.
        """
        async with self.tool_switch_lock:
            enabled = (await self.store.settings())["llm_tools_enabled"]
            method = "activate_llm_tool" if enabled else "deactivate_llm_tool"
            asynchronous = getattr(self.context, f"{method}_async", None)
            for name in TOOL_NAMES:
                # AstrBot 4.27 exposes synchronous tool control; newer releases add async APIs.
                updated = (
                    await asynchronous(name)
                    if asynchronous is not None
                    else await asyncio.to_thread(getattr(self.context, method), name)
                )
                if not updated:
                    raise RuntimeError(f"Registered lottery tool {name} was not found.")
            self.logger.info(
                "Lottery LLM tools %s.", "enabled" if enabled else "disabled"
            )

    @filter.on_astrbot_loaded()
    async def restore_tool_switch(self) -> None:
        """Reapply tool settings after AstrBot finishes restoring global tool state."""
        await self.apply_tool_switch()

    async def terminate(self) -> None:
        """Stop owned tasks and close SQLite before plugin reload."""
        tasks = [
            self.worker,
            self.sender,
            *(window["task"] for window in self.private_windows.values()),
        ]
        for task in tasks:
            if task:
                task.cancel()
        for task in tasks:
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.worker = self.sender = None
        self.private_receipts.clear()
        self.private_windows.clear()
        await self.avatars.close()
        if self.image_session is not None:
            await self.image_session.close()
            self.image_session = None
        await self.store.close()
        self.context.registered_web_apis[:] = [
            api
            for api in self.context.registered_web_apis
            if not api[0].startswith(f"/{PLUGIN_NAME}/")
        ]
        self.logger.info(
            "CatLottery stopped; schedules and deliveries remain persisted."
        )

    def identity(self, event: AstrMessageEvent) -> dict:
        """Bind identities to real AioCqhttp message events, never LLM arguments.

        Args:
            event: Trusted framework event.

        Returns:
            Actual platform, bot, sender, and group identifiers.

        Raises:
            ValueError: The event or its routing fields are inconsistent.
        """
        if event.get_platform_name() != "aiocqhttp":
            raise ValueError("喵喵抽奖仅支持 AstrBot 的 AioCqhttp 平台。")
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict) or raw.get("post_type") != "message":
            raise ValueError("仅真实 QQ 消息可以操作抽奖。")
        user_id = str(event.get_sender_id())
        bot_id = str(event.get_self_id())
        group_id = str(event.get_group_id() or "")
        if any(not re.fullmatch(r"[1-9][0-9]{4,19}", x) for x in (user_id, bot_id)):
            raise ValueError("无法确认你的 QQ 身份或我的 QQ 账号。")
        if str(raw.get("user_id")) != user_id or str(raw.get("self_id")) != bot_id:
            raise ValueError("QQ 消息身份不一致，已拒绝操作。")
        if group_id:
            if (
                event.get_message_type() != MessageType.GROUP_MESSAGE
                or raw.get("message_type") != "group"
                or str(raw.get("group_id")) != group_id
            ):
                raise ValueError("无法确认群聊来源。")
        elif (
            event.get_message_type() != MessageType.FRIEND_MESSAGE
            or raw.get("message_type") != "private"
        ):
            raise ValueError("请通过群聊或普通私聊发送抽奖指令。")
        return {
            "platform_id": event.get_platform_id(),
            "bot_id": bot_id,
            "user_id": user_id,
            "group_id": group_id,
            "nickname": " ".join((event.get_sender_name() or user_id).split())[:80],
        }

    def check_target(self, item: dict, identity: dict) -> None:
        """Enforce each lottery's exact platform, robot, and group allowlist.

        Args:
            item: Activity rules.
            identity: Trusted message identity.

        Raises:
            ValueError: The current platform or group is not allowed.
        """
        keys = (
            ("platform_id", "bot_id", "group_id")
            if identity["group_id"]
            else ("platform_id", "bot_id")
        )
        if not any(
            all(target[k] == identity[k] for k in keys) for target in item["targets"]
        ):
            raise ValueError("当前连接的平台或群不在这个抽奖的允许列表中。")

    async def check_manager(self, identity: dict) -> None:
        """Enforce operator authority even for an LLM-triggered administrative action.

        Args:
            identity: Actual sender identity.

        Raises:
            ValueError: The sender is not an AstrBot or plugin operator.
        """
        settings = await self.store.settings()
        admins = {str(x) for x in self.context.get_config().get("admins_id", [])}
        if identity["user_id"] not in admins | set(settings["manager_ids"]):
            self.logger.warning(
                "Lottery management permission denied on platform %s.",
                identity["platform_id"],
            )
            raise ValueError("此操作仅限 AstrBot 管理员或管理页中设置的抽奖管理员。")

    async def card(
        self,
        event: AstrMessageEvent,
        title: str,
        sections: list[CardSection],
        subtitle: str = "",
        badge: str = "喵喵抽奖",
        cover: str = "",
        participation_counts: dict | None = None,
    ) -> None:
        """Reply with the same Pillow renderer used by scheduled group notices.

        Args:
            event: Destination event.
            title: Main message.
            sections: Card sections.
            subtitle: Activity context.
            badge: Card category.
            cover: Optional internal filename for a lottery's independent cover.
            participation_counts: Live successful and pending totals for an activity.
        """
        if cover:
            sections = [
                ("活动封面", "", self.store.directory / "artwork" / cover),
                *sections,
            ]
        pngs = await asyncio.to_thread(
            render_pages,
            title,
            subtitle,
            sections,
            badge,
            **(
                {"participation_counts": participation_counts}
                if participation_counts is not None
                else {}
            ),
        )
        await event.send(MessageChain([Image.fromBytes(png) for png in pngs]))

    async def error_card(
        self,
        event: AstrMessageEvent,
        title: str,
        sections: list[CardSection],
        subtitle: str = "",
        badge: str = "抽奖提示",
    ) -> None:
        """Attempt one error reply without allowing a transport failure to escape.

        Args:
            event: Destination event.
            title: User-facing error title.
            sections: Safe error context and next steps.
            subtitle: Optional activity context.
            badge: Card category.
        """
        try:
            await self.card(event, title, sections, subtitle, badge)
        except NETWORK_ERRORS as exc:
            self.logger.warning(
                "Lottery error reply could not be delivered (%s).", type(exc).__name__
            )

    async def platform_inventory(self) -> list[dict]:
        """Discover running adapters and live account/group metadata with bounded waits.

        Returns:
            AioCqhttp platforms, including disconnected instances and QQ identities.
        """
        platforms = [
            p
            for p in self.context.platform_manager.platform_insts
            if p.meta().name == "aiocqhttp" and p.config.get("enable", True)
        ]
        records = []
        for platform in platforms:
            record = {
                "id": platform.meta().id,
                "name": platform.config.get("name") or platform.meta().id,
                "accounts": [],
                "online": False,
                "error": "",
            }
            client = platform.get_client()
            # CQHttp 1.4.x exposes connected X-Self-ID values in this map.
            self_ids = list(client._wsr_api_clients)
            if not self_ids:
                record["error"] = "平台已启用，QQ 协议端尚未连接。"
            for self_id in self_ids:
                try:
                    login = await asyncio.wait_for(
                        client.call_action("get_login_info", self_id=int(self_id)),
                        timeout=8,
                    )
                    groups = await asyncio.wait_for(
                        client.call_action("get_group_list", self_id=int(self_id)),
                        timeout=8,
                    )
                    record["accounts"].append(
                        {
                            "bot_id": str(login["user_id"]),
                            "nickname": str(login.get("nickname", "")),
                            "groups": [
                                {
                                    "group_id": str(g["group_id"]),
                                    "group_name": str(g.get("group_name", "")),
                                }
                                for g in groups
                            ],
                        }
                    )
                    record["online"] = True
                except NETWORK_ERRORS + (KeyError, TypeError, ValueError) as exc:
                    record["error"] = (
                        "暂时无法读取账号或群列表，请检查 QQ 协议端并刷新。"
                    )
                    self.logger.warning(
                        "AioCqhttp discovery failed for %s (%s).",
                        platform.meta().id,
                        type(exc).__name__,
                    )
            records.append(record)
        return records

    async def verify_targets(self, rules: dict) -> None:
        """Ensure newly configured targets belong to live enabled AioCqhttp accounts.

        Args:
            rules: Validated rules from a command or management Page.

        Raises:
            ValueError: A platform, bot, or group cannot be verified.
        """
        for target in rules["targets"]:
            platform = self.context.get_platform_inst(target["platform_id"])
            if (
                platform is None
                or platform.meta().name != "aiocqhttp"
                or not platform.config.get("enable", True)
            ):
                raise ValueError("所选 AioCqhttp 平台未开启，请刷新平台列表。")
            try:
                client = platform.get_client()
                login = await asyncio.wait_for(
                    client.call_action("get_login_info", self_id=int(target["bot_id"])),
                    timeout=8,
                )
                groups = await asyncio.wait_for(
                    client.call_action("get_group_list", self_id=int(target["bot_id"])),
                    timeout=8,
                )
            except NETWORK_ERRORS as exc:
                raise ValueError(
                    "所选 QQ 账号离线，无法验证目标群，请连接协议端后重试。"
                ) from exc
            if (
                not isinstance(login, dict)
                or not isinstance(groups, list)
                or any(not isinstance(group, dict) for group in groups)
            ):
                self.logger.warning(
                    "OneBot returned invalid account or group metadata."
                )
                raise ValueError(
                    "QQ 协议端返回的账号或群信息无效，请刷新平台列表后重试。"
                )
            if str(login.get("user_id")) != target["bot_id"] or target[
                "group_id"
            ] not in {str(g.get("group_id")) for g in groups}:
                raise ValueError("所选 QQ 账号或群列表不匹配，请重新选择。")

    async def show_item(self, event: AstrMessageEvent, item: dict) -> None:
        """Display public rules without exposing private submissions or correct answers.

        Args:
            event: Destination message event.
            item: Activity rules and state.
        """
        state = {
            "open": "报名中" if time.time() < item["close_at"] else "已截止 · 等待开奖",
            "drawn": "已开奖",
            "cancelled": "已取消",
        }[item["status"]]
        sections = prize_sections(
            item,
            self.store.directory / "artwork",
            results=bool(item.get("tier_draws")) or item["status"] == "drawn",
        ) + [
            (
                "抽奖规则",
                f"合计 {item['winner_count']} 个名额，每个 QQ 号最多中奖一次；人数不足时按奖项顺序分配。",
            ),
            (
                "时间 · 北京时间",
                f"报名截止：{date_text(item['close_at'])}\n自动开奖：{date_text(item['draw_at'])}",
            ),
            (
                "参与方式",
                f"群聊发送 /抽奖 参与 {item['id']}\n"
                + (
                    f"报名后需私聊完成 {len(item['questions'])} 项答题或资料。"
                    + (
                        "答题必须当场答对，全部填写完成即报名成功。"
                        if item.get("require_correct", True)
                        else "提交后由管理员审核，全部通过才算成功参与。"
                    )
                    if item["questions"]
                    else "本次无需填写资料，群聊报名即可成功。"
                ),
            ),
        ]
        if item["description"]:
            sections.append(("活动说明", item["description"]))
        await self.card(
            event,
            item["title"],
            sections,
            f"编号 {item['id']}  ·  {state}",
            "抽奖详情",
            cover=item.get("cover", ""),
            participation_counts=await self.store.participation_state(item["id"]),
        )

    async def show_status(self, event: AstrMessageEvent, lottery_id: str) -> dict:
        """Show the actual sender's qualification, result, and conditional live odds.

        Args:
            event: Trusted QQ event; mentions and quoted users are never used.
            lottery_id: Exact activity selected by the sender.

        Returns:
            Sender-only status and aggregate estimates for command and tool callers.

        Raises:
            ValueError: The current platform or group is outside the activity scope.
        """
        identity = self.identity(event)
        live = await self.store.participation_state(lottery_id, identity["user_id"])
        item, entry = live["item"], live["entry"]
        self.check_target(item, identity)
        eligible = bool(
            entry
            and entry["status"] == "complete"
            and entry.get("review_status", "approved") == "approved"
        )
        winner = next(
            (
                value
                for value in item["winners"]
                if value["user_id"] == identity["user_id"]
            ),
            None,
        )
        drawn_tiers = {value["tier_index"] for value in item["tier_draws"]}
        slots = (
            sum(
                tier["count"]
                for index, tier in enumerate(item["prize_tiers"])
                if index not in drawn_tiers
            )
            if item["status"] == "open"
            else 0
        )
        candidates = max(
            0, live["approved"] - len({value["user_id"] for value in item["winners"]})
        )
        probability = min(1.0, slots / candidates) if slots and candidates else None
        if winner:
            tier = item["prize_tiers"][winner.get("tier_index", 0)]
            outcome = f"你已获得 {tier['name']}\n{tier['prize']}\n本场最多中奖一次，已不再参与剩余奖项"
        elif item["status"] == "drawn":
            outcome = "本场已开奖，你未中奖；最终名单已保存"
        elif item["status"] == "cancelled":
            outcome = "活动已取消，不再开奖"
        else:
            outcome = f"剩余 {slots} 个名额 · 当前 {candidates} 位成功参与者尚未中奖\n"
            outcome += (
                f"{'你当前' if eligible else '当前候选池'}估算中奖率 {probability:.2%}"
                if probability is not None
                else "当前暂无可计算的中奖率"
            )
            outcome += "\n" + (
                "你已具备开奖资格"
                if eligible
                else "你尚未获得开奖资格，成功参与后才进入候选名单"
            )
            outcome += "\n估算针对剩余奖项至少中奖一次，随报名、审核和提前开奖变化，最终以实际开奖名单为准"
        status = {
            "status": entry["status"] if entry else "not_joined",
            "review_status": entry.get(
                "review_status",
                "approved" if entry["status"] == "complete" else "incomplete",
            )
            if entry
            else None,
            "eligible": eligible,
            "answered": form_progress(entry, len(item["questions"]))[0] if entry else 0,
            "questions": len(item["questions"]),
            "user_id": identity["user_id"],
            "approved_count": live["approved"],
            "pending_review_count": live["pending"],
            "remaining_slots": slots,
            "remaining_candidates": candidates,
            "pool_win_probability": probability,
            "user_win_probability": probability if eligible and not winner else None,
            "has_won": winner is not None,
            "lottery_status": item["status"],
        }
        await self.card(
            event,
            entry_text(entry, len(item["questions"])),
            [
                (
                    "身份与报名",
                    f"QQ {identity['user_id']} · {identity['nickname']}\n"
                    + (
                        f"已填写 {status['answered']}/{status['questions']} 题"
                        if item["questions"]
                        else "本场无需填写资料"
                    ),
                ),
                ("中奖状态与估算", outcome),
                (
                    "活动时间 · 北京时间",
                    f"报名截止 {date_text(item['close_at'])}\n自动开奖 {date_text(item['draw_at'])}",
                ),
                (
                    "自己查询",
                    f"/抽奖 状态 {lottery_id}"
                    + (
                        "\n私聊恢复回答：/抽奖 继续"
                        if entry
                        and entry["status"] == "pending"
                        and item["status"] == "open"
                        and time.time() < item["close_at"]
                        else ""
                    ),
                ),
            ],
            f"{item['title']} · {lottery_id}",
            "参与状态",
            participation_counts=live,
        )
        return status

    async def show_question(
        self, event: AstrMessageEvent, item: dict, entry: dict
    ) -> None:
        """Show the next private question with unambiguous input instructions.

        Args:
            event: Private destination event.
            item: Activity rules.
            entry: Sender's pending entry.
        """
        index = await self.store.form_position(entry["bot_id"], entry["user_id"])
        entry = await self.store.entry(item["id"], entry["user_id"]) or entry
        await self.card(
            event,
            f"第 {index + 1} / {len(item['questions'])} 题",
            question_sections(item, entry, index, self.store.directory / "uploads"),
            f"{item['title']}  ·  {item['id']}  ·  QQ {entry['user_id']}",
            "私聊填写",
            participation_counts=await self.store.participation_state(item["id"]),
        )

    async def join(self, event: AstrMessageEvent, lottery_id: str) -> str:
        """Enroll the actual group sender and guide friendship/private forms if needed.

        Args:
            event: Actual group message event.
            lottery_id: User-selected activity identifier.

        Returns:
            Minimal outcome for tool callers; all user notices are images.

        Raises:
            ValueError: Private participation or an unauthorized target is requested.
        """
        identity = self.identity(event)
        if not identity["group_id"]:
            raise ValueError(
                "私聊不能参与抽奖。请回到该抽奖允许的群发送 /抽奖 参与 编号。"
            )
        item = await self.store.get(lottery_id)
        self.check_target(item, identity)
        entry, created = await self.store.enroll(
            lottery_id, identity, restart_existing=True
        )
        if created:
            self.logger.info(
                "Lottery %s enrollment reserved; status=%s.",
                lottery_id,
                entry["status"],
            )
        else:
            self.logger.debug(
                "Lottery %s existing enrollment returned or restarted.", lottery_id
            )
        self.wake.set()
        if entry["status"] == "complete":
            if not created:
                await self.card(
                    event,
                    entry_text(entry, len(item["questions"])),
                    [
                        (
                            "报名记录",
                            f"QQ {identity['user_id']} 已提交本次资料。{entry_text(entry, len(item['questions']))}，无需重复报名。",
                        ),
                        ("自动开奖", date_text(item["draw_at"]) + "（北京时间）"),
                    ],
                    f"{item['title']} · {lottery_id}",
                    participation_counts=await self.store.participation_state(
                        lottery_id
                    ),
                )
            return (
                (
                    "报名成功；群聊与私聊图片通知已加入发送队列。"
                    if item.get("group_success_notify", True)
                    else "报名成功；私聊图片通知已加入发送队列，本场已关闭群聊成功通知。"
                )
                if created
                else f"{entry_text(entry, len(item['questions']))}，未重复计数；只有参与成功者进入开奖名单。"
            )
        if not created:
            await self.store.private_session(
                entry["bot_id"], entry["user_id"], lottery_id
            )
        try:
            original_platform = self.context.get_platform_inst(entry["platform_id"])
            if original_platform is None:
                raise ConnectionError("Original platform offline")
            friends = await asyncio.wait_for(
                original_platform.get_client().call_action(
                    "get_friend_list", self_id=int(entry["bot_id"])
                ),
                timeout=8,
            )
            is_friend = identity["user_id"] in {str(f["user_id"]) for f in friends}
            guidance = (
                f"我会通过 QQ {entry['bot_id']} 私聊发送题目，直接回答即可\n没有收到题目时，私聊发送 /抽奖 继续"
                if is_friend
                else f"请先添加我（QQ {entry['bot_id']}）为好友，再私聊发送 /抽奖 继续"
            )
        except NETWORK_ERRORS + (TypeError, KeyError) as exc:
            guidance = f"暂时无法确认好友关系，请私聊我（QQ {entry['bot_id']}）发送 /抽奖 继续\n如果私聊无法发送，请先添加我为好友"
            self.logger.warning("Friend lookup failed (%s).", type(exc).__name__)
        await self.store.queue_question(lottery_id, entry["bot_id"], entry["user_id"])
        self.wake.set()
        await self.card(
            event,
            "已预留报名 · 还差私聊资料",
            [
                ("下一步", guidance),
                (
                    "你的报名",
                    f"QQ {identity['user_id']}\n截止前完成 {len(item['questions'])} 个问题。"
                    + (
                        "答题当场核对。"
                        if item.get("require_correct", True)
                        else "提交后等待管理员审核资格。"
                    ),
                ),
                ("资料截止", date_text(item["close_at"]) + "（北京时间）"),
            ],
            f"{item['title']} · {lottery_id}",
            participation_counts=await self.store.participation_state(lottery_id),
        )
        return "已预留报名，用户需按图片引导私聊填写；尚未完成报名。"

    # AstrBot takes registration priority from the innermost decorator.
    @filter.command("抽奖")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=90)
    async def lottery_command(self, event: AstrMessageEvent):
        """Dispatch /抽奖 subcommands and render every outcome, including syntax errors.

        Args:
            event: Actual QQ command message event.
        """
        event.stop_event()
        event.should_call_llm(True)
        try:
            identity = self.identity(event)
            # A single root handler avoids framework-generated text errors for
            # missing arguments and unknown command-group subcommands.
            text = "".join(
                part.text for part in event.get_messages() if isinstance(part, Plain)
            )
            raw_segments = event.message_obj.raw_message.get("message")
            if isinstance(raw_segments, list):
                # The adapter trims Plain components; raw segments preserve submitted whitespace.
                text = "".join(
                    segment["data"]["text"]
                    for segment in raw_segments
                    if isinstance(segment, dict)
                    and segment.get("type") == "text"
                    and isinstance(segment.get("data"), dict)
                    and isinstance(segment["data"].get("text"), str)
                )
            text = text.lstrip()
            for prefix in self.context.get_config(event.unified_msg_origin).get(
                "wake_prefix", ["/", "／"]
            ):
                if prefix and text.startswith(prefix):
                    text = text[len(prefix) :].lstrip()
                    break
            # The framework already matched the root command, including configured renames.
            text = re.sub(r"^\S+(?:\s|$)", "", text, count=1)
            parts = re.match(r"(\S+)(?:\s([\s\S]*))?", text.lstrip())
            command = parts.group(1) if parts else "帮助"
            argument = (parts.group(2) or "") if parts else ""
            if command != "回答":
                argument = argument.strip()
            if command in {"帮助", "help"}:
                await self.card(
                    event,
                    "喵喵抽奖 · 使用帮助",
                    HELP,
                    "群聊报名 · 私聊填写 · 定时开奖",
                    "使用帮助",
                )
            elif command == "列表":
                items = await self.store.snapshot()
                items = [
                    x
                    for x in items
                    if any(
                        t["platform_id"] == identity["platform_id"]
                        and t["bot_id"] == identity["bot_id"]
                        and (
                            not identity["group_id"]
                            or t["group_id"] == identity["group_id"]
                        )
                        for t in x["targets"]
                    )
                ]
                sections = []
                for item in items[:20]:
                    label = {
                        "open": "报名中",
                        "closed": "已截止",
                        "drawn": "已开奖",
                        "cancelled": "已取消",
                    }[item["phase"]]
                    sections.append(
                        (
                            f"{item['id']} · {label}",
                            f"{item['title']}\n{item['prize']} · {item['complete_count']} 人报名成功\n开奖 {date_text(item['draw_at'])}",
                        )
                    )
                await self.card(
                    event,
                    "当前抽奖活动",
                    sections
                    or [("暂时没有抽奖", "当前平台和群还没有允许参与的抽奖。")],
                    "显示最近 20 个抽奖 · /抽奖 详情 编号",
                    "抽奖列表",
                )
            elif command == "待办":
                if identity["group_id"]:
                    raise ValueError("请私聊我发送 /抽奖 待办。")
                pending = []
                for item in await self.store.snapshot():
                    entry = await self.store.entry(item["id"], identity["user_id"])
                    if (
                        entry
                        and entry["bot_id"] == identity["bot_id"]
                        and entry["platform_id"] == identity["platform_id"]
                        and entry["status"] == "pending"
                        and item["phase"] == "open"
                    ):
                        pending.append(
                            (
                                item["title"],
                                f"/抽奖 切换 {item['id']}\n已完成 {form_progress(entry, len(item['questions']))[0]}/{len(item['questions'])} 项",
                            )
                        )
                await self.card(
                    event,
                    "把报名资料补齐吧",
                    pending
                    or [
                        (
                            "没有待办",
                            "你没有尚未完成的群报名，请先在允许的群内参与抽奖。",
                        )
                    ],
                    f"QQ {identity['user_id']}",
                    "私聊待办",
                )
            elif command in {"继续", "上一题", "下一题"}:
                if identity["group_id"]:
                    raise ValueError("此指令请在报名时与我的私聊中使用")
                lottery_id = await self.store.private_session(
                    identity["bot_id"], identity["user_id"]
                )
                if not lottery_id:
                    if command != "继续":
                        raise ValueError("没有正在回答的题目，请私聊发送 /抽奖 继续")
                    pending = []
                    for candidate in await self.store.snapshot():
                        entry = await self.store.entry(
                            candidate["id"], identity["user_id"]
                        )
                        if (
                            entry
                            and entry["bot_id"] == identity["bot_id"]
                            and entry["platform_id"] == identity["platform_id"]
                            and entry["status"] == "pending"
                            and candidate["phase"] == "open"
                        ):
                            pending.append(candidate["id"])
                    if len(pending) != 1:
                        raise ValueError(
                            "有多场待办，请发送 /抽奖 待办 选择活动"
                            if pending
                            else "没有待回答的报名，请先在活动允许的群内参与"
                        )
                    lottery_id = pending[0]
                    await self.store.private_session(
                        identity["bot_id"], identity["user_id"], lottery_id
                    )
                elif command == "继续":
                    await self.store.private_session(
                        identity["bot_id"], identity["user_id"], lottery_id
                    )
                item = await self.store.get(lottery_id)
                self.check_target(item, identity)
                entry = await self.store.entry(lottery_id, identity["user_id"])
                if not entry or entry["platform_id"] != identity["platform_id"]:
                    raise ValueError("请通过报名时使用的平台私聊我")
                await self.store.form_position(
                    identity["bot_id"],
                    identity["user_id"],
                    -1 if command == "上一题" else 1 if command == "下一题" else 0,
                )
                await self.show_question(event, item, entry)
            elif command in {"取消填写", "取消回答"}:
                if identity["group_id"]:
                    raise ValueError("此指令请在私聊中使用。")
                await self.store.private_session(
                    identity["bot_id"], identity["user_id"], ""
                )
                await self.card(
                    event,
                    "已暂停填写",
                    [
                        (
                            "随时继续",
                            "已回答内容保留，私聊发送 /抽奖 继续 恢复回答\n请在报名截止前完成全部题目",
                        )
                    ],
                )
            elif command == "回答":
                if identity["group_id"]:
                    raise ValueError("请在私聊中回答，避免把报名资料发到群里。")
                if (
                    await self.store.private_session(
                        identity["bot_id"], identity["user_id"]
                    )
                    is None
                ):
                    raise ValueError("请私聊发送 /抽奖 继续 恢复已有的群报名资料")
                await self.collect_private(event, content=argument)
            elif command == "创建":
                await self.check_manager(identity)
                if not identity["group_id"]:
                    raise ValueError(
                        "指令创建请在目标群使用，跨平台多群抽奖请在管理页面创建。"
                    )
                fields = [x.strip() for x in argument.split("|")]
                if len(fields) != 5:
                    raise ValueError(
                        "用法：/抽奖 创建 标题 | 奖品 | 中奖人数 | 截止时间 | 开奖时间\n示例：/抽奖 创建 午后好运 | 猫猫贴纸 | 2 | 2026-10-01 20:00 | 2026-10-01 20:10"
                    )
                try:
                    count = int(fields[2])
                except ValueError as exc:
                    raise ValueError("中奖人数必须是整数。") from exc
                rules = validate_lottery(
                    {
                        "title": fields[0],
                        "prize": fields[1],
                        "winner_count": count,
                        "close_at": fields[3],
                        "draw_at": fields[4],
                        "targets": [
                            {
                                k: identity[k]
                                for k in ("platform_id", "bot_id", "group_id")
                            }
                        ],
                        "questions": [],
                    }
                )
                await self.verify_targets(rules)
                item = await self.store.save(rules, identity["user_id"])
                await self.store.action(item["id"], "publish")
                self.wake.set()
                await self.card(
                    event,
                    "抽奖已创建",
                    [
                        ("抽奖编号", item["id"]),
                        (
                            "下一步",
                            "群公告已加入发送队列。你可以在管理页添加私聊问题和其他平台、群；已有报名后，报名规则将锁定。",
                        ),
                    ],
                    item["title"],
                )
            elif command in {
                "详情",
                "参与",
                "状态",
                "退出",
                "填写",
                "切换",
                "发布",
                "截止",
                "开奖",
                "取消",
            }:
                arguments = argument.split()
                if not arguments:
                    raise ValueError(
                        f"用法：/抽奖 {command} 编号"
                        + (" 确认" if command in {"开奖", "取消", "截止"} else "")
                    )
                lottery_id = arguments[0].lower()
                item = await self.store.get(lottery_id)
                self.check_target(item, identity)
                if command == "详情":
                    await self.show_item(event, item)
                elif command == "参与":
                    await self.join(event, lottery_id)
                elif command == "状态":
                    await self.show_status(event, lottery_id)
                elif command == "退出":
                    await self.store.withdraw(lottery_id, identity)
                    await self.card(
                        event,
                        "已退出本次抽奖",
                        [("报名记录已移除", "截止前可以重新在允许的群内报名。")],
                        f"{item['title']} · {lottery_id}",
                    )
                elif command in {"填写", "切换"}:
                    if identity["group_id"]:
                        raise ValueError("请私聊我回答题目，群聊不接收报名资料")
                    entry = await self.store.entry(lottery_id, identity["user_id"])
                    if entry and entry["platform_id"] != identity["platform_id"]:
                        raise ValueError("请通过报名时使用的平台私聊我。")
                    await self.store.private_session(
                        identity["bot_id"], identity["user_id"], lottery_id
                    )
                    await self.show_question(event, item, entry)
                else:
                    await self.check_manager(identity)
                    if command in {"截止", "开奖", "取消"} and arguments[1:] != [
                        "确认"
                    ]:
                        raise ValueError(
                            f"此操作会{'立即开奖并结束报名' if command == '开奖' else '取消抽奖' if command == '取消' else '立即截止报名'}。执行请发送 /抽奖 {command} {lottery_id} 确认。"
                        )
                    action = {
                        "发布": "publish",
                        "截止": "close",
                        "开奖": "draw",
                        "取消": "cancel",
                    }[command]
                    await self.store.action(lottery_id, action)
                    self.logger.info(
                        "Lottery %s action=%s via QQ command.", lottery_id, action
                    )
                    self.wake.set()
                    await self.card(
                        event,
                        f"已{command}",
                        [
                            (
                                "群通知",
                                "通知已排队，将发送到本场活动设置的全部群。",
                            )
                        ],
                        f"{item['title']} · {lottery_id}",
                    )
            elif not identity["group_id"] and await self.store.private_session(
                identity["bot_id"], identity["user_id"]
            ):
                # Only an existing active private form can accept the shorthand answer.
                await self.collect_private(event, content=text)
            else:
                raise ValueError("未知子命令。发送 /抽奖 查看图片帮助。")
        except ValueError as exc:
            self.logger.debug(
                "Lottery command validation rejected (%s).", type(exc).__name__
            )
            await self.error_card(
                event, "暂时无法操作", [("操作提示", str(exc))], badge="抽奖提示"
            )
        except (sqlite3.Error, RuntimeError, json.JSONDecodeError):
            self.logger.exception("Lottery command storage failure.")
            await self.error_card(
                event,
                "服务暂时不可用",
                [
                    (
                        "请稍后查询状态",
                        "请检查 AstrBot 日志。已有报名和开奖结果以保存的记录为准。",
                    )
                ],
            )
        except NETWORK_ERRORS as exc:
            self.logger.warning(
                "Lottery command network failure (%s).", type(exc).__name__
            )
            # A reply failure must not trigger another send through the same broken transport.

    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=81)
    async def restore_friend_forms(self, event: AstrMessageEvent):
        """Restore existing group reservations after a verified OneBot friend-add notice.

        Args:
            event: Adapter event whose raw notice identifies the actual friend and bot.
        """
        raw = event.message_obj.raw_message
        if (
            not isinstance(raw, dict)
            or raw.get("post_type") != "notice"
            or raw.get("notice_type") != "friend_add"
        ):
            return
        bot_id, user_id = str(event.get_self_id()), str(event.get_sender_id())
        if (
            event.get_platform_name() != "aiocqhttp"
            or event.get_group_id()
            or str(raw.get("self_id")) != bot_id
            or str(raw.get("user_id")) != user_id
            or not all(
                re.fullmatch(r"[1-9][0-9]{4,19}", value) for value in (bot_id, user_id)
            )
        ):
            self.logger.warning(
                "Untrusted friend-add notice rejected for lottery restoration."
            )
            return
        try:
            result = await self.store.restart_forms(
                bot_id=bot_id, user_id=user_id, platform_id=event.get_platform_id()
            )
            if result["restarted_users"]:
                self.wake.set()
                self.logger.info(
                    "Friend-add notice restored a pending lottery form for QQ %s.",
                    user_id,
                )
        except (sqlite3.Error, RuntimeError, json.JSONDecodeError):
            self.logger.exception(
                "Friend-add lottery restoration failed; manual continuation remains available."
            )

    async def finish_private_window(
        self, scope: tuple[str, ...], window: dict, *, wait: bool = True
    ) -> None:
        """Commit a fixed collection window and retain ownership through the next reply.

        Args:
            scope: Trusted platform, bot, and sender identifiers.
            window: Captured question, mode generation, and ordered message fragments.
            wait: Wait for the collection deadline before saving.
        """
        try:
            if wait:
                await asyncio.sleep(max(0, window["deadline"] - time.monotonic()))
            if self.private_windows.get(scope) is not window:
                return
            window["closing"] = True
            await self.collect_private(
                window["event"], content="\n".join(window["texts"]), submission=window
            )
        except NETWORK_ERRORS as exc:
            self.logger.warning(
                "Private answer window reply failed (%s).", type(exc).__name__
            )
        except Exception:
            # A detached task must report unexpected failures without leaving ownership stuck.
            self.logger.exception("Private answer collection window failed.")
        finally:
            if self.private_windows.get(scope) is window:
                del self.private_windows[scope]

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP, priority=80)
    async def collect_private(
        self,
        event: AstrMessageEvent,
        content: str | None = None,
        *,
        submission: dict | None = None,
    ):
        """Consume only actual private messages for an explicitly selected pending form.

        Args:
            event: Real private sender event with trusted message components.
            content: Explicit answer command content, including text starting with a slash.
            submission: Captured collection window, supplied only by the owned timer.
        """
        text = (
            content
            if content is not None
            else "".join(
                part.text for part in event.get_messages() if isinstance(part, Plain)
            )
        )
        if content is None:
            prefixes = self.context.get_config(event.unified_msg_origin).get(
                "wake_prefix", ["/", "／"]
            )
            if text.lstrip().startswith(
                ("/", "／", *(prefix for prefix in prefixes if prefix))
            ) or re.match(r"^抽奖(?:\s|$)", text.lstrip()):
                return
        raw = event.message_obj.raw_message
        if not isinstance(raw, dict) or raw.get("post_type") != "message":
            return
        if content is None and isinstance(raw.get("message"), list):
            text = "".join(
                segment["data"]["text"]
                for segment in raw["message"]
                if isinstance(segment, dict)
                and segment.get("type") == "text"
                and isinstance(segment.get("data"), dict)
                and isinstance(segment["data"].get("text"), str)
            )
        lottery_id = ""
        stored_images: list[Path] = []
        try:
            identity = self.identity(event)
            if identity["group_id"]:
                return
            message_id = str(
                raw.get("message_id", getattr(event.message_obj, "message_id", ""))
                or ""
            )
            scope = (identity["platform_id"], identity["bot_id"], identity["user_id"])
            receipt_key = (*scope, message_id)
            now = time.monotonic()
            while self.private_receipts:
                first = next(iter(self.private_receipts))
                if (
                    len(self.private_receipts) <= 10000
                    and now - self.private_receipts[first] < 1200
                ):
                    break
                self.private_receipts.popitem(last=False)
            if (
                submission is None
                and message_id
                and receipt_key in self.private_receipts
            ):
                event.stop_event()
                event.should_call_llm(True)
                self.logger.debug("Duplicate private lottery message suppressed.")
                return
            lottery_id = await self.store.private_session(
                identity["bot_id"], identity["user_id"]
            )
            if lottery_id is None:
                if submission is not None:
                    self.wake.set()
                return
            entry = await self.store.entry(lottery_id, identity["user_id"])
            if entry and entry["platform_id"] != identity["platform_id"]:
                if content is None:
                    return
                raise ValueError("请通过报名时使用的平台私聊我。")
            event.stop_event()
            event.should_call_llm(True)
            images = (
                submission["images"]
                if submission is not None
                else [part for part in event.get_messages() if isinstance(part, Image)]
            )
            item = await self.store.get(lottery_id)
            self.check_target(item, identity)
            entry = await self.store.entry(lottery_id, identity["user_id"])
            if not entry or entry["status"] != "pending":
                raise ValueError("这份报名已完成或移除，请查看 /抽奖 状态 编号。")
            index = await self.store.form_position(
                identity["bot_id"], identity["user_id"]
            )
            if item["status"] != "open" or time.time() >= item["close_at"]:
                await self.store.private_session(
                    identity["bot_id"], identity["user_id"], ""
                )
                raise ValueError("报名已截止，未完成的资料不会进入开奖名单。")
            if submission is not None and (
                submission["lottery_id"] != lottery_id
                or submission["index"] != index
                or submission["mode_expires_at"] != entry.get("mode_expires_at")
            ):
                self.logger.debug(
                    "Stale private answer collection discarded after mode or question change."
                )
                return
            if submission is None:
                if message_id:
                    self.private_receipts[receipt_key] = now
                window = self.private_windows.get(scope)
                if window and (
                    window["lottery_id"] != lottery_id
                    or window["mode_expires_at"] != entry.get("mode_expires_at")
                ):
                    window["task"].cancel()
                    del self.private_windows[scope]
                    window = None
                if window and (window["closing"] or now >= window["deadline"]):
                    if not window.get("saving_notice"):
                        window["saving_notice"] = True
                        await event.send(
                            MessageChain(
                                [Plain("本题正在保存，请等我发送下一题后再回答")]
                            )
                        )
                    return
                if window and window["index"] != index:
                    window["task"].cancel()
                    del self.private_windows[scope]
                    window = None
                if window is None:
                    if len(self.private_windows) >= 1000:
                        raise ValueError("当前作答人数较多，请稍后重新回答本题")
                    window = {
                        "event": event,
                        "lottery_id": lottery_id,
                        "index": index,
                        "mode_expires_at": entry.get("mode_expires_at"),
                        "deadline": now + 5,
                        "texts": [],
                        "images": [],
                        "fingerprints": set(),
                        "image_sources": set(),
                        "closing": False,
                    }
                    self.private_windows[scope] = window
                    window["task"] = asyncio.create_task(
                        self.finish_private_window(scope, window)
                    )
                fingerprint = hashlib.sha256(
                    json.dumps(
                        [
                            text,
                            [str(image.file or image.url or "") for image in images],
                        ],
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
                first = not window["fingerprints"]
                if fingerprint in window["fingerprints"]:
                    return
                sources = [str(image.file or image.url or "") for image in images]
                texts = window["texts"] + (
                    [text] if text and text not in window["texts"] else []
                )
                if (
                    len("\n".join(texts)) > 1000
                    or len(window["image_sources"] | set(sources)) > 9
                ):
                    if first:
                        window["task"].cancel()
                        del self.private_windows[scope]
                    raise ValueError(
                        "本条补充未加入收集窗口：每题文字最多 1000 字，图片最多 9 张"
                    )
                window["fingerprints"].add(fingerprint)
                window["texts"] = texts
                for image, source in zip(images, sources):
                    if source not in window["image_sources"]:
                        window["images"].append(image)
                        window["image_sources"].add(source)
                if first:
                    try:
                        await event.send(
                            MessageChain(
                                [
                                    Plain(
                                        "已进入 5 秒答题收集窗口，可继续补充文字和图片；结束后我会确认保存，再发送下一题"
                                    )
                                ]
                            )
                        )
                    except NETWORK_ERRORS as exc:
                        self.logger.warning(
                            "Private collection reminder failed (%s); collection continues.",
                            type(exc).__name__,
                        )
                return
            question = item["questions"][index]
            if len(images) > 9:
                raise ValueError("每题最多保留 9 张图片")
            if question["kind"] in {"image", "mixed"} and not images:
                raise ValueError("本题需要一张图片，请按题目要求重新发送。")
            if len(text) > 1000 or (question["kind"] != "image" and not text.strip()):
                raise ValueError(
                    "本题需要 1–1000 字非空文本，可附带图片；空格与换行也计入长度。"
                )
            image_hashes = set()
            for image in images:
                data = await self.read_submission_image(image)
                digest = hashlib.sha256(data).digest()
                if digest in image_hashes:
                    continue
                image_hashes.add(digest)
                async with self.image_slots:
                    with await asyncio.to_thread(
                        PillowImage.open, BytesIO(data)
                    ) as original:
                        if original.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                            raise ValueError("请发送 JPG、PNG、WebP 或 GIF 图片。")
                        if original.width * original.height > 24_000_000:
                            raise ValueError("图片分辨率过大，请缩小后发送。")
                        await asyncio.to_thread(original.load)
                        with await asyncio.to_thread(
                            ImageOps.exif_transpose, original
                        ) as oriented:
                            with await asyncio.to_thread(
                                oriented.convert, "RGBA"
                            ) as rgba:
                                with PillowImage.new(
                                    "RGB", rgba.size, "white"
                                ) as picture:
                                    await asyncio.to_thread(
                                        picture.paste, rgba, (0, 0), rgba
                                    )
                                    await asyncio.to_thread(
                                        picture.thumbnail, (2000, 2000)
                                    )
                                    stored_image = (
                                        self.store.directory
                                        / "uploads"
                                        / f"{secrets.token_hex(16)}.jpg"
                                    )
                                    stored_images.append(stored_image)
                                    await asyncio.to_thread(
                                        picture.save, stored_image, "JPEG", quality=88
                                    )
            if question["kind"] == "image":
                answer = {"kind": "image", "value": stored_images[0].name, "text": text}
            else:
                answer = {
                    "kind": "mixed" if question["kind"] == "mixed" else "text",
                    "value": text,
                }
                if stored_images:
                    answer["image"] = stored_images[0].name
            if len(stored_images) > 1:
                answer["images"] = [path.name for path in stored_images]
            item, entry = await self.store.answer(
                lottery_id,
                identity,
                answer,
                index,
                mode_expires_at=entry.get("mode_expires_at"),
            )
            stored_images.clear()
            if submission is not None:
                try:
                    await event.send(
                        MessageChain(
                            [
                                Plain(
                                    f"第 {index + 1} 题作答结束，答案已保存"
                                    + (
                                        "，全部资料已提交"
                                        if entry["status"] == "complete"
                                        else "，接下来是下一题"
                                    )
                                )
                            ]
                        )
                    )
                except NETWORK_ERRORS as exc:
                    self.logger.warning(
                        "Private answer confirmation failed (%s); saved answer retained.",
                        type(exc).__name__,
                    )
            if entry["status"] == "complete":
                self.logger.info(
                    "Lottery %s private form completed; eligibility=%s; notices queued.",
                    lottery_id,
                    entry.get("review_status", "approved"),
                )
                self.wake.set()
            else:
                self.logger.debug(
                    "Lottery %s form advanced to question %d.",
                    lottery_id,
                    form_progress(entry, len(item["questions"]))[1] + 1,
                )
                try:
                    await self.show_question(event, item, entry)
                except NETWORK_ERRORS as exc:
                    await self.store.queue_question(
                        lottery_id, identity["bot_id"], identity["user_id"]
                    )
                    self.wake.set()
                    self.logger.warning(
                        "Private question reply failed (%s); retry queued.",
                        type(exc).__name__,
                    )
        except (
            ValueError,
            OSError,
            UnidentifiedImageError,
            TimeoutError,
            asyncio.TimeoutError,
            PillowImage.DecompressionBombError,
        ) as exc:
            self.logger.debug(
                "Lottery %s private submission rejected (%s).",
                lottery_id,
                type(exc).__name__,
            )
            message = (
                str(exc)
                if isinstance(exc, ValueError)
                else "图片读取失败，请重新发送有效的 JPG、PNG、WebP 或 GIF 图片。"
            )
            await self.error_card(
                event, "这一步还没有完成", [("填写提示", message)], f"抽奖 {lottery_id}"
            )
        except (sqlite3.Error, RuntimeError, json.JSONDecodeError):
            self.logger.exception(
                "Lottery %s private form storage failure.", lottery_id
            )
            await self.error_card(
                event,
                "资料暂时未能确认",
                [("请查询状态", "请稍后发送 /抽奖 状态 编号，检查这一步是否已保存。")],
            )
        except OneBotError as exc:
            self.logger.warning(
                "Private form reply failed (%s); saved progress is retained.",
                type(exc).__name__,
            )
        finally:
            if stored_images:
                try:
                    await self.store.cleanup_artwork(
                        {path.name for path in stored_images}
                    )
                except (sqlite3.Error, RuntimeError):
                    self.logger.warning("Uncommitted private image cleanup deferred.")

    async def read_submission_image(self, image: Image) -> bytes:
        """Read a bounded QQ image without arbitrary URL fetching or local file access.

        Args:
            image: Image component from the authenticated OneBot message.

        Returns:
            At most 8 MB of image data, ready for format and pixel validation.

        Raises:
            ValueError: The source is unsupported, inaccessible, or exceeds the byte limit.
        """
        limit = 8 * 1024 * 1024
        source = image.url or image.file or ""
        try:
            async with self.image_slots:
                if source.startswith(("http://", "https://")):
                    url = urlsplit(source)
                    host = (url.hostname or "").lower()
                    if (
                        url.username
                        or url.password
                        or url.port not in {None, 80, 443}
                        or not (
                            host.endswith(".qpic.cn") or host == "multimedia.nt.qq.com"
                        )
                    ):
                        raise ValueError(
                            "图片来源不受支持，请直接发送 QQ 图片，不要提交外部链接。"
                        )
                    if self.image_session is None or self.image_session.closed:
                        self.image_session = aiohttp.ClientSession(
                            timeout=aiohttp.ClientTimeout(total=25, connect=8),
                            connector=aiohttp.TCPConnector(limit=4),
                        )
                    async with self.image_session.get(
                        source, allow_redirects=False
                    ) as response:
                        if response.status != 200:
                            raise ValueError("图片已失效或暂时无法下载，请重新发送。")
                        if (
                            response.content_length is not None
                            and response.content_length > limit
                        ):
                            raise ValueError("图片不能超过 8 MB，请压缩后重新发送。")
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            data.extend(chunk)
                            if len(data) > limit:
                                raise ValueError(
                                    "图片不能超过 8 MB，请压缩后重新发送。"
                                )
                        return bytes(data)
                if source.startswith("base64://"):
                    encoded = source.removeprefix("base64://")
                    if len(encoded) > ((limit + 2) // 3) * 4:
                        raise ValueError("图片不能超过 8 MB，请压缩后重新发送。")
                    data = base64.b64decode(encoded, validate=True)
                else:
                    if source.startswith(("\\\\", "//")):
                        raise ValueError("不支持网络共享图片，请重新发送 QQ 图片。")
                    if source.startswith("file://"):
                        url = urlsplit(source)
                        if url.netloc:
                            raise ValueError("不支持网络共享图片，请重新发送 QQ 图片。")
                        source = unquote(url.path)
                        if re.match(r"^/[A-Za-z]:/", source):
                            source = source[1:]
                    path = Path(source).resolve()
                    if not path.is_relative_to(Path(get_astrbot_temp_path()).resolve()):
                        raise ValueError("图片不在消息缓存中，请重新发送 QQ 图片。")
                    with path.open("rb") as file:
                        data = await asyncio.to_thread(file.read, limit + 1)
                if len(data) > limit:
                    raise ValueError("图片不能超过 8 MB，请压缩后重新发送。")
                return data
        except binascii.Error as exc:
            raise ValueError("图片数据无效，请重新发送 QQ 图片。") from exc
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            self.logger.warning("Private image read failed (%s).", type(exc).__name__)
            raise ValueError("图片暂时无法读取，请重新发送。") from exc

    async def scheduler(self) -> None:
        """Finalize due draws independently of slow or failing QQ delivery attempts."""
        next_cleanup = 0.0
        while True:
            try:
                if await self.store.expire_forms():
                    self.wake.set()
                for item in await self.store.snapshot():
                    if item["status"] == "open" and time.time() >= item["draw_at"]:
                        try:
                            await self.store.action(item["id"], "draw", due_only=True)
                            self.logger.info(
                                "Scheduled lottery %s finalized.", item["id"]
                            )
                            self.wake.set()
                        except ValueError:
                            # Another command can finalize the same draw first.
                            continue
                for lottery_id in await self.store.schedule_announcements():
                    self.logger.info(
                        "Lottery %s scheduled group announcement queued.", lottery_id
                    )
                    self.wake.set()
                if time.time() >= next_cleanup:
                    now = time.monotonic()
                    self.private_receipts = OrderedDict(
                        (key, value)
                        for key, value in self.private_receipts.items()
                        if now - value < 1200
                    )
                    removed = await self.store.cleanup_artwork()
                    if removed:
                        self.logger.info(
                            "Removed %d unbound lottery artwork uploads.", removed
                        )
                    next_cleanup = time.time() + 3600
                    avatar_removed = await self.avatars.cleanup(
                        (await self.store.settings())["avatar_cache_hours"]
                    )
                    if avatar_removed:
                        self.logger.info(
                            "Removed %d expired or excess QQ avatar cache files.",
                            avatar_removed,
                        )
            except Exception:
                # Keep unexpected faults from killing the persistent scheduler.
                self.logger.exception(
                    "CatLottery scheduler failure; retrying next cycle."
                )
            await asyncio.sleep(5)

    async def delivery_loop(self) -> None:
        """Recover the durable outbox without blocking the deadline scheduler."""
        while True:
            self.wake.clear()
            try:
                await self.send_deliveries()
            except Exception:
                self.logger.exception(
                    "CatLottery delivery loop failure; retrying next cycle."
                )
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def send_deliveries(self) -> None:
        """Send persisted results and enrollment cards to exact OneBot recipients."""
        async with self.delivery_lock:
            for row in await self.store.deliveries():
                try:
                    message = json.loads(row["body"])
                    item, target = message["item"], message["target"]
                    async with self.store.lock:
                        async with self.store.db.execute(
                            "SELECT 1 FROM outbox WHERE id=? AND delivered_at IS NULL",
                            (row["id"],),
                        ) as cursor:
                            if await cursor.fetchone() is None:
                                continue
                    live = await self.store.participation_state(item["id"])
                    kind = message["kind"]
                    if kind == "closed" and (
                        live["item"]["status"] != "open"
                        or time.time() < live["item"]["close_at"]
                    ):
                        await self.store.delivery_done(row["id"], skipped=True)
                        continue
                    if kind in {"announcement", "participation_guide"} and (
                        live["item"]["status"] != "open"
                        or time.time() >= live["item"]["close_at"]
                        or message.get("scheduled")
                        and item["announcement_schedule"]
                        != live["item"]["announcement_schedule"]
                    ):
                        await self.store.delivery_done(row["id"], skipped=True)
                        self.logger.debug(
                            "Lottery %s obsolete recruitment notice skipped.",
                            item["id"],
                        )
                        continue
                    if target["channel"] == "group" and (
                        not live["item"]["group_success_notify"]
                        and (
                            kind == "success"
                            or kind == "review"
                            and message["entry"].get("review_status") == "approved"
                        )
                        or not live["item"]["group_pending_notify"]
                        and (
                            kind == "submitted"
                            or kind == "review"
                            and message["entry"].get("review_status") == "pending"
                        )
                    ):
                        await self.store.delivery_done(row["id"], skipped=True)
                        self.logger.debug(
                            "Lottery %s group enrollment notice suppressed by current policy.",
                            item["id"],
                        )
                        continue
                    if kind not in {"result", "tier_result", "cancelled"}:
                        # Recruitment and receipts describe current rules; draw records stay frozen.
                        item = live["item"]
                    platform = self.context.get_platform_inst(target["platform_id"])
                    if (
                        platform is None
                        or platform.meta().name != "aiocqhttp"
                        or not platform.config.get("enable", True)
                    ):
                        raise ConnectionError("Platform unavailable")
                    client = platform.get_client()
                    if target["bot_id"] not in client._wsr_api_clients:
                        raise ConnectionError("Bot offline")
                    subtitle = f"{item['title']} · {item['id']}"
                    portrait = None
                    if kind == "question":
                        entry = await self.store.entry(
                            item["id"], message["entry"]["user_id"]
                        )
                        active = await self.store.private_session(
                            target["bot_id"], target["recipient"]
                        )
                        try:
                            index = await self.store.form_position(
                                target["bot_id"], target["recipient"]
                            )
                        except ValueError:
                            index = -1
                        if (
                            not entry
                            or active != item["id"]
                            or index != message["entry"]["question_index"]
                        ):
                            await self.store.delivery_done(row["id"], skipped=True)
                            continue
                        title = f"第 {index + 1} / {len(item['questions'])} 题"
                        sections = question_sections(
                            item, entry, index, self.store.directory / "uploads"
                        )
                    elif kind == "form_timeout":
                        if await self.store.private_session(
                            target["bot_id"], target["recipient"]
                        ):
                            await self.store.delivery_done(row["id"], skipped=True)
                            continue
                        title = "已退出抽奖作答模式"
                        sections = [
                            (
                                "恢复作答",
                                "本轮 20 分钟已结束，你已填写的答案会保留\n报名截止前，私聊发送 /抽奖 继续 可恢复作答\n有多场待办时发送 /抽奖 待办 选择活动\n也可回原报名群发送 /抽奖 参与 "
                                + item["id"]
                                + "，从第一题重新作答",
                            )
                        ]
                    elif kind == "participation_guide":
                        title, sections = "", []
                    elif kind in {"success", "submitted", "review", "answer_changed"}:
                        entry = message["entry"]
                        current = await self.store.entry(item["id"], entry["user_id"])
                        if not current or current.get(
                            "answer_revision", 0
                        ) != entry.get("answer_revision", 0):
                            await self.store.delivery_done(row["id"], skipped=True)
                            self.logger.debug(
                                "Skipped an obsolete lottery eligibility receipt for %s.",
                                item["id"],
                            )
                            continue
                        try:
                            portrait = await asyncio.wait_for(
                                self.avatars.get(
                                    entry["user_id"],
                                    (await self.store.settings())["avatar_cache_hours"],
                                ),
                                timeout=2,
                            )
                        except asyncio.TimeoutError:
                            self.logger.debug(
                                "QQ avatar still loading; delivering the receipt with the brand fallback."
                            )
                        status = entry.get("review_status", "approved")
                        title = entry_text(entry, len(item["questions"]))
                        explanation = {
                            "approved": "已成功参与，每个 QQ 号仅计一次，已获得开奖资格。",
                            "pending": "资料已提交，请等待管理员审核。整份资料审核通过后才有开奖资格。",
                            "rejected": "整体作答未通过审核，暂无开奖资格，请联系活动管理员",
                            "incomplete": "资料尚未填写完整，暂无开奖资格",
                        }[status]
                        if kind == "answer_changed":
                            title = (
                                "整体作答未通过"
                                if entry["status"] != "complete"
                                else "填写资料已更新"
                            )
                            explanation = (
                                "管理员已调整你的填写资料，请查询 /抽奖 状态 "
                                + item["id"]
                            )
                            if entry["status"] != "complete":
                                explanation += (
                                    "\n报名截止前请私聊发送 /抽奖 继续，补齐资料后重新审核"
                                    if item["status"] == "open"
                                    and time.time() < item["close_at"]
                                    else "\n报名已截止，请联系活动管理员处理"
                                )
                            elif not item.get("require_correct", True):
                                explanation += "\n请等待管理员重新审核整份资料"
                        sections = [
                            (
                                "报名确认",
                                f"{entry['nickname']} · QQ {entry['user_id']}\n{explanation}",
                            ),
                            (
                                "奖品与开奖",
                                f"{item['prize']}\n开奖 {date_text(item['draw_at'])}（北京时间）",
                            ),
                        ]
                    elif kind in {"result", "tier_result"}:
                        title = "开奖结果" if kind == "result" else "奖项提前开奖"
                        winners = item["winners"]
                        sections = prize_sections(
                            item, self.store.directory / "artwork", results=True
                        ) + [
                            (
                                "开奖进度",
                                f"已揭晓 {len(item.get('tier_draws', [])) or len(prize_tiers(item))}/{len(prize_tiers(item))} 个奖项\n有效报名 {item['eligible_count']} 人 · 已中奖 {len(winners)} 人"
                                + (
                                    "\n无人符合开奖资格，本次无人中奖。"
                                    if not winners and kind == "result"
                                    else ""
                                ),
                            ),
                            (
                                "开奖记录",
                                f"{date_text(item['drawn_at'] if kind == 'result' else item['tier_draws'][-1]['drawn_at'])}（北京时间）"
                                + (
                                    f"\n剩余奖项开奖：{date_text(item['draw_at'])}"
                                    if kind == "tier_result"
                                    else ""
                                ),
                            ),
                        ]
                    elif kind in {"cancelled", "closed"}:
                        title = (
                            "本次抽奖已取消" if kind == "cancelled" else item["title"]
                        )
                        if kind == "closed":
                            subtitle = f"活动 {item['id']}"
                        sections = [
                            (
                                "活动状态",
                                "本次活动已取消，不再接受报名或开奖。"
                                if kind == "cancelled"
                                else f"已停止报名与填写，已提交的资料仍可在开奖前审核\n自动开奖：{date_text(item['draw_at'])}（北京时间）",
                            )
                        ]
                    else:
                        title, sections = item["title"], []
                    if kind in {"result", "tier_result"} and item.get("cover"):
                        sections.insert(
                            0,
                            (
                                "活动封面",
                                "",
                                self.store.directory / "artwork" / item["cover"],
                            ),
                        )
                    pngs = (
                        []
                        if kind == "participation_guide"
                        else await asyncio.to_thread(
                            render_announcement,
                            item,
                            self.store.directory / "artwork",
                            participation_counts=live,
                        )
                        if kind == "announcement"
                        else await asyncio.to_thread(
                            render_pages,
                            title,
                            subtitle,
                            sections,
                            "私聊回答"
                            if kind == "question"
                            else "报名确认"
                            if kind in {"success", "submitted", "review"}
                            else "开奖通知"
                            if kind in {"result", "tier_result"}
                            else "抽奖公告",
                            **({"avatar_path": portrait} if portrait else {}),
                            participation_counts=live,
                            **({"bold_title": True} if kind == "closed" else {}),
                        )
                    )
                    parameters = {
                        "self_id": int(target["bot_id"]),
                        "message": [
                            {
                                "type": "image",
                                "data": {
                                    "file": "base64://" + base64.b64encode(png).decode()
                                },
                            }
                            for png in pngs
                        ],
                    }
                    if kind == "participation_guide":
                        guide = f"【喵喵抽奖】{item['title']}\n✅ 参与抽奖请发送：/抽奖 参与 {item['id']}"
                        guide += (
                            f"\n✨ 报名后我会私聊发送题目，请按题回答，共 {len(item['questions'])} 题\n未收到题目时，先添加我为好友，私聊发送 /抽奖 继续\n"
                            + (
                                "完成全部题目后获得资格"
                                if item.get("require_correct", True)
                                else "完成全部题目后等待管理员审核，通过后获得资格"
                            )
                            if item["questions"]
                            else "\n✨ 无需填写资料，群内报名即可参与"
                        )
                        guide += f"\n-------\n报名截止：{date_text(item['close_at'])}（北京时间）\n活动详情：/抽奖 详情 {item['id']}"
                        guide += f"\n成功参与 {live['approved']} 人 · 待审核 {live['pending']} 人"
                        parameters["message"] = [
                            {"type": "text", "data": {"text": guide}}
                        ]
                    parameters[
                        "group_id" if target["channel"] == "group" else "user_id"
                    ] = int(target["recipient"])
                    # Rendering and avatar downloads can yield while a review, withdrawal,
                    # cancellation, or notification policy update revokes this message.
                    async with self.store.lock:
                        async with self.store.db.execute(
                            "SELECT 1 FROM outbox WHERE id=? AND delivered_at IS NULL",
                            (row["id"],),
                        ) as cursor:
                            if await cursor.fetchone() is None:
                                continue
                        if kind == "form_timeout":
                            async with self.store.db.execute(
                                "SELECT 1 FROM sessions WHERE bot_id=? AND user_id=?",
                                (target["bot_id"], target["recipient"]),
                            ) as cursor:
                                if await cursor.fetchone() is not None:
                                    continue
                    if kind in {"announcement", "participation_guide", "question"}:
                        current = await self.store.get(item["id"])
                        if (
                            current["status"] != "open"
                            or time.time() >= current["close_at"]
                        ):
                            await self.store.delivery_done(row["id"], skipped=True)
                            continue
                    await asyncio.wait_for(
                        client.call_action(
                            "send_group_msg"
                            if target["channel"] == "group"
                            else "send_private_msg",
                            **parameters,
                        ),
                        timeout=15,
                    )
                    await self.store.delivery_done(row["id"])
                    self.logger.debug(
                        "Lottery %s %s notice delivered to %s.",
                        item["id"],
                        message["kind"],
                        target["channel"],
                    )
                except NETWORK_ERRORS as exc:
                    await self.store.delivery_done(row["id"], type(exc).__name__)
                    self.logger.warning(
                        "Lottery %s %s notice failed for %s; retry queued (%s).",
                        item["id"],
                        message["kind"],
                        target["channel"],
                        type(exc).__name__,
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    await self.store.delivery_done(row["id"], type(exc).__name__)
                    self.logger.error(
                        "Lottery %s notice preparation failed (%s); retry queued.",
                        row["lottery_id"],
                        type(exc).__name__,
                    )

    @web_boundary
    async def web_state(self):
        """Return management state only through the authenticated AstrBot bridge."""
        return json_response(
            {
                "lotteries": await self.store.snapshot(private=True),
                "settings": await self.store.settings(),
                "server_time": time.time(),
                "timezone": "Asia/Shanghai",
            }
        )

    @web_boundary
    async def web_platforms(self):
        """Return auto-discovered enabled platforms without any access tokens."""
        return json_response({"platforms": await self.platform_inventory()})

    @web_boundary
    async def web_settings(self):
        """Save operators and apply the persistent LLM switch from the management Page."""
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("管理员设置必须为对象。")
            settings = await self.store.settings(payload)
            await self.apply_tool_switch()
            self.logger.info("Lottery operator and tool settings updated via WebUI.")
            return json_response(settings)
        except ValueError as exc:
            return error_response(str(exc))

    @web_boundary
    async def web_save(self):
        """Create or edit complete per-lottery rules with live target validation."""
        try:
            payload = await request.json()
            # Store.save enforces deadlines against the current saved activity under its lock.
            rules = validate_lottery(payload, now=0)
            lottery_id = str(payload.get("id", ""))
            # Existing, unchanged targets can retain an offline account's schedules.
            existing = await self.store.get(lottery_id) if lottery_id else None
            if existing is None or existing["targets"] != rules["targets"]:
                await self.verify_targets(rules)
            item = await self.store.save(rules, f"web:{request.username}", lottery_id)
            self.logger.info(
                "Lottery %s %s via WebUI.",
                item["id"],
                "updated" if lottery_id else "created",
            )
            self.wake.set()
            return json_response(item)
        except ValueError as exc:
            return error_response(str(exc))

    @web_boundary
    async def web_detail(self, lottery_id: str):
        """Expose private answers only to an authenticated management session.

        Args:
            lottery_id: Activity identifier from the validated route.

        Returns:
            Entries and per-channel delivery records.
        """
        try:
            item = await self.store.get(lottery_id)
            data = await self.store.manage_entries(lottery_id, page=1)
            data.update(item=item, server_time=time.time())
            return json_response(data)
        except ValueError as exc:
            return error_response(str(exc), status_code=404)

    @web_boundary
    async def web_entries(self, lottery_id: str):
        """Search bounded enrollment summaries without returning private answers.

        Args:
            lottery_id: Activity identifier.

        Returns:
            One filtered page and full activity counts.
        """
        try:
            await self.store.get(lottery_id)
            data = await self.store.manage_entries(
                lottery_id,
                page=int(request.query.get("page", "1")),
                page_size=int(request.query.get("page_size", "20")),
                status=request.query.get("status", "all"),
                query=request.query.get("q", ""),
                include_deliveries=False,
            )
            data.pop("deliveries")
            return json_response(data)
        except (ValueError, TypeError) as exc:
            return error_response(
                str(exc)
                if isinstance(exc, ValueError)
                and not str(exc).startswith("invalid literal")
                else "页码或筛选条件无效"
            )

    @web_boundary
    async def web_entry(self, lottery_id: str, user_id: str):
        """Open one explicitly selected person's full private form for review.

        Args:
            lottery_id: Exact activity identifier.
            user_id: Selected participant's actual QQ number.

        Returns:
            Private entry and current rules, including qualification lock status.
        """
        if not re.fullmatch(r"[1-9][0-9]{4,19}", user_id):
            return error_response("报名不存在", status_code=404)
        try:
            item = await self.store.get(lottery_id)
            entry = await self.store.entry(lottery_id, user_id)
            if not entry:
                raise ValueError("该报名已移除，请刷新列表")
            return json_response(
                {"entry": entry, "item": item, "server_time": time.time()}
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=404)

    @web_boundary
    async def web_avatar(self, user_id: str):
        """Return an authenticated bounded avatar preview with cache expiry.

        Args:
            user_id: Numeric QQ number from a participant row.

        Returns:
            A sanitized data URL, or an empty preview for the identity fallback.
        """
        try:
            hours = (await self.store.settings())["avatar_cache_hours"]
            path = await self.avatars.get(user_id, hours)
            data = await asyncio.to_thread(path.read_bytes) if path else None
            return json_response(
                {
                    "preview": "data:image/jpeg;base64,"
                    + base64.b64encode(data).decode("ascii")
                    if data
                    else "",
                    "expires_at": path.stat().st_mtime + hours * 3600
                    if path
                    else time.time() + 300,
                }
            )
        except ValueError as exc:
            return error_response(str(exc), status_code=400)
        except OSError:
            self.logger.debug(
                "QQ avatar was cleaned during preview; returning fallback."
            )
            return json_response({"preview": "", "expires_at": time.time() + 60})

    @web_boundary
    async def web_clear_avatars(self):
        """Clear only avatar cache files after an explicit Dashboard request.

        Returns:
            The number of removed cache files.
        """
        payload = await request.json()
        if not isinstance(payload, dict) or payload.get("confirmed") is not True:
            return error_response("请确认清理头像缓存")
        removed = await self.avatars.cleanup(clear=True)
        self.logger.info("QQ avatar cache explicitly cleared; removed=%d.", removed)
        return json_response({"removed": removed})

    @web_boundary
    async def web_action(self, lottery_id: str):
        """Execute explicit management operations with final-action confirmation.

        Args:
            lottery_id: Activity identifier from the validated route.

        Returns:
            Saved result or deletion confirmation.
        """
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("操作参数无效。")
            action = payload.get("action")
            if not isinstance(action, str):
                raise ValueError("请选择有效操作。")
            if (
                action in {"draw", "draw_tier", "cancel", "close", "delete"}
                and payload.get("confirmed") is not True
            ):
                raise ValueError("请确认本次操作。")
            if action == "delete":
                await self.store.delete(lottery_id)
                self.logger.info(
                    "Lottery %s and private submissions deleted via WebUI.", lottery_id
                )
                return json_response({"deleted": True})
            if action == "retry":
                await self.store.get(lottery_id)
                async with self.store.lock:
                    await self.store.db.execute(
                        "UPDATE outbox SET next_at=0 WHERE lottery_id=? AND delivered_at IS NULL",
                        (lottery_id,),
                    )
                self.wake.set()
                self.logger.info(
                    "Lottery %s pending notices requeued via WebUI.", lottery_id
                )
                return json_response({"queued": True})
            item = await self.store.action(
                lottery_id, action, tier_index=payload.get("tier_index")
            )
            self.logger.info(
                "Lottery %s action=%s; tier=%s via WebUI.",
                lottery_id,
                action,
                payload.get("tier_index"),
            )
            self.wake.set()
            return json_response(item)
        except ValueError as exc:
            return error_response(str(exc))

    @web_boundary
    async def web_review(self, lottery_id: str):
        """Review private submissions using the authenticated management identity.

        Args:
            lottery_id: Exact activity identifier from the route.

        Returns:
            Review counts or a safe validation error.
        """
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or not isinstance(
                payload.get("action"), str
            ):
                raise ValueError("请选择有效的管理操作")
            if payload.get("action") == "restart_forms":
                if payload.get("confirmed") is not True:
                    raise ValueError("请确认重新通知全部未填写完的用户")
                result = await self.store.restart_forms(lottery_id)
                self.wake.set()
                return json_response(result)
            if payload.get("action") in {
                "edit_answer",
                "delete_answer",
                "delete_entries",
            }:
                result = await self.store.manage_answers(
                    lottery_id, payload, f"web:{request.username}"
                )
            else:
                if payload.get("action") == "mark":
                    raise ValueError("请审核整份填写资料，不支持单题标记")
                if (
                    payload.get("action") == "decision"
                    and type(payload.get("revision")) is not int
                ):
                    raise ValueError("请刷新报名资料后审核")
                if payload.get("action") == "bulk" and (
                    not isinstance(payload.get("revisions"), dict)
                    or not isinstance(payload.get("user_ids"), list)
                    or any(
                        not isinstance(user, str)
                        or type(payload["revisions"].get(user)) is not int
                        for user in payload["user_ids"]
                    )
                ):
                    raise ValueError("请刷新并明确选择报名资料后批量审核")
                result = await self.store.review(
                    lottery_id, payload, f"web:{request.username}"
                )
            self.logger.info(
                "Lottery %s review action=%s; marked=%d; changed=%d.",
                lottery_id,
                payload["action"],
                result["marked_answers"],
                result["changed_entries"],
            )
            self.wake.set()
            return json_response(result)
        except ValueError as exc:
            return error_response(str(exc))

    @web_boundary
    async def web_upload_artwork(self, *, private: bool = False):
        """Validate and sanitize one uploaded award image or lottery cover.

        Args:
            private: Store a private answer image instead of public artwork.

        Returns:
            Generated image reference, or a readable file validation error.
        """
        try:
            files = await request.files()
            uploaded = files.get("file")
            if uploaded is None or len(files.getlist("file")) != 1:
                raise ValueError("请上传一张图片")
            data = await uploaded.read(8 * 1024 * 1024 + 1)
            if not data or len(data) > 8 * 1024 * 1024:
                raise ValueError("图片不能为空，且不能超过 8 MB")
            with await asyncio.to_thread(PillowImage.open, BytesIO(data)) as original:
                if original.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                    raise ValueError("图片支持 JPG、PNG、WebP、GIF，GIF 保存第一帧")
                if original.width * original.height > 24_000_000:
                    raise ValueError("图片不能超过 2400 万像素，请缩小后上传")
                await asyncio.to_thread(original.load)
                with await asyncio.to_thread(
                    ImageOps.exif_transpose, original
                ) as oriented:
                    with await asyncio.to_thread(oriented.convert, "RGBA") as rgba:
                        picture = PillowImage.new("RGB", rgba.size, "white")
                        with picture:
                            await asyncio.to_thread(picture.paste, rgba, (0, 0), rgba)
                            await asyncio.to_thread(
                                picture.thumbnail,
                                (1600, 1600),
                                PillowImage.Resampling.LANCZOS,
                            )
                            filename = f"{secrets.token_hex(16)}.jpg"
                            path = (
                                self.store.directory
                                / ("uploads" if private else "artwork")
                                / filename
                            )
                            try:
                                await asyncio.to_thread(
                                    picture.save, path, "JPEG", quality=90
                                )
                            except OSError as exc:
                                path.unlink(missing_ok=True)
                                raise RuntimeError(
                                    "Could not persist lottery artwork."
                                ) from exc
                            except BaseException:
                                path.unlink(missing_ok=True)
                                raise
            self.logger.info(
                "Lottery artwork uploaded and sanitized; bytes=%d.", len(data)
            )
            return json_response({"image": filename})
        except (UnidentifiedImageError, PillowImage.DecompressionBombError, OSError):
            self.logger.warning("Invalid or unreadable lottery artwork rejected.")
            return error_response(
                "图片无法读取，请换用有效的 JPG、PNG、WebP 或 GIF 文件"
            )
        except ValueError as exc:
            self.logger.debug("Lottery artwork validation rejected.")
            return error_response(str(exc))

    @web_boundary
    async def web_upload_answer_image(self):
        """Sanitize private answer uploads with the same bounded image pipeline.

        Returns:
            A private attachment reference or a validation error.
        """
        return await self.web_upload_artwork(private=True)

    @web_boundary
    async def web_artwork(self, filename: str):
        """Preview public lottery artwork through the authenticated Pages JSON bridge.

        Args:
            filename: Internally generated artwork reference, never a user path or URL.

        Returns:
            A bounded JPEG preview data URL, or a safe not-found response.
        """
        if not re.fullmatch(r"[a-f0-9]{32}\.jpg", filename):
            return error_response("图片不存在", status_code=404)
        path = self.store.directory / "artwork" / filename
        if not path.is_file():
            return error_response("图片不存在或已清理，请重新上传", status_code=404)
        with await asyncio.to_thread(PillowImage.open, path) as picture:
            await asyncio.to_thread(
                picture.thumbnail, (720, 480), PillowImage.Resampling.LANCZOS
            )
            output = BytesIO()
            await asyncio.to_thread(picture.save, output, "JPEG", quality=88)
        return json_response(
            {
                "preview": "data:image/jpeg;base64,"
                + base64.b64encode(output.getvalue()).decode("ascii")
            }
        )

    @web_boundary
    async def web_image(self, filename: str):
        """Download only internally generated form images with Dashboard identity.

        Args:
            filename: Generated UUID-like JPEG filename.

        Returns:
            Image response, or an authorization/not-found error.
        """
        if not re.fullmatch(r"[a-f0-9]{32}\.jpg", filename):
            return error_response("图片不存在。", status_code=404)
        path = self.store.directory / "uploads" / filename
        if not path.is_file():
            return error_response("图片不存在或已删除。", status_code=404)
        if request.query.get("preview") == "1":
            try:
                with await asyncio.to_thread(PillowImage.open, path) as original:
                    with original.convert("RGB") as picture:
                        picture.thumbnail((1000, 1000), PillowImage.Resampling.LANCZOS)
                        output = BytesIO()
                        await asyncio.to_thread(
                            picture.save, output, "JPEG", quality=88
                        )
                return json_response(
                    {
                        "preview": "data:image/jpeg;base64,"
                        + base64.b64encode(output.getvalue()).decode()
                    },
                    headers={"Cache-Control": "no-store"},
                )
            except OSError:
                return error_response("图片不存在或已删除", status_code=404)
        return file_response(
            path,
            filename=filename,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-store"},
        )

    @filter.llm_tool(name="catlottery_list")
    @tool_boundary
    async def tool_list(self, event: AstrMessageEvent) -> str:
        """List lotteries allowed for the actual QQ event's platform and group.

        Use only when the sender asks what lotteries are available. This is
        read-only, returns at most 20 recent activities, and never enrolls anyone.
        Private messages may only inspect
        activities associated with this robot. Returned IDs are the only IDs to
        use in subsequent tools; never invent an activity ID or infer identity
        from a nickname, quoted message, mention, or conversation/session ID.

        Returns:
            Public activity IDs, deadlines, and states, without private answers.
        """
        try:
            identity = self.identity(event)
            items = await self.store.snapshot()
            allowed = [
                x
                for x in items
                if any(
                    t["platform_id"] == identity["platform_id"]
                    and t["bot_id"] == identity["bot_id"]
                    and (
                        not identity["group_id"]
                        or t["group_id"] == identity["group_id"]
                    )
                    for t in x["targets"]
                )
            ]
            return json.dumps(
                [
                    {
                        k: x[k]
                        for k in (
                            "id",
                            "title",
                            "prize",
                            "prize_tiers",
                            "phase",
                            "close_at",
                            "draw_at",
                            "complete_count",
                            "review_pending_count",
                            "group_success_notify",
                            "group_pending_notify",
                            "announcement_schedule",
                        )
                    }
                    for x in allowed[:20]
                ],
                ensure_ascii=False,
            )
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="catlottery_info")
    @tool_boundary
    async def tool_info(self, event: AstrMessageEvent, lottery_id: str) -> str:
        """Show an allowed lottery's public rules as a QQ image; does not enroll.

        Use only for an explicit request to view a specific lottery. Obtain the
        exact ID from catlottery_list or the user's message. Never disclose quiz
        answers or participant form contents. The tool sends an image itself.

        Args:
            lottery_id (string): Exact eight-character activity ID, not a group number or title.

        Returns:
            Public rules, ordered awards, current notification settings, and a
            card_sent flag; quiz references and submitted answers are never returned.
        """
        try:
            identity = self.identity(event)
            item = await self.store.get(lottery_id)
            self.check_target(item, identity)
            await self.show_item(event, item)
            return json.dumps(
                {
                    **{
                        key: item[key]
                        for key in (
                            "id",
                            "title",
                            "status",
                            "close_at",
                            "draw_at",
                            "require_correct",
                            "group_success_notify",
                            "group_pending_notify",
                            "announcement_schedule",
                        )
                    },
                    "awards": [
                        {
                            **tier,
                            "award_number": index + 1,
                            "drawn": item["status"] == "drawn"
                            or any(
                                record["tier_index"] == index
                                for record in item.get("tier_draws", [])
                            ),
                        }
                        for index, tier in enumerate(prize_tiers(item))
                    ],
                    "question_count": len(item["questions"]),
                    "card_sent": True,
                },
                ensure_ascii=False,
            )
        except ValueError as exc:
            await self.error_card(event, "无法查看该抽奖", [("操作提示", str(exc))])
            return str(exc)

    @filter.llm_tool(name="catlottery_join")
    @tool_boundary
    async def tool_join(self, event: AstrMessageEvent, lottery_id: str) -> str:
        """Enroll only the actual sender of an allowed QQ GROUP message.

        Call ONLY when that sender explicitly asks to participate in this exact
        lottery. Listing, asking rules, discussing odds, or talking about someone
        else's enrollment is not consent to join. Never call from a private
        message, never enroll a mentioned/quoted user, and never accept a user_id
        or session ID from the model. If multiple lotteries are possible, ask
        which one first. With questions this only reserves a slot and sends
        private-form instructions. In instant-check mode, all questions must be
        completed; in review mode, completed submissions must also be approved.
        Never claim eligibility while review is pending or rejected. With no
        questions a private success image is queued; a group success image is
        also queued only when enabled for this activity.
        Rejoining an unsuccessful enrollment through its original group clears
        that activity's existing answers and starts question one again. Call
        only with explicit intent to join or restart, never merely to check status.
        Approved participants keep eligibility and never have to answer again.

        Args:
            lottery_id (string): Exact ID selected by the sender, from catlottery_list or a real announcement.

        Returns:
            Complete, already enrolled, pending private form, or rejected.
        """
        try:
            return await self.join(event, lottery_id)
        except ValueError as exc:
            await self.error_card(event, "这次还不能参加", [("报名提示", str(exc))])
            return str(exc)

    @filter.llm_tool(name="catlottery_status")
    @tool_boundary
    async def tool_status(self, event: AstrMessageEvent, lottery_id: str) -> str:
        """Read only the actual QQ sender's registration status for one lottery.

        Use when the sender asks about their own enrollment, result, or current
        estimated chance of winning. Works in allowed groups or the associated
        robot's DM, never creates a slot,
        never reads another person's answers. Submission completion is separate
        from eligibility: pending or rejected review is NOT successful participation.
        No user, nickname, or conversation ID parameter is accepted.

        Args:
            lottery_id (string): Exact activity ID selected by the actual sender.

        Returns:
            Sender-only qualification, result, live counts, and conditional odds.
            Probabilities are fractions from 0 to 1, not percentages. The pool
            estimate assumes the current approved nonwinning candidates and
            remaining awards. Never describe it as a guarantee or a final chance;
            pending, rejected, unregistered, and already winning users have no
            user estimate. No entries or private answers of other users are returned.
        """
        try:
            status = await self.show_status(event, lottery_id)
            return json.dumps(status, ensure_ascii=False)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    @filter.llm_tool(name="catlottery_withdraw")
    @tool_boundary
    async def tool_withdraw(self, event: AstrMessageEvent, lottery_id: str) -> str:
        """Withdraw the actual GROUP sender's own entry before the cutoff.

        Use ONLY for an explicit request to leave this exact lottery. This
        removes that sender's submitted form, never cancels the whole lottery,
        and cannot act for another QQ number, mentioned person, or session ID.
        Private messages cannot withdraw; ask the sender to return to an allowed
        group. Do not use for a request to pause private form filling.

        Args:
            lottery_id (string): Exact ID of the lottery the sender explicitly wants to leave.

        Returns:
            Withdrawal outcome.
        """
        try:
            identity = self.identity(event)
            await self.store.withdraw(lottery_id, identity)
            await self.card(
                event,
                "已退出本次抽奖",
                [("报名已移除", "如果改变主意，截止前可在允许的群内重新报名。")],
                lottery_id,
            )
            return "已退出，该发送者的报名和资料已移除。"
        except ValueError as exc:
            await self.error_card(event, "暂时不能退出", [("操作提示", str(exc))])
            return str(exc)

    @filter.llm_tool(name="catlottery_fill")
    @tool_boundary
    async def tool_fill(self, event: AstrMessageEvent, lottery_id: str) -> str:
        """Start or resume the actual sender's existing private lottery form.

        Call ONLY in a private message when the sender asks to fill or resume
        this exact activity. This NEVER creates an enrollment: the same QQ user
        must already have reserved a slot by joining from an allowed group, and
        must private-message the original robot through the original platform.
        If multiple pending forms exist, ask the sender to select an ID first.
        Group enrollment already activates the form and queues its first private
        question. Use this tool to resume a paused form or switch to an explicitly
        selected pending activity; do not call it before every answer. The current
        question is sent as an image. The sender must then directly
        send their actual text/quiz answer or image; do not fabricate, infer,
        or submit answers on their behalf. Each resume opens a 20-minute answer
        mode, preserving saved answers and selecting the first missing question.
        Mode timeout can be resumed before the enrollment cutoff. Completed
        forms and activities past the cutoff cannot be resumed. The first actual
        answer starts a 5-second collection window for that question; text and
        up to nine separate images can be added until it ends. Never call this
        tool during that window. A group message must be directed to private chat.

        Args:
            lottery_id (string): Exact ID of the sender's already-reserved pending lottery.

        Returns:
            Whether the next question was sent, or why the form cannot resume.
        """
        try:
            identity = self.identity(event)
            if identity["group_id"]:
                raise ValueError(
                    "请私聊报名时联系的我直接回答题目；未收到题目时发送 /抽奖 继续"
                )
            item = await self.store.get(lottery_id)
            self.check_target(item, identity)
            entry = await self.store.entry(lottery_id, identity["user_id"])
            if entry and entry["platform_id"] != identity["platform_id"]:
                raise ValueError("请通过报名时使用的平台私聊我。")
            await self.store.private_session(
                identity["bot_id"], identity["user_id"], lottery_id
            )
            await self.show_question(event, item, entry)
            return "已发送当前题，等待用户真实私聊回答；尚未报名成功。"
        except ValueError as exc:
            await self.error_card(event, "暂时无法填写", [("填写提示", str(exc))])
            return str(exc)

    @filter.llm_tool(name="catlottery_create")
    @tool_boundary
    async def tool_create(
        self,
        event: AstrMessageEvent,
        title: str,
        prize: str,
        winner_count: int,
        close_at: str,
        draw_at: str,
    ) -> str:
        """Create a no-form lottery in the administrator's actual QQ group only.

        Use ONLY when an authorized operator explicitly asks to CREATE a new
        lottery and provides title, prize, winner count, registration cutoff,
        and draw time. Ask about any missing values; never invent prizes or
        dates. No arbitrary group/platform/user arguments are allowed: the
        trusted current group and robot are used. This tool creates ONE single
        prize only. For tiered awards, per-award counts, prize images, covers,
        multiple groups, different platforms,
        or private questions, direct the operator to this plugin's
        management Page. Use catlottery_manage with draw_tier for an existing
        award's early draw and catlottery_notifications to change reminders.
        This creates and announces immediately. For viewing
        an existing lottery use catlottery_info instead. Server validates admin
        permission, enabled AioCqhttp platform, live robot/group, and future dates.

        Args:
            title (string): Operator-provided activity title, 1–80 characters.
            prize (string): Exact operator-provided prize description, 1–300 characters.
            winner_count (number): Integer number of distinct winning QQ users, 1–100; fractions are invalid.
            close_at (string): Explicit future cutoff, ISO 8601 or YYYY-MM-DD HH:MM, default UTC+8.
            draw_at (string): Explicit future draw time, at or after close_at, default UTC+8.

        Returns:
            The actual created ID and queued announcement outcome, or error.
        """
        try:
            identity = self.identity(event)
            await self.check_manager(identity)
            if not identity["group_id"]:
                raise ValueError(
                    "创建工具仅限管理员在目标群使用，其他设置请使用管理页面。"
                )
            rules = validate_lottery(
                {
                    "title": title,
                    "prize": prize,
                    "winner_count": winner_count,
                    "close_at": close_at,
                    "draw_at": draw_at,
                    "targets": [
                        {k: identity[k] for k in ("platform_id", "bot_id", "group_id")}
                    ],
                    "questions": [],
                }
            )
            await self.verify_targets(rules)
            item = await self.store.save(rules, identity["user_id"])
            await self.store.action(item["id"], "publish")
            self.logger.info(
                "Lottery %s created and published via LLM tool.", item["id"]
            )
            self.wake.set()
            await self.card(
                event,
                "抽奖已创建",
                [
                    ("抽奖编号", item["id"]),
                    ("活动规则", "本群抽奖已创建，群公告已排队发送。"),
                ],
                title,
            )
            return json.dumps(
                {"created": item["id"], "announcement_queued": True}, ensure_ascii=False
            )
        except ValueError as exc:
            await self.error_card(event, "还不能创建抽奖", [("操作提示", str(exc))])
            return str(exc)

    @filter.llm_tool(name="catlottery_manage")
    @tool_boundary
    async def tool_manage(
        self,
        event: AstrMessageEvent,
        lottery_id: str,
        action: str,
        confirmed: bool,
        award_number: int = 0,
    ) -> str:
        """Operate one existing lottery as an authorized operator, with explicit intent.

        action must be exactly publish (send public announcement), close (stop
        registration AND private answers now, keep scheduled draw), draw (close
        registration AND draw ALL REMAINING awards immediately, preserving
        earlier award results and irreversibly freezing winners), draw_tier
        (draw ONLY the specified remaining award early, keeping other awards
        on their existing schedule), or cancel
        (end the activity without drawing, rejected if ANY award was drawn).
        For a selected award obtain its ONE-BASED award_number from
        catlottery_info; never infer its position from "一等奖" or its name.
        Never map such a request to draw, which finishes the entire activity.
        Never use draw for a request
        to check draw time, view existing winners, or wait for the schedule.
        Never use cancel for a participant's own withdrawal. confirmed may be
        true ONLY when the actual sender has explicitly requested the exact
        action on the exact lottery; ambiguity requires a follow-up question.
        Requires actual QQ administrator identity and this lottery's allowed
        platform/group. Results are sent to EVERY configured group. Drawing an
        already finalized lottery is rejected; delivery retry never rerolls.
        Only successfully enrolled and approved users enter the draw; unfinished,
        pending-review, and rejected entries are excluded. An empty eligible pool
        produces a final empty result and group notifications, never an error.

        Args:
            lottery_id (string): Exact existing lottery ID selected by the operator.
            action (string): Exactly publish, close, draw, draw_tier, or cancel.
            confirmed (boolean): True only for the operator's explicit request for this exact action and lottery.
            award_number (number): ONE-BASED number from catlottery_info, required for draw_tier; use 0 for every other action. Never pass a zero-based index or guess.

        Returns:
            Actual persisted state and whether group notifications are queued.
        """
        try:
            identity = self.identity(event)
            await self.check_manager(identity)
            item = await self.store.get(lottery_id)
            self.check_target(item, identity)
            if confirmed is not True:
                raise ValueError("需要管理员明确指定抽奖和操作。")
            if not isinstance(action, str) or action not in {
                "publish",
                "close",
                "draw",
                "draw_tier",
                "cancel",
            }:
                raise ValueError("操作仅支持 publish、close、draw、draw_tier、cancel")
            if (
                isinstance(award_number, bool)
                or not isinstance(award_number, int)
                or action == "draw_tier"
                and not 1 <= award_number <= len(prize_tiers(item))
                or action != "draw_tier"
                and award_number != 0
            ):
                raise ValueError("单个奖项开奖请使用详情中的奖项序号，其余操作序号为 0")
            item = await self.store.action(
                lottery_id,
                action,
                tier_index=award_number - 1 if action == "draw_tier" else None,
            )
            self.logger.info("Lottery %s action=%s via LLM tool.", lottery_id, action)
            self.wake.set()
            await self.card(
                event,
                "抽奖操作已完成",
                [
                    (
                        "已执行",
                        {
                            "publish": "发布群公告",
                            "close": "截止报名",
                            "draw": "立即开奖",
                            "draw_tier": f"提前揭晓{prize_tiers(item)[award_number - 1]['name']}"
                            if action == "draw_tier"
                            else "",
                            "cancel": "取消抽奖",
                        }[action],
                    ),
                    ("群通知", "通知已加入发送队列，将发送到本次抽奖的全部群。"),
                ],
                f"{item['title']} · {lottery_id}",
            )
            return json.dumps(
                {
                    "id": lottery_id,
                    "status": item["status"],
                    "notifications_queued": True,
                },
                ensure_ascii=False,
            )
        except ValueError as exc:
            await self.error_card(event, "操作未执行", [("操作提示", str(exc))])
            return str(exc)

    @filter.llm_tool(name="catlottery_notifications")
    @tool_boundary
    async def tool_notifications(
        self,
        event: AstrMessageEvent,
        lottery_id: str,
        setting: str,
        value: str,
        start_at: str = "",
        interval_minutes: int = 60,
        confirmed: bool = False,
    ) -> str:
        """Change ONE notification setting of an existing lottery as an authorized operator.

        Call ONLY for an explicit operator request for this exact setting and
        lottery. Read current rules with catlottery_info when ambiguous; ask for
        missing times or intervals instead of inventing them. This never creates
        a lottery, enrolls anyone, changes questions or prizes, or draws winners.
        setting=group_success controls ONLY group participation-success receipts;
        setting=group_pending controls ONLY group submitted/pending-review receipts.
        Their private receipts always remain enabled. value must be on or off.
        setting=announcement controls GROUP recruitment announcements (image
        plus a separate text message), with value=off, once, or repeat. This
        never schedules repeated private review receipts or result messages.
        once/repeat require an explicitly provided future start_at before the
        registration cutoff. repeat additionally requires the operator's interval.
        Notifications stop at cutoff, cancellation, or final drawing; missed
        cycles do not pile up. off only disables automatic announcements, so
        manual publish remains available. Existing queued automatic notices are
        revoked when the schedule changes. All other settings are preserved.

        Args:
            lottery_id (string): Exact existing lottery ID selected by the operator.
            setting (string): Exactly group_success, group_pending, or announcement; changes only one setting.
            value (string): on/off for group_success/group_pending; off/once/repeat for announcement.
            start_at (string): Explicit future ISO 8601 or YYYY-MM-DD HH:MM time for once/repeat, default UTC+8; empty for other changes.
            interval_minutes (number): Integer 1–10080 explicitly requested for repeat; ignored for other changes, where 60 may be supplied.
            confirmed (boolean): True only for the sender's explicit request to change this exact lottery setting; false performs no change.

        Returns:
            Saved notification policies or a rejection, without private answers.
        """
        try:
            identity = self.identity(event)
            await self.check_manager(identity)
            item = await self.store.get(lottery_id)
            self.check_target(item, identity)
            if confirmed is not True:
                raise ValueError("需要管理员明确指定抽奖和通知设置")
            if not isinstance(setting, str) or setting not in {
                "group_success",
                "group_pending",
                "announcement",
            }:
                raise ValueError("请选择群聊成功通知、群聊待审核通知或群公告计划")
            if not isinstance(value, str) or value not in (
                {"off", "once", "repeat"}
                if setting == "announcement"
                else {"on", "off"}
            ):
                raise ValueError("通知设置值无效")
            if setting == "announcement":
                item["announcement_schedule"] = {
                    "mode": value,
                    "start_at": start_at,
                    "interval_minutes": interval_minutes,
                }
            else:
                item[f"{setting}_notify"] = value == "on"
            saved = await self.store.save(item, identity["user_id"], lottery_id)
            self.logger.info(
                "Lottery %s notification setting=%s value=%s via LLM tool.",
                lottery_id,
                setting,
                value,
            )
            self.wake.set()
            labels = {
                "group_success": "群聊参与成功通知",
                "group_pending": "群聊待审核通知",
                "announcement": "群公告计划",
            }
            values = {
                "on": "已开启",
                "off": "已关闭",
                "once": "定时一次",
                "repeat": "循环发送",
            }
            await self.card(
                event,
                "通知设置已保存",
                [("本次设置", f"{labels[setting]} · {values[value]}")],
                f"{saved['title']} · {lottery_id}",
            )
            return json.dumps(
                {
                    "id": lottery_id,
                    "group_success_notify": saved["group_success_notify"],
                    "group_pending_notify": saved["group_pending_notify"],
                    "announcement_schedule": saved["announcement_schedule"],
                    "announcement_next_at": saved.get("announcement_next_at"),
                },
                ensure_ascii=False,
            )
        except ValueError as exc:
            await self.error_card(event, "通知设置未修改", [("操作提示", str(exc))])
            return str(exc)
