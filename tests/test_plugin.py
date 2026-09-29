"""Verify real AstrBot event identity, OneBot routing, permissions, and recovery."""

import asyncio
import base64
import json
import sqlite3
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from astrbot.api.message_components import Image, Plain
from astrbot.api.web import PluginRequest, bind_request_context
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.dashboard.services.plugin_page_service import PluginPageService
from astrbot_plugin_catlottery.main import TOOL_NAMES, CatLottery
from astrbot_plugin_catlottery.storage import Store
from PIL import Image as PillowImage
from starlette.requests import Request


@pytest.mark.parametrize("command", [False, True])
async def test_original_onebot_text_survives_adapter_whitespace_trimming(
    plugin, rules, command
):
    rules["questions"] = [{"kind": "text", "prompt": "保留原文"}]
    item = await plugin.store.save(rules, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    original = "  first  line\nsecond  line  "
    raw = f"/抽奖 回答 {original}" if command else original
    current = event(plugin, group="", text=raw.strip())
    current.message_obj.raw_message["message"] = [
        {"type": "text", "data": {"text": raw}}
    ]
    await (
        plugin.lottery_command(current) if command else plugin.collect_private(current)
    )
    assert (await plugin.store.entry(item["id"], "4444444"))["answers"][0][
        "value"
    ] == original


@pytest.mark.parametrize("prefix", ["!", "猫猫 "])
async def test_custom_wake_prefix_preserves_answers_and_ignores_other_commands(
    plugin, rules, prefix
):
    plugin.context.get_config = lambda umo=None: {"wake_prefix": [prefix]}
    rules["questions"] = [{"kind": "text", "prompt": "保留原文"}]
    item = await plugin.store.save(rules, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    unrelated = event(plugin, group="", text=f"{prefix}help")
    await plugin.collect_private(unrelated)
    assert not (await plugin.store.entry(item["id"], "4444444"))["answers"]
    original = "  first  line\nsecond  line  "
    raw = f"{prefix}抽奖 回答 {original}"
    current = event(plugin, group="", text=raw.strip())
    current.message_str = f"抽奖 回答 {original.strip()}"
    current.message_obj.raw_message["message"] = [
        {"type": "text", "data": {"text": raw}}
    ]
    await plugin.lottery_command(current)
    assert (await plugin.store.entry(item["id"], "4444444"))["answers"][0][
        "value"
    ] == original


@pytest.mark.parametrize(
    "source",
    [
        "http://127.0.0.1/secret",
        "http://169.254.169.254/latest/",
        "https://gchat.qpic.cn.example.org/x",
        "https://user:password@gchat.qpic.cn/x",
        "https://gchat.qpic.cn:444/x",
        "file://server/share/image.jpg",
        "C:/Windows/private.jpg",
    ],
)
async def test_untrusted_private_image_sources_are_rejected_before_io(plugin, source):
    with pytest.raises(ValueError):
        await plugin.read_submission_image(Image(file=source))
    assert plugin.image_session is None


async def test_private_image_download_stops_at_limit_and_disables_redirects(plugin):
    response = AsyncMock()
    response.status = 200
    response.content_length = None

    async def chunks(size):
        for _ in range(130):
            yield b"x" * size
        pytest.fail("Oversized response was fully consumed")

    response.content.iter_chunked = chunks
    context = AsyncMock()
    context.__aenter__.return_value = response
    plugin.image_session = Mock(
        closed=False, get=Mock(return_value=context), close=AsyncMock()
    )
    with pytest.raises(ValueError, match="8 MB"):
        await plugin.read_submission_image(Image.fromURL("https://gchat.qpic.cn/image"))
    assert plugin.image_session.get.call_args.kwargs["allow_redirects"] is False


async def test_private_image_orientation_and_transparency_are_sanitized(plugin, rules):
    rules["questions"] = [{"kind": "image", "prompt": "图片"}]
    item = await plugin.store.save(rules, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    data = BytesIO()
    with PillowImage.new("RGBA", (30, 50), (0, 0, 0, 0)) as source:
        exif = source.getexif()
        exif[274] = 6
        source.save(data, "PNG", exif=exif)
    await plugin.collect_private(
        event(plugin, group="", components=[Image.fromBytes(data.getvalue())])
    )
    entry = await plugin.store.entry(item["id"], "4444444")
    with PillowImage.open(
        plugin.store.directory / "uploads" / entry["answers"][0]["value"]
    ) as image:
        assert image.size == (50, 30)
        assert min(image.getpixel((0, 0))) > 245
        assert not image.getexif()


async def test_notice_revoked_during_render_is_not_sent(plugin, rules, monkeypatch):
    item = await plugin.store.save(rules, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    reached = asyncio.Event()
    release = asyncio.Event()

    async def avatar(*args):
        reached.set()
        await release.wait()
        return None

    plugin.avatars.get = avatar
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.main.render_pages", lambda *a, **k: [b"image"]
    )
    sending = asyncio.create_task(plugin.send_deliveries())
    await asyncio.wait_for(reached.wait(), 2)
    await plugin.store.withdraw(item["id"], plugin.identity(event(plugin)))
    release.set()
    await sending
    assert not plugin.context.get_platform_inst("napcat").client.calls


async def test_failed_error_reply_does_not_raise_again(plugin):
    current = event(plugin, text="抽奖 不存在")
    current.send = AsyncMock(side_effect=ConnectionError("offline"))
    await plugin.lottery_command(current)
    current.send.assert_awaited_once()


class Client:
    """Fake only the external OneBot transport, preserving framework event behavior."""

    def __init__(self, bot_id="1234567", groups=None):
        self.bot_id = bot_id
        self.groups = groups or ["2222222"]
        self._wsr_api_clients = {bot_id: object()}
        self.calls = []
        self.friends = ["4444444"]
        self.fail_private = False

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "get_login_info":
            return {
                "user_id": int(kwargs.get("self_id", self.bot_id)),
                "nickname": "猫猫机器人",
            }
        if action == "get_group_list":
            return [
                {"group_id": int(group), "group_name": "好运群"}
                for group in self.groups
            ]
        if action == "get_friend_list":
            return [{"user_id": int(friend)} for friend in self.friends]
        if action == "send_private_msg" and self.fail_private:
            raise ConnectionError("Offline private channel")
        return {"message_id": "1"}

    async def send_group_msg(self, **kwargs):
        return await self.call_action("send_group_msg", **kwargs)

    async def send_private_msg(self, **kwargs):
        return await self.call_action("send_private_msg", **kwargs)


class Platform:
    """Supply the same metadata and client boundaries used by Context."""

    def __init__(self, platform_id, client, enabled=True, name="aiocqhttp"):
        self.config = {"enable": enabled, "name": platform_id}
        self.metadata = PlatformMetadata(name, "test adapter", platform_id)
        self.client = client

    def meta(self):
        return self.metadata

    def get_client(self):
        return self.client


@pytest.fixture
async def plugin(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.main.get_astrbot_temp_path", lambda: str(tmp_path)
    )
    clients = [Client(), Client("7654321", ["3333333"])]
    platforms = [Platform("napcat", clients[0]), Platform("snowluma", clients[1])]
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=platforms),
        get_platform_inst=lambda platform_id: next(
            (platform for platform in platforms if platform.meta().id == platform_id),
            None,
        ),
        get_config=lambda umo=None: {"admins_id": ["9999999"]},
        registered_web_apis=[],
        register_web_api=lambda *args: None,
        activate_llm_tool_async=AsyncMock(return_value=True),
        deactivate_llm_tool_async=AsyncMock(return_value=True),
    )
    instance = CatLottery(context)
    instance.store = Store(tmp_path)
    instance.avatars.get = AsyncMock(return_value=None)
    await instance.store.open()
    yield instance
    await instance.terminate()


async def test_tool_switch_blocks_all_tools_and_leaves_qq_commands_available(
    plugin, rules
):
    item = await plugin.store.save(rules, "admin")
    with web_request({"manager_ids": [], "llm_tools_enabled": False}):
        assert (await plugin.web_settings()).status_code == 200
    assert [
        call.args[0]
        for call in plugin.context.deactivate_llm_tool_async.await_args_list
    ] == list(TOOL_NAMES)
    for handler, args in (
        (plugin.tool_list, ()),
        (plugin.tool_info, (item["id"],)),
        (plugin.tool_join, (item["id"],)),
        (plugin.tool_status, (item["id"],)),
        (plugin.tool_withdraw, (item["id"],)),
        (plugin.tool_fill, (item["id"],)),
        (plugin.tool_create, ("标题", "奖品", 1, "2030-01-01", "2030-01-02")),
        (plugin.tool_manage, (item["id"], "draw", True)),
        (plugin.tool_notifications, (item["id"], "group_pending", "off", "", 60, True)),
    ):
        assert "已关闭" in await handler(event(plugin), *args)
    assert (await plugin.store.get(item["id"]))["status"] == "open"
    assert len(await plugin.store.snapshot()) == 1
    assert (await plugin.store.snapshot())[0]["entry_count"] == 0
    await plugin.lottery_command(event(plugin, text=f"抽奖 参与 {item['id']}"))
    assert (await plugin.store.entry(item["id"], "4444444"))["status"] == "complete"
    with web_request({"manager_ids": [], "llm_tools_enabled": True}):
        assert (await plugin.web_settings()).status_code == 200
    assert [
        call.args[0] for call in plugin.context.activate_llm_tool_async.await_args_list
    ] == list(TOOL_NAMES)
    assert "已关闭" not in await plugin.tool_list(event(plugin))


async def test_tool_switch_supports_astrbot_427_api_and_startup_restore(plugin):
    del plugin.context.activate_llm_tool_async
    del plugin.context.deactivate_llm_tool_async
    plugin.context.activate_llm_tool = Mock(return_value=True)
    plugin.context.deactivate_llm_tool = Mock(return_value=True)
    await plugin.store.settings({"manager_ids": [], "llm_tools_enabled": False})
    await plugin.store.close()
    await plugin.initialize()
    await plugin.restore_tool_switch()
    assert plugin.context.deactivate_llm_tool.call_count == len(TOOL_NAMES) * 2
    await plugin.store.settings({"manager_ids": [], "llm_tools_enabled": True})
    await plugin.apply_tool_switch()
    assert [
        call.args[0] for call in plugin.context.activate_llm_tool.call_args_list
    ] == list(TOOL_NAMES)


async def test_multiple_questions_accept_mixed_messages_and_preserve_spaces(
    plugin, rules, tmp_path
):
    rules["require_correct"] = False
    rules["questions"] = [
        {"kind": "quiz", "prompt": "两词之间两个空格", "answers": ["first  second"]},
        {"kind": "mixed", "prompt": "文字和图片都必填"},
        {"kind": "image", "prompt": "图片可附说明"},
    ]
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.lottery_command(
        event(plugin, group="", text=f"抽奖 填写 {item['id']}")
    )
    first = "  FIRST  SECOND\n "
    await plugin.collect_private(event(plugin, group="", text=first))
    entry = await plugin.store.entry(item["id"], "4444444")
    assert entry["answers"][0]["value"] == first
    source = tmp_path / "mixed.png"
    PillowImage.new("RGB", (64, 64), "pink").save(source)
    await plugin.collect_private(event(plugin, group="", text="missing image"))
    await plugin.collect_private(
        event(plugin, group="", components=[Image.fromFileSystem(source)])
    )
    assert len((await plugin.store.entry(item["id"], "4444444"))["answers"]) == 1
    text = "  /docs/a b\n  line  two  "
    await plugin.lottery_command(
        event(
            plugin,
            group="",
            components=[Plain(f"/抽奖 回答 {text}"), Image.fromFileSystem(source)],
        )
    )
    await plugin.collect_private(
        event(
            plugin,
            group="",
            components=[Plain("  photo  caption\n"), Image.fromFileSystem(source)],
        )
    )
    entry = await plugin.store.entry(item["id"], "4444444")
    assert entry["status"] == "complete"
    assert entry["review_status"] == "pending"
    assert entry["answers"][1]["kind"] == "mixed"
    assert entry["answers"][1]["value"] == text
    assert entry["answers"][2]["text"] == "  photo  caption\n"
    for answer in entry["answers"][1:]:
        filename = answer["value"] if answer["kind"] == "image" else answer["image"]
        assert (plugin.store.directory / "uploads" / filename).is_file()
    assert len(await plugin.store.deliveries()) == 2


async def test_wrong_quiz_with_image_discards_file_but_correct_text_keeps_both(
    plugin, rules, tmp_path
):
    rules["questions"] = [
        {"kind": "quiz", "prompt": "包含空格的答案", "answers": ["a  b"]}
    ]
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.store.private_session("1234567", "4444444", item["id"])
    source = tmp_path / "answer.png"
    PillowImage.new("RGB", (64, 64), "blue").save(source)
    for text, accepted in (("a b", False), ("  A  B\n", True)):
        await plugin.collect_private(
            event(
                plugin, group="", components=[Plain(text), Image.fromFileSystem(source)]
            )
        )
        entry = await plugin.store.entry(item["id"], "4444444")
        assert bool(entry["answers"]) is accepted
        assert len(list((plugin.store.directory / "uploads").glob("*.jpg"))) == int(
            accepted
        )
    assert entry["answers"][0]["value"] == "  A  B\n"
    assert entry["answers"][0]["image"]


async def test_deferred_review_keeps_wrong_mixed_answer_and_truthful_status(
    plugin, rules, tmp_path, monkeypatch
):
    rules.update(
        require_correct=False,
        questions=[
            {"kind": "quiz", "prompt": "正确答案有两个空格", "answers": ["a  b"]},
            {"kind": "text", "prompt": "第二题"},
        ],
    )
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.store.private_session("1234567", "4444444", item["id"])
    source = tmp_path / "deferred.png"
    PillowImage.new("RGB", (80, 80), "pink").save(source)
    await plugin.collect_private(
        event(
            plugin,
            group="",
            components=[Plain("  wrong  answer\n"), Image.fromFileSystem(source)],
        )
    )
    entry = await plugin.store.entry(item["id"], "4444444")
    assert entry["answers"][0]["value"] == "  wrong  answer\n"
    assert entry["answers"][0]["correct"] is None
    assert len(list((plugin.store.directory / "uploads").glob("*.jpg"))) == 1
    await plugin.collect_private(event(plugin, group="", text="second answer"))
    status = json.loads(await plugin.tool_status(event(plugin), item["id"]))
    assert (
        status["status"] == "complete"
        and status["review_status"] == "pending"
        and status["eligible"] is False
    )
    assert "等待审核" in await plugin.join(event(plugin), item["id"])
    with web_request({"action": "match"}):
        response = await plugin.web_review(item["id"])
    assert response.status_code == 200
    assert json.loads(response.body)["marked_answers"] == 0
    for index in (0, 1):
        with web_request(
            {
                "action": "mark",
                "user_id": "4444444",
                "question_index": index,
                "correct": True,
            },
            username="reviewer",
        ):
            assert (await plugin.web_review(item["id"])).status_code == 200
    status = json.loads(await plugin.tool_status(event(plugin, group=""), item["id"]))
    assert status["eligible"] is True and status["review_status"] == "approved"
    captured = []

    def capture(title, subtitle, sections, badge, **kwargs):
        captured.append((title, sections))
        return [b"image"]

    monkeypatch.setattr("astrbot_plugin_catlottery.main.render_pages", capture)
    await plugin.send_deliveries()
    assert len(captured) == 2
    assert all(title == "参与成功" for title, _ in captured)
    assert "wrong  answer" not in str(captured)
    client = plugin.context.get_platform_inst("napcat").client
    assert {name for name, kwargs in client.calls[-2:]} == {
        "send_group_msg",
        "send_private_msg",
    }


async def test_empty_eligible_pool_announces_saved_result_to_each_group(
    plugin, rules, monkeypatch
):
    rules.update(
        require_correct=False, questions=[{"kind": "text", "prompt": "需要审核的资料"}]
    )
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.store.private_session("1234567", "4444444", item["id"])
    await plugin.collect_private(event(plugin, group="", text="not reviewed"))
    drawn = await plugin.store.action(item["id"], "draw")
    assert drawn["eligible_count"] == 0 and drawn["winners"] == []
    captured = []

    def capture(title, subtitle, sections, badge, **kwargs):
        if badge == "开奖通知":
            captured.append(sections)
        return [b"image"]

    monkeypatch.setattr("astrbot_plugin_catlottery.main.render_pages", capture)
    await plugin.send_deliveries()
    assert len(captured) == 2
    assert all("无人符合开奖资格" in str(sections) for sections in captured)
    for platform_id, group_id in (("napcat", 2222222), ("snowluma", 3333333)):
        client = plugin.context.get_platform_inst(platform_id).client
        assert any(
            action == "send_group_msg" and params["group_id"] == group_id
            for action, params in client.calls
        )


@pytest.fixture
def rules():
    return {
        "title": "猫猫贴纸抽奖",
        "prize": "一套贴纸",
        "winner_count": 1,
        "close_at": time.time() + 3600,
        "draw_at": time.time() + 7200,
        "targets": [
            {"platform_id": "napcat", "bot_id": "1234567", "group_id": "2222222"},
            {"platform_id": "snowluma", "bot_id": "7654321", "group_id": "3333333"},
        ],
        "questions": [],
    }


def event(
    plugin,
    *,
    text="抽奖",
    user="4444444",
    platform="napcat",
    group="2222222",
    components=None,
):
    """Build actual framework event objects with verified OneBot raw fields.

    Args:
        plugin: Plugin fixture supplying the transport.
        text: Plain message content.
        user: Actual sender QQ number.
        platform: Adapter identifier.
        group: Group number, or empty for a private event.
        components: Optional actual AstrBot message components.

    Returns:
        Real AiocqhttpMessageEvent instance.
    """
    adapter = plugin.context.get_platform_inst(platform)
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    message.sender = MessageMember(user, "实际发送者")
    message.self_id = adapter.client.bot_id
    message.group_id = group
    message.message_str = text
    message.message = components if components is not None else [Plain(text)]
    message.raw_message = {
        "post_type": "message",
        "message_type": "group" if group else "private",
        "self_id": int(message.self_id),
        "user_id": int(user),
    }
    if group:
        message.raw_message["group_id"] = int(group)
    return AiocqhttpMessageEvent(
        text, message, adapter.meta(), group or user, adapter.client
    )


async def test_root_unknown_and_missing_command_all_send_images(plugin):
    for text in ("抽奖", "抽奖 不存在", "抽奖 参与", "抽奖 创建 invalid"):
        current = event(plugin, text=text)
        await plugin.lottery_command(current)
        assert current.is_stopped()
    client = plugin.context.get_platform_inst("napcat").client
    notices = [kwargs for name, kwargs in client.calls if name == "send_group_msg"]
    assert len(notices) == 4
    assert all(message["message"][0]["type"] == "image" for message in notices)


async def test_identity_rejects_quoted_sender_spoofing_and_private_join(plugin, rules):
    item = await plugin.store.save(rules, "admin")
    current = event(plugin)
    identity = plugin.identity(current)
    assert identity["user_id"] == "4444444"
    current.message_obj.raw_message["user_id"] = 5555555
    with pytest.raises(ValueError, match="身份不一致"):
        plugin.identity(current)
    outcome = await plugin.tool_join(event(plugin, group=""), item["id"])
    assert "私聊不能" in outcome
    assert (await plugin.store.snapshot())[0]["entry_count"] == 0


async def test_enabled_platforms_detect_multiple_accounts_and_exclude_other_types(
    plugin,
):
    platforms = plugin.context.platform_manager.platform_insts
    platforms[0].client._wsr_api_clients["2345678"] = object()
    platforms.append(Platform("disabled", Client(), enabled=False))
    platforms.append(Platform("telegram", Client(), name="telegram"))
    inventory = await plugin.platform_inventory()
    assert {platform["id"] for platform in inventory} == {"napcat", "snowluma"}
    assert {account["bot_id"] for account in inventory[0]["accounts"]} == {
        "1234567",
        "2345678",
    }


async def test_success_group_and_private_delivery_are_independent(plugin, rules):
    item = await plugin.store.save(rules, "admin")
    client = plugin.context.get_platform_inst("napcat").client
    client.fail_private = True
    await plugin.join(event(plugin), item["id"])
    await plugin.send_deliveries()
    details = await plugin.store.manage_entries(item["id"])
    assert any(
        delivery["target"]["channel"] == "group" and delivery["delivered_at"]
        for delivery in details["deliveries"]
    )
    assert any(
        delivery["target"]["channel"] == "private" and delivery["attempts"] == 1
        for delivery in details["deliveries"]
    )
    assert (await plugin.store.entry(item["id"], "4444444"))["status"] == "complete"


async def test_duplicate_pending_from_other_bot_still_guides_original_bot(
    plugin, rules, monkeypatch
):
    rules["questions"] = [{"kind": "text", "prompt": "昵称"}]
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    captured = []

    async def capture(current_event, title, sections, subtitle="", badge="", **kwargs):
        counts = kwargs["participation_counts"]
        assert (counts["approved"], counts["pending"]) == (0, 0)
        captured.append(sections)

    monkeypatch.setattr(plugin, "card", capture)
    await plugin.join(event(plugin, platform="snowluma", group="3333333"), item["id"])
    assert "1234567" in captured[0][0][1]
    assert "7654321" not in captured[0][0][1]


async def test_real_private_messages_complete_form_and_store_image(
    plugin, rules, tmp_path
):
    rules["questions"] = [
        {"kind": "quiz", "prompt": "选猫", "options": ["猫", "狗"], "answers": ["猫"]},
        {"kind": "text", "prompt": "填写昵称"},
        {"kind": "image", "prompt": "发送猫图"},
    ]
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.lottery_command(
        event(plugin, group="", text=f"抽奖 填写 {item['id']}")
    )
    await plugin.collect_private(event(plugin, group="", text="B"))
    assert len((await plugin.store.entry(item["id"], "4444444"))["answers"]) == 0
    await plugin.collect_private(event(plugin, group="", text="A"))
    await plugin.collect_private(event(plugin, group="", text="独立资料"))
    source = tmp_path / "source.png"
    PillowImage.new("RGBA", (100, 100), (255, 220, 230, 255)).save(source)
    await plugin.collect_private(
        event(plugin, group="", text="", components=[Image.fromFileSystem(source)])
    )
    entry = await plugin.store.entry(item["id"], "4444444")
    assert entry["status"] == "complete"
    filename = entry["answers"][2]["value"]
    assert (plugin.store.directory / "uploads" / filename).is_file()
    with PillowImage.open(plugin.store.directory / "uploads" / filename) as image:
        assert image.format == "JPEG"
    assert len(await plugin.store.deliveries()) == 2


async def test_oversize_and_invalid_private_images_do_not_advance(
    plugin, rules, tmp_path
):
    rules["questions"] = [{"kind": "image", "prompt": "发图"}]
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.store.private_session("1234567", "4444444", item["id"])
    invalid = tmp_path / "invalid.png"
    invalid.write_bytes(b"not an image")
    await plugin.collect_private(
        event(plugin, group="", text="", components=[Image.fromFileSystem(invalid)])
    )
    oversized = tmp_path / "large.png"
    oversized.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    await plugin.collect_private(
        event(plugin, group="", text="", components=[Image.fromFileSystem(oversized)])
    )
    entry = await plugin.store.entry(item["id"], "4444444")
    assert entry["answers"] == []
    assert not list((plugin.store.directory / "uploads").iterdir())


async def test_llm_admin_tools_enforce_real_sender_and_per_lottery_platform(
    plugin, rules
):
    item = await plugin.store.save(rules, "admin")
    outcome = await plugin.tool_manage(event(plugin), item["id"], "draw", True)
    assert "仅限" in outcome
    assert (await plugin.store.get(item["id"]))["status"] == "open"
    outcome = await plugin.tool_manage(
        event(plugin, user="9999999"), item["id"], "draw", False
    )
    assert "明确指定" in outcome
    await plugin.tool_manage(event(plugin, user="9999999"), item["id"], "draw", True)
    assert (await plugin.store.get(item["id"]))["status"] == "drawn"


async def test_llm_public_info_has_award_numbers_and_no_answer_references(
    plugin, rules
):
    rules.update(
        require_correct=False,
        questions=[{"kind": "quiz", "prompt": "问题", "answers": ["secret-reference"]}],
        prize_tiers=[
            {"name": "二等奖", "prize": "玩偶", "count": 1},
            {"name": "一等奖", "prize": "贴纸", "count": 1},
        ],
    )
    item = await plugin.store.save(rules, "admin")
    info = json.loads(await plugin.tool_info(event(plugin), item["id"]))
    assert "secret-reference" not in json.dumps(info)
    assert [award["award_number"] for award in info["awards"]] == [1, 2]
    assert info["card_sent"] and info["question_count"] == 1
    rejected = await plugin.tool_manage(
        event(plugin, user="9999999"), item["id"], "draw", True, award_number=2
    )
    assert "序号" in rejected
    assert (await plugin.store.get(item["id"]))["tier_draws"] == []
    await plugin.tool_manage(
        event(plugin, user="9999999"), item["id"], "draw_tier", True, award_number=2
    )
    saved = await plugin.store.get(item["id"])
    assert saved["status"] == "open"
    assert [record["tier_index"] for record in saved["tier_draws"]] == [1]


async def test_llm_notifications_require_real_admin_explicit_confirmation_and_exact_scope(
    plugin, rules
):
    item = await plugin.store.save(rules, "admin")
    assert "仅限" in await plugin.tool_notifications(
        event(plugin), item["id"], "group_pending", "off", confirmed=True
    )
    assert "明确指定" in await plugin.tool_notifications(
        event(plugin, user="9999999"),
        item["id"],
        "group_pending",
        "off",
        confirmed="true",
    )
    assert (await plugin.store.get(item["id"]))["group_pending_notify"] is True
    result = json.loads(
        await plugin.tool_notifications(
            event(plugin, user="9999999"),
            item["id"],
            "group_pending",
            "off",
            confirmed=True,
        )
    )
    assert (
        result["group_pending_notify"] is False
        and result["group_success_notify"] is True
    )
    wrong_platform = event(plugin, platform="snowluma", user="9999999", group="8888888")
    assert "允许列表" in await plugin.tool_notifications(
        wrong_platform, item["id"], "group_success", "off", confirmed=True
    )
    assert (await plugin.store.get(item["id"]))["group_success_notify"] is True
    start = time.time() + 120
    scheduled = json.loads(
        await plugin.tool_notifications(
            event(plugin, user="9999999"),
            item["id"],
            "announcement",
            "repeat",
            start_at=start,
            interval_minutes=15,
            confirmed=True,
        )
    )
    assert scheduled["announcement_next_at"] == start
    assert scheduled["announcement_schedule"]["interval_minutes"] == 15
    assert scheduled["group_pending_notify"] is False


async def test_scheduled_announcement_uses_live_counts_and_separate_text(
    plugin, rules, monkeypatch
):
    start = time.time() + 60
    item = await plugin.store.save(
        {**rules, "announcement_schedule": {"mode": "once", "start_at": start}}, "admin"
    )
    await plugin.store.schedule_announcements(now=start)
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    captured = []

    def capture(item, directory, **kwargs):
        captured.append(kwargs["participation_counts"])
        return [b"image"]

    monkeypatch.setattr("astrbot_plugin_catlottery.main.render_announcement", capture)
    await plugin.send_deliveries()
    await plugin.send_deliveries()
    assert all((value["approved"], value["pending"]) == (1, 0) for value in captured)
    for platform in plugin.context.platform_manager.platform_insts:
        calls = [
            params
            for action, params in platform.client.calls
            if action == "send_group_msg"
        ]
        texts = [
            params["message"][0]["data"]["text"]
            for params in calls
            if params["message"][0]["type"] == "text"
        ]
        assert len(texts) == 1
        assert "成功参与 1 人 · 待审核 0 人" in texts[0]


async def test_pending_group_switch_preserves_private_confirmation(
    plugin, rules, monkeypatch
):
    rules.update(
        group_pending_notify=False,
        require_correct=False,
        questions=[{"kind": "text", "prompt": "資料"}],
    )
    item = await plugin.store.save(rules, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    await plugin.collect_private(event(plugin, group="", text="submitted"))
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.main.render_pages",
        lambda *args, **kwargs: [b"image"],
    )
    for platform in plugin.context.platform_manager.platform_insts:
        platform.client.calls.clear()
    await plugin.send_deliveries()
    calls = plugin.context.get_platform_inst("napcat").client.calls
    assert [action for action, params in calls] == ["send_private_msg"]
    assert (await plugin.store.participation_state(item["id"]))["pending"] == 1


async def test_due_draw_recovery_not_blocked_by_slow_delivery(plugin, rules):
    item = await plugin.store.save(rules, "admin")
    item["close_at"] = item["draw_at"] = time.time() - 1
    await plugin.store.db.execute(
        "UPDATE lotteries SET body=? WHERE id=?", (json.dumps(item), item["id"])
    )
    task = asyncio.create_task(plugin.scheduler())
    try:
        for _ in range(50):
            if (await plugin.store.get(item["id"]))["status"] == "drawn":
                break
            await asyncio.sleep(0.01)
        assert (await plugin.store.get(item["id"]))["status"] == "drawn"
        assert len(await plugin.store.deliveries()) == 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def web_request(payload=None, username="operator", *, uploaded=None, query=""):
    """Bind real framework request objects without running a public HTTP server.

    Args:
        payload: JSON request body, including malformed shapes under test.
        username: Dashboard identity or None for an unauthenticated caller.
        uploaded: Optional real multipart file bytes for image endpoint tests.

    Returns:
        Context manager exposing the real plugin request proxy.
    """

    body = json.dumps(payload).encode()
    headers = []
    if uploaded is not None:
        body = (
            b'--catlottery-test\r\nContent-Disposition: form-data; name="file"; filename="cover.png"\r\nContent-Type: image/png\r\n\r\n'
            + uploaded
            + b"\r\n--catlottery-test--\r\n"
        )
        headers = [(b"content-type", b"multipart/form-data; boundary=catlottery-test")]

    async def receive():
        return {
            "type": "http.request",
            "body": body,
            "more_body": False,
        }

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": headers,
        "query_string": query.encode(),
        "server": ("localhost", 6185),
        "scheme": "http",
    }
    return bind_request_context(
        PluginRequest(Request(scope, receive), username=username)
    )


