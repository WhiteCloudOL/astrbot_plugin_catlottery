"""Exercise shared identity, cutoff boundaries, transactions, and durable results."""

import asyncio
import json
import time
from copy import deepcopy

import pytest
from astrbot_plugin_catlottery.storage import Store, timestamp, validate_lottery


@pytest.mark.parametrize("kind", [None, [], {}, True])
def test_question_kind_wrong_json_types_are_validation_errors(rules, kind):
    rules["questions"] = [{"kind": kind, "prompt": "题目"}]
    with pytest.raises(ValueError, match="问题类型"):
        validate_lottery(rules)


@pytest.fixture
async def store(tmp_path):
    instance = Store(tmp_path)
    await instance.open()
    yield instance
    await instance.close()


@pytest.fixture
def rules():
    return {
        "title": "猫猫的好运",
        "prize": "贴纸",
        "winner_count": 2,
        "close_at": time.time() + 3600,
        "draw_at": time.time() + 7200,
        "targets": [
            {"platform_id": "napcat", "bot_id": "1234567", "group_id": "2222222"},
            {"platform_id": "snowluma", "bot_id": "7654321", "group_id": "3333333"},
        ],
        "questions": [],
    }


@pytest.fixture
def sender():
    return {
        "platform_id": "napcat",
        "bot_id": "1234567",
        "group_id": "2222222",
        "user_id": "4444444",
        "nickname": "小猫",
    }


async def test_cross_platform_and_group_duplicate_counts_once(store, rules, sender):
    item = await store.save(rules, "admin")
    other = {
        **sender,
        "platform_id": "snowluma",
        "bot_id": "7654321",
        "group_id": "3333333",
    }
    results = await asyncio.gather(
        store.enroll(item["id"], sender), store.enroll(item["id"], other)
    )
    assert sorted(created for entry, created in results) == [False, True]
    assert (await store.snapshot())[0]["complete_count"] == 1
    assert len(await store.deliveries()) == 2


async def test_edit_preserves_enrollment_and_outbox(store, rules, sender):
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    rules["description"] = "更新领取说明"
    await store.save(rules, "admin", item["id"])
    assert await store.entry(item["id"], sender["user_id"])
    assert len(await store.deliveries()) == 2
    rules["winner_count"] = 3
    with pytest.raises(ValueError, match="已有报名"):
        await store.save(rules, "admin", item["id"])


async def test_random_identifier_collision_never_overwrites_an_activity(
    store, rules, sender, monkeypatch
):
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    identifiers = iter([item["id"], "abcdef01"])
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.storage.secrets.token_hex",
        lambda length: next(identifiers),
    )
    other = await store.save({**rules, "title": "另一场活动"}, "admin")
    assert other["id"] != item["id"]
    assert (await store.get(item["id"]))["title"] == rules["title"]
    assert await store.entry(item["id"], sender["user_id"])


async def test_private_and_unlisted_targets_cannot_enroll(store, rules, sender):
    item = await store.save(rules, "admin")
    for invalid in (
        {**sender, "group_id": ""},
        {**sender, "platform_id": "other"},
        {**sender, "bot_id": "6666666"},
        {**sender, "group_id": "7777777"},
    ):
        with pytest.raises(ValueError, match="允许"):
            await store.enroll(item["id"], invalid)
    assert (await store.snapshot())[0]["entry_count"] == 0


async def test_private_form_requires_original_sender_robot_and_platform(
    store, rules, sender
):
    rules["questions"] = [{"kind": "text", "prompt": "请填写昵称"}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    with pytest.raises(ValueError, match="群报名"):
        await store.private_session(sender["bot_id"], "5555555", item["id"])
    private = {**sender, "group_id": ""}
    for invalid in (
        sender,
        {**private, "user_id": "5555555"},
        {**private, "bot_id": "7654321"},
        {**private, "platform_id": "snowluma"},
    ):
        with pytest.raises(ValueError, match="QQ 号"):
            await store.answer(
                item["id"], invalid, {"kind": "text", "value": "猫猫"}, 0
            )
    assert (await store.entry(item["id"], sender["user_id"]))["status"] == "pending"


async def test_multiple_quiz_text_image_questions_and_notification_privacy(
    store, rules, sender
):
    rules["questions"] = [
        {
            "kind": "quiz",
            "prompt": "选蓝色",
            "options": ["蓝色", "粉色"],
            "answers": ["蓝色"],
        },
        {"kind": "text", "prompt": "填写资料"},
        {"kind": "image", "prompt": "发送图片"},
    ]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    private = {**sender, "group_id": ""}
    await store.private_session(sender["bot_id"], sender["user_id"], item["id"])
    with pytest.raises(ValueError, match="不正确"):
        await store.answer(item["id"], private, {"kind": "text", "value": "B"}, 0)
    await store.answer(item["id"], private, {"kind": "text", "value": "A"}, 0)
    with pytest.raises(ValueError, match="已处理"):
        await store.answer(item["id"], private, {"kind": "text", "value": "A"}, 0)
    await store.answer(
        item["id"], private, {"kind": "text", "value": "PRIVATE-CONTACT"}, 1
    )
    with pytest.raises(ValueError, match="图片记录"):
        await store.answer(
            item["id"], private, {"kind": "image", "value": "../../secret.jpg"}, 2
        )
    item, entry = await store.answer(
        item["id"], private, {"kind": "image", "value": "a" * 32 + ".jpg"}, 2
    )
    assert entry["status"] == "complete"
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    deliveries = await store.deliveries()
    assert {json.loads(row["body"])["target"]["channel"] for row in deliveries} == {
        "group",
        "private",
    }
    assert all("PRIVATE-CONTACT" not in row["body"] for row in deliveries)
    assert "answers" not in (await store.snapshot())[0]["questions"][0]


async def test_cutoff_exact_boundary_blocks_join_and_private_answer(
    store, rules, sender, monkeypatch
):
    rules["questions"] = [{"kind": "text", "prompt": "填资料"}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.storage.time.time", lambda: item["close_at"]
    )
    with pytest.raises(ValueError, match="截止"):
        await store.enroll(item["id"], {**sender, "user_id": "5555555"})
    with pytest.raises(ValueError, match="截止"):
        await store.answer(
            item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "ok"}, 0
        )
    with pytest.raises(ValueError, match="截止"):
        await store.withdraw(item["id"], sender)
    assert (await store.snapshot())[0]["phase"] == "closed"


