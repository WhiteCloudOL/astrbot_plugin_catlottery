"""Exercise shared identity, cutoff boundaries, transactions, and durable results."""

import asyncio
import json
import time
from copy import deepcopy

import pytest
from astrbot_plugin_catlottery.storage import Store, timestamp, validate_lottery


async def test_settings_switch_preserves_legacy_settings_and_survives_restart(store):
    await store.db.execute(
        "UPDATE settings SET body=? WHERE id=1", ('{"manager_ids":[]}',)
    )
    assert (await store.settings())["llm_tools_enabled"] is True
    await store.settings({"manager_ids": ["9999999"], "llm_tools_enabled": False})
    await store.settings({"manager_ids": ["9999999", "9999999"]})
    await store.close()
    await store.open()
    assert await store.settings() == {
        "manager_ids": ["9999999"],
        "llm_tools_enabled": False,
    }


@pytest.mark.parametrize("enabled", [0, 1, "false", "true", None, [], {}])
async def test_settings_reject_non_boolean_tool_switch(store, enabled):
    with pytest.raises(ValueError, match="LLM"):
        await store.settings({"manager_ids": [], "llm_tools_enabled": enabled})
    assert (await store.settings())["llm_tools_enabled"] is True


@pytest.mark.parametrize("kind", ["quiz", "text", "image", "mixed"])
@pytest.mark.parametrize("action", ["withdraw", "delete"])
async def test_original_text_and_attachments_survive_and_are_removed_together(
    store, rules, sender, kind, action
):
    question = {"kind": kind, "prompt": "提交资料"}
    if kind == "quiz":
        question["answers"] = ["a  b"]
    rules["questions"] = [question]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    text = "  a  b\n "
    filename = "a" * 32 + ".jpg"
    path = store.directory / "uploads" / filename
    path.write_bytes(b"attachment")
    answer = {"kind": kind if kind in {"image", "mixed"} else "text"}
    if kind == "image":
        answer.update(value=filename, text=text)
    else:
        answer.update(value=text, image=filename)
    _, entry = await store.answer(item["id"], {**sender, "group_id": ""}, answer, 0)
    assert entry["status"] == "complete"
    assert entry["answers"] == [answer]
    if action == "withdraw":
        await store.withdraw(item["id"], sender)
    else:
        await store.action(item["id"], "cancel")
        await store.delete(item["id"])
    assert not path.exists()


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
    assert len(await store.deliveries()) == len(rules["targets"])
    with pytest.raises(ValueError, match="结束"):
        await store.action(item["id"], "draw")