async def test_all_management_endpoints_deny_unauthenticated_access(plugin):
    for handler, args in (
        (plugin.web_state, ()),
        (plugin.web_platforms, ()),
        (plugin.web_settings, ()),
        (plugin.web_save, ()),
        (plugin.web_detail, ("12345678",)),
        (plugin.web_action, ("12345678",)),
        (plugin.web_review, ("12345678",)),
        (plugin.web_image, ("a" * 32 + ".jpg",)),
        (plugin.web_entries, ("12345678",)),
        (plugin.web_entry, ("12345678", "4444444")),
        (plugin.web_avatar, ("4444444",)),
        (plugin.web_clear_avatars, ()),
    ):
        with web_request(username=None):
            assert (await handler(*args)).status_code == 403


async def test_real_request_query_pagination_single_entry_and_private_image_preview(
    plugin, rules
):
    rules.update(
        require_correct=False, questions=[{"kind": "mixed", "prompt": "图片与文字"}]
    )
    item = await plugin.store.save(rules, "admin")
    filename = "a" * 32 + ".jpg"
    with PillowImage.new("RGB", (1500, 1200), "pink") as picture:
        picture.save(plugin.store.directory / "uploads" / filename)
    for index in range(25):
        identity = {
            **plugin.identity(event(plugin)),
            "user_id": str(4444444 + index),
            "nickname": f"猫猫 {index}",
        }
        await plugin.store.enroll(item["id"], identity)
        await plugin.store.answer(
            item["id"],
            {**identity, "group_id": ""},
            {"kind": "mixed", "value": "  private  text\n", "image": filename},
            0,
        )
    with web_request(query="page=2&page_size=10&status=pending"):
        response = await plugin.web_entries(item["id"])
        data = json.loads(response.body)
        assert (
            response.status_code == 200
            and data["page"] == 2
            and len(data["entries"]) == 10
        )
        assert "private  text" not in response.body.decode()
    with web_request(query="q=4444444"):
        data = json.loads((await plugin.web_entries(item["id"])).body)
        assert data["total"] == 1
    with web_request(query="page=wrong"):
        assert (await plugin.web_entries(item["id"])).status_code == 400
    with web_request():
        response = await plugin.web_entry(item["id"], "4444444")
        assert (
            json.loads(response.body)["entry"]["answers"][0]["value"]
            == "  private  text\n"
        )
        assert response.headers["cache-control"] == "no-store"
    with web_request(query="preview=1"):
        response = await plugin.web_image(filename)
        picture = base64.b64decode(
            json.loads(response.body)["preview"].split(",", 1)[1]
        )
        with PillowImage.open(BytesIO(picture)) as image:
            assert image.format == "JPEG" and max(image.size) == 1000
        assert response.headers["cache-control"] == "no-store"