async def test_atomic_draw_excludes_pending_and_cannot_reroll(store, rules, sender):
    rules["questions"] = [{"kind": "text", "prompt": "填资料"}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.enroll(item["id"], {**sender, "user_id": "5555555"})
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "ok"}, 0
    )
    results = await asyncio.gather(
        store.action(item["id"], "draw"),
        store.action(item["id"], "draw"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    saved = await store.get(item["id"])
    assert saved["eligible_count"] == 1
    assert saved["winners"] == [
        {"user_id": sender["user_id"], "nickname": sender["nickname"]}
    ]
    notices = [
        json.loads(row["body"])
        for row in await store.deliveries()
        if json.loads(row["body"])["kind"] == "result"
    ]
    assert len(notices) == 2
    assert {notice["target"]["recipient"] for notice in notices} == {
        "2222222",
        "3333333",
    }


async def test_persisted_winners_and_failed_deliveries_survive_restart(
    store, rules, sender
):
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    drawn = await store.action(item["id"], "draw")
    rows = await store.deliveries()
    await store.delivery_done(rows[0]["id"], "ConnectionError")
    await store.delivery_done(rows[1]["id"])
    directory = store.directory
    await store.close()
    await store.open()
    assert (await store.get(item["id"]))["winners"] == drawn["winners"]
    details = await store.manage_entries(item["id"])
    assert any(
        row["attempts"] == 1 and row["next_at"] > time.time()
        for row in details["deliveries"]
    )
    assert any(row["delivered_at"] for row in details["deliveries"])
    assert store.directory == directory


async def test_empty_pool_draw_is_final_and_scheduled_draw_not_early(store, rules):
    item = await store.save(rules, "admin")
    with pytest.raises(ValueError, match="尚未"):
        await store.action(item["id"], "draw", due_only=True)
    drawn = await store.action(item["id"], "draw")
    assert drawn["winners"] == []
    assert drawn["eligible_count"] == 0
    assert drawn["status"] == "drawn"


async def test_withdraw_and_delete_remove_private_files(store, rules, sender):
    filename = "b" * 32 + ".jpg"
    rules["questions"] = [{"kind": "image", "prompt": "发图"}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "image", "value": filename}, 0
    )
    image = store.directory / "uploads" / filename
    image.write_bytes(b"test")
    await store.withdraw(item["id"], sender)
    assert not image.exists()
    assert await store.entry(item["id"], sender["user_id"]) is None
    assert not await store.deliveries()
    with pytest.raises(ValueError, match="先取消"):
        await store.delete(item["id"])
    await store.action(item["id"], "cancel")
    await store.delete(item["id"])
    assert await store.snapshot() == []


@pytest.mark.parametrize(
    "change",
    [
        {"winner_count": True},
        {"winner_count": 1.5},
        {"winner_count": 0},
        {"winner_count": 101},
        {"targets": []},
        {"title": ""},
        {"draw_at": 1},
        {"close_at": float("nan")},
        {"questions": [{"kind": "quiz", "prompt": "题", "answers": []}]},
        {
            "questions": [
                {"kind": "quiz", "prompt": "题", "options": ["猫"], "answers": ["狗"]}
            ]
        },
        {"questions": [{"kind": "image", "prompt": "图", "options": ["猫"]}]},
    ],
)
def test_invalid_rule_boundaries_rejected(rules, change):
    with pytest.raises(ValueError):
        validate_lottery({**deepcopy(rules), **change})


def test_naive_and_explicit_dates_share_utc8():
    assert timestamp("2026-10-01 20:00") == timestamp("2026-10-01T20:00:00+08:00")
    assert timestamp("2026-10-01T12:00:00Z") == timestamp("2026-10-01 20:00")
