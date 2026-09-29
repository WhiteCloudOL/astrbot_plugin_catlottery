"""Verify real AstrBot event identity, OneBot routing, permissions, and recovery."""

import asyncio
import json
import sqlite3
import time
from types import SimpleNamespace

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
from astrbot_plugin_catlottery.main import CatLottery
from astrbot_plugin_catlottery.storage import Store
from PIL import Image as PillowImage
from starlette.requests import Request


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
async def plugin(tmp_path):
    clients = [Client(), Client("7654321", ["3333333"])]
    platforms = [Platform("napcat", clients[0]), Platform("snowluma", clients[1])]
    context = SimpleNamespace(
        platform_manager=SimpleNamespace(platform_insts=platforms),
        get_platform_inst=lambda platform_id: next(
            (platform for platform in platforms if platform.meta().id == platform_id),
            None,
        ),
        get_config=lambda: {"admins_id": ["9999999"]},
        registered_web_apis=[],
        register_web_api=lambda *args: None,
    )
    instance = CatLottery(context)
    instance.store = Store(tmp_path)
    await instance.store.open()
    yield instance
    await instance.terminate()


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

    async def capture(current_event, title, sections, subtitle="", badge=""):
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


def web_request(payload=None, username="operator"):
    """Bind real framework request objects without running a public HTTP server.

    Args:
        payload: JSON request body, including malformed shapes under test.
        username: Dashboard identity or None for an unauthenticated caller.

    Returns:
        Context manager exposing the real plugin request proxy.
    """

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps(payload).encode(),
            "more_body": False,
        }

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": [],
        "query_string": b"",
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
        (plugin.web_image, ("a" * 32 + ".jpg",)),
    ):
        with web_request(username=None):
            assert (await handler(*args)).status_code == 403


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
    assert len(context.registered_web_apis) == 8
    tasks = (instance.worker, instance.sender)
    await instance.terminate()
    assert all(task.done() for task in tasks)
    assert instance.store.db is None
    assert len(context.registered_web_apis) == 1


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