async def test_group_join_starts_private_question_and_navigation_needs_no_activity_id(
    plugin, rules, monkeypatch
):
    rules.update(
        require_correct=False,
        questions=[{"kind": "text", "prompt": f"问题 {index}"} for index in range(3)],
    )
    item = await plugin.store.save(rules, "admin")
    cards = []

    async def card(destination, title, sections, *args, **kwargs):
        cards.append((title, sections, kwargs.get("participation_counts")))

    monkeypatch.setattr(plugin, "card", card)
    await plugin.join(event(plugin), item["id"])
    assert (cards[0][2]["approved"], cards[0][2]["pending"]) == (0, 0)
    assert await plugin.store.private_session("1234567", "4444444") == item["id"]
    deliveries = await plugin.store.deliveries()
    assert (
        len(deliveries) == 1
        and json.loads(deliveries[0]["body"])["target"]["channel"] == "private"
    )
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.main.render_pages",
        lambda *args, **kwargs: [b"image"],
    )
    await plugin.send_deliveries()
    client = plugin.context.get_platform_inst("napcat").client
    assert client.calls[-1][0] == "send_private_msg"
    assert client.calls[-1][1]["user_id"] == 4444444
    await plugin.collect_private(event(plugin, group="", text="  answer  one  "))
    await plugin.lottery_command(event(plugin, group="", text="/抽奖 上一题"))
    assert cards[-1][0] == "第 1 / 3 题"
    assert "/抽奖 上一题" not in str(cards[-1][1]) and "/抽奖 下一题" in str(
        cards[-1][1]
    )
    await plugin.lottery_command(event(plugin, group="", text="/抽奖 下一题"))
    assert cards[-1][0] == "第 2 / 3 题"
    assert "/抽奖 下一题" not in str(cards[-1][1])
    await plugin.lottery_command(event(plugin, group="", text="/抽奖 取消回答"))
    assert await plugin.store.private_session("1234567", "4444444") is None
    await plugin.lottery_command(event(plugin, group="", text="/抽奖 继续"))
    assert cards[-1][0] == "第 2 / 3 题"
    assert "填写 编号" not in str(cards)