async def test_review_mode_advances_wrong_answers_and_requires_every_mark(
    store, rules, sender
):
    rules.update(
        require_correct=False,
        questions=[
            {
                "kind": "quiz",
                "prompt": "选猫",
                "options": ["猫", "狗"],
                "answers": ["猫"],
            },
            {"kind": "mixed", "prompt": "PRIVATE-PROMPT"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    private = {**sender, "group_id": ""}
    await store.private_session(sender["bot_id"], sender["user_id"], item["id"])
    _, entry = await store.answer(
        item["id"], private, {"kind": "text", "value": "B"}, 0
    )
    assert len(entry["answers"]) == 1
    assert entry["answers"][0]["correct"] is None
    _, entry = await store.answer(
        item["id"],
        private,
        {"kind": "mixed", "value": "  PRIVATE-CONTACT\n", "image": "a" * 32 + ".jpg"},
        1,
    )
    assert entry["status"] == "complete" and entry["review_status"] == "pending"
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    summary = (await store.snapshot())[0]
    assert (
        summary["complete_count"],
        summary["submitted_count"],
        summary["review_pending_count"],
    ) == (0, 1, 1)
    result = await store.review(item["id"], {"action": "match"}, "web:admin")
    assert result["marked_answers"] == 1
    assert (await store.entry(item["id"], sender["user_id"]))[
        "review_status"
    ] == "rejected"
    for index in (0, 1):
        await store.review(
            item["id"],
            {
                "action": "mark",
                "user_id": sender["user_id"],
                "question_index": index,
                "correct": True,
            },
            "web:admin",
        )
    entry = await store.entry(item["id"], sender["user_id"])
    assert entry["review_status"] == "approved"
    assert entry["answers"][1]["value"] == "  PRIVATE-CONTACT\n"
    assert all(answer["reviewed_by"] == "web:admin" for answer in entry["answers"])
    deliveries = await store.deliveries()
    assert len(deliveries) == 2
    assert all("PRIVATE-CONTACT" not in row["body"] for row in deliveries)
    assert {json.loads(row["body"])["target"]["channel"] for row in deliveries} == {
        "group",
        "private",
    }
    await store.review(
        item["id"],
        {
            "action": "mark",
            "user_id": sender["user_id"],
            "question_index": 0,
            "correct": None,
        },
        "web:admin",
    )
    assert (await store.entry(item["id"], sender["user_id"]))[
        "review_status"
    ] == "pending"


async def test_later_matching_preserves_manual_marks_spaces_and_images(
    store, rules, sender
):
    rules.update(
        require_correct=False,
        questions=[
            {
                "kind": "quiz",
                "prompt": "选择",
                "options": ["猫", "狗"],
                "answers": ["猫"],
            },
            {"kind": "quiz", "prompt": "两个空格", "answers": ["a  b"]},
            {"kind": "quiz", "prompt": "需要人工核对图文", "answers": ["yes"]},
            {"kind": "quiz", "prompt": "没有文字参考答案"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    private = {**sender, "group_id": ""}
    for index, answer in enumerate(
        [
            {"kind": "text", "value": "  １\n"},
            {"kind": "text", "value": "  Ａ  Ｂ\n"},
            {"kind": "text", "value": "yes", "image": "b" * 32 + ".jpg"},
            {"kind": "text", "value": "自定义文字"},
        ]
    ):
        await store.answer(item["id"], private, answer, index)
    await store.review(
        item["id"],
        {
            "action": "mark",
            "user_id": sender["user_id"],
            "question_index": 0,
            "correct": False,
        },
        "web:human",
    )
    result = await store.review(item["id"], {"action": "match"}, "web:matcher")
    assert result["marked_answers"] == 1
    entry = await store.entry(item["id"], sender["user_id"])
    assert [answer["correct"] for answer in entry["answers"]] == [
        False,
        True,
        None,
        None,
    ]
    assert entry["answers"][0]["reviewed_by"] == "web:human"
    assert (await store.review(item["id"], {"action": "match"}, "web:matcher"))[
        "marked_answers"
    ] == 0


async def test_draw_uses_only_approved_entries_and_freezes_review(store, rules, sender):
    rules.update(require_correct=False, questions=[{"kind": "text", "prompt": "资料"}])
    item = await store.save(rules, "admin")
    for user_id in ("4444444", "5555555", "6666666", "7777777"):
        identity = {**sender, "user_id": user_id}
        await store.enroll(item["id"], identity)
        if user_id != "7777777":
            await store.answer(
                item["id"],
                {**identity, "group_id": ""},
                {"kind": "text", "value": "answer"},
                0,
            )
    await store.action(item["id"], "close")
    for user_id, correct in (("4444444", True), ("5555555", False)):
        await store.review(
            item["id"],
            {
                "action": "mark",
                "user_id": user_id,
                "question_index": 0,
                "correct": correct,
            },
            "web:admin",
        )
    summary = (await store.snapshot())[0]
    assert (
        summary["complete_count"],
        summary["submitted_count"],
        summary["rejected_count"],
        summary["review_pending_count"],
    ) == (1, 3, 1, 1)
    drawn = await store.action(item["id"], "draw")
    assert drawn["eligible_count"] == 1
    assert [winner["user_id"] for winner in drawn["winners"]] == ["4444444"]
    with pytest.raises(ValueError, match="锁定"):
        await store.review(item["id"], {"action": "match"}, "web:admin")


async def test_unapproved_pool_produces_final_empty_result_and_group_notices(
    store, rules, sender
):
    rules.update(require_correct=False, questions=[{"kind": "text", "prompt": "资料"}])
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "waiting"}, 0
    )
    drawn = await store.action(item["id"], "draw")
    assert (
        drawn["status"] == "drawn"
        and drawn["eligible_count"] == 0
        and drawn["winners"] == []
    )
    notices = [
        json.loads(row["body"])
        for row in await store.deliveries()
        if json.loads(row["body"])["kind"] == "result"
    ]
    assert {notice["target"]["recipient"] for notice in notices} == {
        "2222222",
        "3333333",
    }
    await store.close()
    await store.open()
    assert (await store.get(item["id"]))["winners"] == []
    with pytest.raises(ValueError):
        await store.action(item["id"], "draw")


async def test_review_rejects_exact_draw_deadline_and_mode_changes(
    store, rules, sender, monkeypatch
):
    rules.update(
        require_correct=False, questions=[{"kind": "quiz", "prompt": "人工题"}]
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "original"}, 0
    )
    with pytest.raises(ValueError, match="已有报名"):
        await store.save(
            {
                **rules,
                "require_correct": True,
                "questions": [
                    {"kind": "quiz", "prompt": "人工题", "answers": ["original"]}
                ],
            },
            "admin",
            item["id"],
        )
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.storage.time.time", lambda: item["draw_at"]
    )
    with pytest.raises(ValueError, match="锁定"):
        await store.review(
            item["id"],
            {
                "action": "mark",
                "user_id": sender["user_id"],
                "question_index": 0,
                "correct": True,
            },
            "web:admin",
        )
    assert (await store.entry(item["id"], sender["user_id"]))[
        "review_status"
    ] == "pending"


async def test_review_and_draw_are_serialized(store, rules, sender):
    rules.update(
        require_correct=False, questions=[{"kind": "quiz", "prompt": "人工题"}]
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "answer"}, 0
    )
    results = await asyncio.gather(
        store.action(item["id"], "draw"),
        store.review(
            item["id"],
            {
                "action": "mark",
                "user_id": sender["user_id"],
                "question_index": 0,
                "correct": True,
            },
            "web:admin",
        ),
        return_exceptions=True,
    )
    saved = await store.get(item["id"])
    assert saved["eligible_count"] in (0, 1)
    if isinstance(results[1], ValueError):
        assert saved["eligible_count"] == 0
        assert (await store.entry(item["id"], sender["user_id"]))[
            "review_status"
        ] == "pending"
    else:
        assert saved["eligible_count"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"action": []},
        {
            "action": "mark",
            "user_id": "4444444",
            "question_index": True,
            "correct": True,
        },
        {
            "action": "mark",
            "user_id": "4444444",
            "question_index": 0,
            "correct": "true",
        },
        {"action": "mark", "user_id": "4444444", "question_index": 0},
        {"action": "mark", "user_id": "4444444", "question_index": 99, "correct": True},
    ],
)
async def test_review_rejects_malformed_marks_without_changing_entries(
    store, rules, sender, payload
):
    rules.update(require_correct=False, questions=[{"kind": "text", "prompt": "题目"}])
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "answer"}, 0
    )
    with pytest.raises(ValueError):
        await store.review(item["id"], payload, "web:admin")
    assert (await store.entry(item["id"], sender["user_id"]))[
        "review_status"
    ] == "pending"


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


@pytest.mark.parametrize("require_correct", [None, "false", 0, 1, [], {}])
def test_answer_mode_requires_boolean(rules, require_correct):
    with pytest.raises(ValueError, match="答题模式"):
        validate_lottery({**rules, "require_correct": require_correct})


async def test_legacy_completed_entries_keep_eligibility_and_instant_rules(
    store, rules, sender
):
    item = await store.save(rules, "admin")
    entry, _ = await store.enroll(item["id"], sender)
    item.pop("require_correct")
    entry.pop("review_status")
    await store.db.execute(
        "UPDATE lotteries SET body=? WHERE id=?", (json.dumps(item), item["id"])
    )
    await store.db.execute(
        "UPDATE entries SET body=? WHERE lottery_id=?", (json.dumps(entry), item["id"])
    )
    summary = (await store.snapshot())[0]
    assert summary["require_correct"] is True
    assert (
        summary["complete_count"] == 1
        and summary["review_pending_count"] == 0
        and summary["rejected_count"] == 0
    )
    with pytest.raises(ValueError, match="当场答对"):
        await store.review(item["id"], {"action": "match"}, "web:admin")
    drawn = await store.action(item["id"], "draw")
    assert drawn["eligible_count"] == 1