async def test_announcement_text_is_a_separate_onebot_send_and_retry_does_not_repeat_images(
    plugin, rules, monkeypatch
):
    item = await plugin.store.save(rules, "admin")
    await plugin.store.action(item["id"], "publish")
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.main.render_announcement",
        lambda *args, **kwargs: [b"image"],
    )
    await plugin.send_deliveries()
    for platform in plugin.context.platform_manager.platform_insts:
        assert len(platform.client.calls) == 1
        assert platform.client.calls[0][1]["message"][0]["type"] == "image"
    client = plugin.context.get_platform_inst("napcat").client
    original = client.call_action
    failed = False

    async def transport(action, **parameters):
        nonlocal failed
        if parameters["message"][0]["type"] == "text" and not failed:
            failed = True
            raise ConnectionError("Text failed")
        return await original(action, **parameters)

    monkeypatch.setattr(client, "call_action", transport)
    await plugin.send_deliveries()
    with web_request({"action": "retry", "confirmed": True}):
        assert (await plugin.web_action(item["id"])).status_code == 200
    await plugin.send_deliveries()
    assert [params["message"][0]["type"] for _, params in client.calls] == [
        "image",
        "text",
    ]
    text = client.calls[-1][1]["message"][0]["data"]["text"]
    assert f"/抽奖 参与 {item['id']}" in text
    assert client.calls[-1][1]["group_id"] == 2222222


async def test_notifications_use_live_counts_and_muted_success_still_reaches_private(
    plugin, rules, monkeypatch
):
    item = await plugin.store.save({**rules, "group_success_notify": False}, "admin")
    for user in ("4444444", "5555555"):
        await plugin.store.enroll(item["id"], plugin.identity(event(plugin, user=user)))
    await plugin.store.action(item["id"], "publish")
    captured = []

    def capture(*args, **kwargs):
        counts = kwargs["participation_counts"]
        captured.append((counts["approved"], counts["pending"]))
        return [b"image"]

    monkeypatch.setattr("astrbot_plugin_catlottery.main.render_pages", capture)
    monkeypatch.setattr("astrbot_plugin_catlottery.main.render_announcement", capture)
    await plugin.send_deliveries()
    assert captured == [(2, 0)] * 4
    calls = plugin.context.get_platform_inst("napcat").client.calls
    assert [action for action, _ in calls] == [
        "send_private_msg",
        "send_private_msg",
        "send_group_msg",
    ]
    await plugin.store.enroll(
        item["id"], plugin.identity(event(plugin, user="6666666"))
    )
    await plugin.send_deliveries()
    text = next(
        parameters["message"][0]["data"]["text"]
        for _, parameters in calls
        if parameters["message"][0]["type"] == "text"
    )
    assert "成功参与 3 人 · 待审核 0 人" in text
    assert captured[-1] == (3, 0)


async def test_self_status_reports_conditional_odds_without_enrolling_or_exposing_others(
    plugin, rules, monkeypatch
):
    rules["prize_tiers"] = [
        {"name": "一等奖", "prize": "玩偶", "count": 1},
        {"name": "二等奖", "prize": "贴纸", "count": 1},
    ]
    item = await plugin.store.save(rules, "admin")
    cards = []

    async def card(*args, **kwargs):
        cards.append(args[2])

    monkeypatch.setattr(plugin, "card", card)
    empty = json.loads(await plugin.tool_status(event(plugin), item["id"]))
    assert empty["pool_win_probability"] is None and empty["status"] == "not_joined"
    assert not (await plugin.store.snapshot())[0]["entry_count"]
    for user in ("4444444", "5555555", "6666666", "7777777"):
        await plugin.store.enroll(item["id"], plugin.identity(event(plugin, user=user)))
    current = json.loads(await plugin.tool_status(event(plugin), item["id"]))
    assert current["user_id"] == "4444444" and current["user_win_probability"] == 0.5
    assert (current["remaining_slots"], current["remaining_candidates"]) == (2, 4)
    assert "50.00%" in str(cards[-1]) and "5555555" not in str(cards[-1])
    drawn = await plugin.store.action(item["id"], "draw_tier", tier_index=0)
    winner = drawn["winners"][0]["user_id"]
    losing_user = next(
        user for user in ("4444444", "5555555", "6666666", "7777777") if user != winner
    )
    winner_state = json.loads(
        await plugin.tool_status(event(plugin, user=winner), item["id"])
    )
    assert winner_state["has_won"] and winner_state["user_win_probability"] is None
    assert "你已获得 一等奖" in str(cards[-1])
    remaining = json.loads(
        await plugin.tool_status(event(plugin, user=losing_user), item["id"])
    )
    assert remaining["user_win_probability"] == pytest.approx(1 / 3)
    outsider = json.loads(
        await plugin.tool_status(event(plugin, user="8888888"), item["id"])
    )
    assert outsider["user_win_probability"] is None and outsider[
        "pool_win_probability"
    ] == pytest.approx(1 / 3)
    assert "尚未获得开奖资格" in str(cards[-1])
    await plugin.lottery_command(event(plugin, text=f"/抽奖 状态 {item['id']}"))
    assert "中奖率" in str(cards[-1]) or "你已获得" in str(cards[-1])
    await plugin.store.action(item["id"], "draw")
    settled = json.loads(
        await plugin.tool_status(event(plugin, user=losing_user), item["id"])
    )
    assert settled["remaining_slots"] == 0 and settled["user_win_probability"] is None


async def test_pending_self_status_does_not_claim_personal_winning_odds(
    plugin, rules, monkeypatch
):
    rules.update(
        require_correct=False, questions=[{"kind": "text", "prompt": "私聊资料"}]
    )
    item = await plugin.store.save(rules, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    await plugin.store.answer(
        item["id"],
        plugin.identity(event(plugin, group="")),
        {"kind": "text", "value": "PRIVATE-ANSWER"},
        0,
    )
    monkeypatch.setattr(plugin, "card", AsyncMock())
    status = json.loads(await plugin.tool_status(event(plugin), item["id"]))
    assert not status["eligible"] and status["pending_review_count"] == 1
    assert status[
        "user_win_probability"
    ] is None and "PRIVATE-ANSWER" not in json.dumps(status)
    await plugin.store.review(
        item["id"],
        {"action": "bulk", "user_ids": ["4444444"], "correct": False},
        "9999999",
    )
    rejected = json.loads(await plugin.tool_status(event(plugin), item["id"]))
    assert rejected["review_status"] == "rejected"
    assert rejected["user_win_probability"] is None
    assert rejected["pending_review_count"] == 0


async def test_self_status_caps_odds_and_cancellation_removes_estimates(
    plugin, rules, monkeypatch
):
    item = await plugin.store.save({**rules, "winner_count": 5}, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    monkeypatch.setattr(plugin, "card", AsyncMock())
    current = json.loads(await plugin.tool_status(event(plugin), item["id"]))
    assert current["remaining_slots"] == 5
    assert current["user_win_probability"] == 1
    assert "100.00%" in str(plugin.card.await_args)
    await plugin.store.action(item["id"], "cancel")
    cancelled = json.loads(await plugin.tool_status(event(plugin), item["id"]))
    assert cancelled["lottery_status"] == "cancelled"
    assert cancelled["user_win_probability"] is None
    assert cancelled["pool_win_probability"] is None
    assert "活动已取消" in str(plugin.card.await_args)


async def test_web_malformed_shapes_and_irreversible_confirmation(plugin, rules):
    item = await plugin.store.save(rules, "admin")
    for payload in (None, [], {"manager_ids": [True]}):
        with web_request(payload):
            assert (await plugin.web_settings()).status_code == 400
    for payload in (
        None,
        [],
        {"action": []},
        {"action": "draw"},
        {"action": "draw", "confirmed": "true"},
    ):
        with web_request(payload):
            assert (await plugin.web_action(item["id"])).status_code == 400
    assert (await plugin.store.get(item["id"]))["status"] == "open"
    with web_request({"action": "draw", "confirmed": True}):
        assert (await plugin.web_action(item["id"])).status_code == 200
    with web_request({"action": "draw", "confirmed": True}):
        assert (await plugin.web_action(item["id"])).status_code == 400


async def test_private_answers_only_in_authenticated_uncached_management(plugin, rules):
    rules["questions"] = [
        {"kind": "quiz", "prompt": "保密答案", "answers": ["secret answer"]}
    ]
    await plugin.store.save(rules, "admin")
    assert "secret answer" not in json.dumps(await plugin.store.snapshot())
    with web_request():
        response = await plugin.web_state()
    assert "secret answer" in response.body.decode()
    assert response.headers["cache-control"] == "no-store"
    with web_request():
        for name in ("../lotteries.sqlite3", "a" * 32 + ".png", "a" * 32 + ".jpg"):
            assert (await plugin.web_image(name)).status_code == 404


async def test_storage_faults_keep_commands_as_images_and_tools_truthful(
    plugin, monkeypatch
):
    async def failed_snapshot(**kwargs):
        raise sqlite3.OperationalError("Storage temporarily unavailable")

    monkeypatch.setattr(plugin.store, "snapshot", failed_snapshot)
    with web_request():
        response = await plugin.web_state()
    assert response.status_code == 503
    assert "OperationalError" not in response.body.decode()
    await plugin.lottery_command(event(plugin, text="抽奖 列表"))
    assert "存储暂时不可用" in await plugin.tool_list(event(plugin))
    calls = plugin.context.get_platform_inst("napcat").client.calls
    assert all(
        kwargs["message"][0]["type"] == "image"
        for name, kwargs in calls
        if name == "send_group_msg"
    )


async def test_explicit_private_answer_preserves_slash_content(plugin, rules):
    rules["questions"] = [{"kind": "text", "prompt": "路径"}]
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.lottery_command(
        event(plugin, group="", text=f"抽奖 填写 {item['id']}")
    )
    await plugin.collect_private(event(plugin, group="", text="/docs/guide"))
    assert (await plugin.store.entry(item["id"], "4444444"))["answers"] == []
    await plugin.lottery_command(event(plugin, group="", text="抽奖 回答 /docs/guide"))
    assert (await plugin.store.entry(item["id"], "4444444"))["answers"][0][
        "value"
    ] == "/docs/guide"


async def test_private_text_can_begin_with_lottery_word(plugin, rules):
    rules["questions"] = [{"kind": "text", "prompt": "参与理由"}]
    item = await plugin.store.save(rules, "admin")
    await plugin.join(event(plugin), item["id"])
    await plugin.store.private_session("1234567", "4444444", item["id"])
    await plugin.collect_private(event(plugin, group="", text="抽奖让我很期待"))
    assert (await plugin.store.entry(item["id"], "4444444"))["status"] == "complete"


async def test_lifecycle_registers_only_owned_routes_and_stops_tasks(plugin, tmp_path):
    context = plugin.context
    context.registered_web_apis = [("/another_plugin/state", None, ["GET"], "External")]
    context.register_web_api = lambda *args: context.registered_web_apis.append(args)
    instance = CatLottery(context)
    instance.store = Store(tmp_path / "lifecycle")
    await instance.initialize()
    assert len(context.registered_web_apis) == 15
    tasks = (instance.worker, instance.sender)
    await instance.terminate()
    assert all(task.done() for task in tasks)
    assert instance.store.db is None
    assert len(context.registered_web_apis) == 1


async def test_artwork_upload_sanitizes_and_previews_via_authenticated_bridge(
    plugin, rules
):
    output = BytesIO()
    source = PillowImage.new("RGBA", (1800, 900), (240, 120, 160, 180))
    exif = PillowImage.Exif()
    exif[274] = 6
    exif[315] = "Private source metadata"
    source.save(output, "PNG", exif=exif)
    with web_request(uploaded=output.getvalue(), username=None):
        assert (await plugin.web_upload_artwork()).status_code == 403
    with web_request(uploaded=output.getvalue()):
        uploaded = await plugin.web_upload_artwork()
    assert uploaded.status_code == 200
    filename = json.loads(uploaded.body)["image"]
    path = plugin.store.directory / "artwork" / filename
    with PillowImage.open(path) as image:
        assert image.format == "JPEG" and image.size == (800, 1600)
        assert not image.getexif()
    with web_request(username=None):
        assert (await plugin.web_artwork(filename)).status_code == 403
    with web_request():
        preview = await plugin.web_artwork(filename)
    assert preview.status_code == 200
    with PillowImage.open(
        BytesIO(base64.b64decode(json.loads(preview.body)["preview"].split(",")[1]))
    ) as image:
        assert image.width <= 720 and image.height <= 480
    rules["cover"] = filename
    rules["prize_tiers"] = [
        {"name": "一等奖", "prize": "猫猫", "count": 1, "image": filename}
    ]
    with web_request(rules):
        response = await plugin.web_save()
    assert response.status_code == 200
    item = json.loads(response.body)
    assert item["cover"] == item["prize_tiers"][0]["image"] == filename
    await plugin.show_item(event(plugin), item)
    await plugin.store.action(item["id"], "publish")
    await plugin.send_deliveries()


@pytest.mark.parametrize(
    "data",
    [b"not an image", b"", b"x" * (8 * 1024 * 1024 + 1)],
    ids=["invalid", "empty", "oversized"],
)
async def test_artwork_upload_rejects_bad_files_without_persistent_garbage(
    plugin, data
):
    with web_request(uploaded=data):
        assert (await plugin.web_upload_artwork()).status_code == 400
    assert not list((plugin.store.directory / "artwork").iterdir())
    for filename in ("../uploads/private.jpg", "missing", "a" * 32 + ".jpg"):
        with web_request():
            assert (await plugin.web_artwork(filename)).status_code == 404


async def test_artwork_storage_failure_is_retryable_and_cleans_partial_file(
    plugin, monkeypatch
):
    output = BytesIO()
    PillowImage.new("RGB", (32, 32), "pink").save(output, "PNG")

    def fail_save(picture, path, *args, **kwargs):
        path.write_bytes(b"partial image")
        raise OSError("Disk is full")

    monkeypatch.setattr(PillowImage.Image, "save", fail_save)
    error_log = Mock()
    monkeypatch.setattr(plugin.logger, "exception", error_log)
    with web_request(uploaded=output.getvalue()):
        response = await plugin.web_upload_artwork()
    assert response.status_code == 503
    assert not list((plugin.store.directory / "artwork").iterdir())
    assert "Disk is full" not in response.body.decode()
    error_log.assert_called_once()
    assert "web_upload_artwork" in str(error_log.call_args)


async def test_web_early_award_requires_confirmation_and_notifies_every_target(
    plugin, rules, monkeypatch
):
    rules["prize_tiers"] = [
        {"name": "一等奖", "prize": "玩偶", "count": 1},
        {"name": "二等奖", "prize": "杯垫", "count": 2},
    ]
    item = await plugin.store.save(rules, "admin")
    await plugin.store.enroll(item["id"], plugin.identity(event(plugin)))
    for username, confirmed, status in (
        (None, True, 403),
        ("operator", False, 400),
        ("operator", True, 200),
    ):
        with web_request(
            {"action": "draw_tier", "tier_index": 0, "confirmed": confirmed},
            username=username,
        ):
            assert (await plugin.web_action(item["id"])).status_code == status
    captured = []

    def capture(title, subtitle, sections, badge, **kwargs):
        if badge == "开奖通知":
            captured.append(sections)
        return [b"image"]

    monkeypatch.setattr("astrbot_plugin_catlottery.main.render_pages", capture)
    await plugin.send_deliveries()
    assert len(captured) == 2
    assert all(
        "一等奖" in str(section)
        and "实际发送者" in str(section)
        and "尚未开奖" in str(section)
        for section in captured
    )
    with web_request({"action": "draw_tier", "tier_index": 0, "confirmed": True}):
        assert (await plugin.web_action(item["id"])).status_code == 400
    assert (await plugin.store.get(item["id"]))["status"] == "open"


def test_real_page_service_rewrites_authenticated_assets_and_modules():
    from pathlib import Path

    page = Path(__file__).resolve().parents[1] / "pages" / "manage"
    service = PluginPageService(SimpleNamespace(), config={})
    args = ("astrbot_plugin_catlottery", "manage")
    params = {"asset_token": "synthetic-asset-token"}
    html = service.rewrite_plugin_page_html(
        (page / "index.html").read_text(encoding="utf-8"),
        *args,
        "index.html",
        theme="light",
        extra_query_params=params,
    )
    css = service.rewrite_plugin_page_css(
        (page / "style.css").read_text(encoding="utf-8"),
        *args,
        "style.css",
        extra_query_params=params,
    )
    js = service.rewrite_plugin_page_js(
        (page / "app.js").read_text(encoding="utf-8"),
        *args,
        "app.js",
        extra_query_params=params,
    )
    assert "cat.png?asset_token=synthetic-asset-token" in html
    assert "bridge-sdk.js?asset_token=synthetic-asset-token" in html
    assert "NotoSansSC.ttf?asset_token=synthetic-asset-token" in css
    assert "components.js?asset_token=synthetic-asset-token" in js
