"""Exercise shared identity, cutoff boundaries, transactions, and durable results."""

import asyncio
import json
import math
import os
import time
from copy import deepcopy

import pytest
from astrbot_plugin_catlottery.storage import (
    Store,
    answer_images,
    timestamp,
    validate_answer,
    validate_lottery,
)


@pytest.mark.parametrize(
    "images",
    [None, "image.jpg", ["../../secret.jpg"], [True], ["f" * 32 + ".jpg"] * 10],
)
def test_multiple_attachment_validation_rejects_invalid_references(images):
    with pytest.raises(ValueError):
        validate_answer(
            {"kind": "text"},
            {"kind": "text", "value": "answer", "images": images},
            False,
        )


def test_multi_attachment_normalization_preserves_legacy_and_order():
    first, second = "1" * 32 + ".jpg", "2" * 32 + ".jpg"
    answer = validate_answer(
        {"kind": "image"},
        {
            "kind": "image",
            "value": first,
            "images": [second, first, second],
            "text": "  exact  ",
        },
        False,
    )
    assert answer_images(answer) == [first, second]
    assert answer["text"] == "  exact  " and answer["correct"] is None
    assert answer_images({"kind": "text", "image": first}) == [first]


@pytest.mark.parametrize(
    "action", ["edit_answer", "delete_answer", "delete_entries", "withdraw", "delete"]
)
async def test_multi_image_management_removes_only_unreferenced_files(
    store, rules, sender, action
):
    rules.update(require_correct=False, questions=[{"kind": "mixed", "prompt": "图片"}])
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    filenames = [str(number) * 32 + ".jpg" for number in (1, 2, 3)]
    for filename in filenames:
        (store.directory / "uploads" / filename).write_bytes(b"private attachment")
    await store.answer(
        item["id"],
        {**sender, "group_id": ""},
        {
            "kind": "mixed",
            "value": "private",
            "image": filenames[0],
            "images": filenames[:2],
        },
        0,
    )
    other = await store.save({**rules, "title": "another activity"}, "admin")
    await store.enroll(other["id"], sender)
    await store.answer(
        other["id"],
        {**sender, "group_id": ""},
        {"kind": "mixed", "value": "shared legacy record", "image": filenames[1]},
        0,
    )
    entry = await store.entry(item["id"], sender["user_id"])
    if action == "withdraw":
        await store.withdraw(item["id"], sender)
    elif action == "delete":
        await store.action(item["id"], "cancel")
        await store.delete(item["id"])
    else:
        payload = {
            "action": action,
            "user_id": sender["user_id"],
            "user_ids": [sender["user_id"]],
            "question_index": 0,
            "revision": entry["answer_revision"],
            "revisions": {sender["user_id"]: entry["answer_revision"]},
            "confirmed": True,
            "notify": False,
        }
        if action == "edit_answer":
            payload["answer"] = {
                "kind": "mixed",
                "value": "changed",
                "images": [filenames[2], filenames[1]],
            }
        await store.manage_answers(item["id"], payload, "admin")
    assert not (store.directory / "uploads" / filenames[0]).exists()
    assert (store.directory / "uploads" / filenames[1]).exists()
    if action == "edit_answer":
        saved = await store.entry(item["id"], sender["user_id"])
        assert answer_images(saved["answers"][0]) == [filenames[2], filenames[1]]
        assert saved["review_status"] == "pending"


async def test_admin_cannot_bind_another_answers_secondary_image(
    store, sender, managed_form
):
    item, entry = managed_form
    second = "2" * 32 + ".jpg"
    (store.directory / "uploads" / second).write_bytes(b"secondary")
    await store.manage_answers(
        item["id"],
        {
            "action": "edit_answer",
            "user_id": sender["user_id"],
            "question_index": 1,
            "revision": entry["answer_revision"],
            "notify": False,
            "answer": {**entry["answers"][1], "images": [second]},
        },
        "admin",
    )
    entry = await store.entry(item["id"], sender["user_id"])
    with pytest.raises(ValueError, match="其他答案"):
        await store.manage_answers(
            item["id"],
            {
                "action": "edit_answer",
                "user_id": sender["user_id"],
                "question_index": 0,
                "revision": entry["answer_revision"],
                "notify": False,
                "answer": {"kind": "text", "value": "stolen image", "images": [second]},
            },
            "admin",
        )


async def test_approved_group_repeat_after_cutoff_preserves_qualification(
    store, rules, sender, monkeypatch
):
    item = await store.save(rules, "admin")
    entry, _ = await store.enroll(item["id"], sender)
    monkeypatch.setattr(time, "time", lambda: item["close_at"])
    repeated, created = await store.enroll(item["id"], sender, restart_existing=True)
    assert repeated == entry and not created
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None


async def test_answer_mode_expires_once_and_survives_reload(
    store, rules, sender, monkeypatch
):
    start = time.time()
    monkeypatch.setattr(time, "time", lambda: start)
    rules.update(
        require_correct=False,
        questions=[
            {"kind": "text", "prompt": "第一题"},
            {"kind": "text", "prompt": "第二题"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    deadline = (await store.entry(item["id"], sender["user_id"]))["mode_expires_at"]
    assert deadline == start + 1800
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "保留答案"}, 0
    )
    await store.queue_question(item["id"], sender["bot_id"], sender["user_id"])
    await store.close()
    await store.open()
    assert (await store.entry(item["id"], sender["user_id"]))[
        "mode_expires_at"
    ] == deadline
    assert await store.expire_forms(now=math.nextafter(deadline, -math.inf)) == 0
    assert await store.expire_forms(now=deadline) == 1
    assert await store.expire_forms(now=deadline) == 0
    entry = await store.entry(item["id"], sender["user_id"])
    assert entry["answers"][0]["value"] == "保留答案"
    notices = [json.loads(row["body"]) for row in await store.deliveries()]
    assert len(notices) == 1 and notices[0]["kind"] == "form_timeout"
    assert notices[0]["target"]["channel"] == "private"
    assert "保留答案" not in json.dumps(notices, ensure_ascii=False)
    monkeypatch.setattr(time, "time", lambda: deadline + 1)
    await store.private_session(sender["bot_id"], sender["user_id"], item["id"])
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == 1
    assert (await store.entry(item["id"], sender["user_id"]))[
        "mode_expires_at"
    ] == deadline + 1801
    assert not await store.deliveries()


@pytest.mark.parametrize("state", ["incomplete", "pending", "rejected", "approved"])
async def test_group_reentry_restarts_only_unsuccessful_forms(
    store, rules, sender, state
):
    rules.update(
        require_correct=False,
        questions=[
            {"kind": "text", "prompt": "第一题"},
            {"kind": "text", "prompt": "第二题"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    private = {**sender, "group_id": ""}
    await store.answer(item["id"], private, {"kind": "text", "value": "第一份答案"}, 0)
    if state != "incomplete":
        await store.answer(
            item["id"], private, {"kind": "text", "value": "第二份答案"}, 1
        )
    if state in {"rejected", "approved"}:
        await store.review(
            item["id"],
            {
                "action": "decision",
                "user_id": sender["user_id"],
                "correct": state == "approved",
            },
            "admin",
        )
    before = await store.entry(item["id"], sender["user_id"])
    entry, created = await store.enroll(item["id"], sender, restart_existing=True)
    assert created is False
    if state == "approved":
        assert entry["answers"] == before["answers"]
        assert entry["answer_revision"] == before["answer_revision"]
        assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    else:
        assert entry["answers"] == [] and entry["status"] == "pending"
        assert "review_decision" not in entry
        assert entry["answer_revision"] == before["answer_revision"] + 1
        assert await store.form_position(sender["bot_id"], sender["user_id"]) == 0
        assert not await store.deliveries()


async def test_expired_or_replaced_mode_cannot_accept_inflight_answer(
    store, rules, sender, monkeypatch
):
    start = time.time()
    monkeypatch.setattr(time, "time", lambda: start)
    rules["questions"] = [{"kind": "text", "prompt": "资料"}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    deadline = start + 1800
    monkeypatch.setattr(time, "time", lambda: start + 1)
    await store.enroll(item["id"], sender, restart_existing=True)
    with pytest.raises(ValueError, match="重新开启"):
        await store.answer(
            item["id"],
            {**sender, "group_id": ""},
            {"kind": "text", "value": "旧模式答案"},
            0,
            mode_expires_at=deadline,
        )
    monkeypatch.setattr(time, "time", lambda: deadline + 1)
    with pytest.raises(ValueError, match="已退出"):
        await store.answer(
            item["id"],
            {**sender, "group_id": ""},
            {"kind": "text", "value": "过期答案"},
            0,
        )
    assert not (await store.entry(item["id"], sender["user_id"]))["answers"]


@pytest.mark.parametrize("mode", ["once", "repeat"])
async def test_fractional_announcement_deadline_uses_exact_saved_time(
    store, rules, monkeypatch, mode
):
    # SQLite 3.40 on Windows rounds this JSON number upward by one float step.
    start = 1790699748.8209887
    monkeypatch.setattr(time, "time", lambda: start - 60)
    rules.update(close_at=start + 3600, draw_at=start + 7200)
    item = await store.save(
        {
            **rules,
            "announcement_schedule": {
                "mode": mode,
                "start_at": start,
                "interval_minutes": 1,
            },
        },
        "admin",
    )
    assert (
        await store.schedule_announcements(now=math.nextafter(start, -math.inf)) == []
    )
    assert await store.schedule_announcements(now=start) == [item["id"]]
    saved = await store.get(item["id"])
    assert saved["announcement_last_at"] == start
    assert saved["announcement_next_at"] == (start + 60 if mode == "repeat" else None)
    assert await store.schedule_announcements(now=start) == []


async def test_fractional_private_session_cutoff_is_exact(
    store, rules, sender, monkeypatch
):
    cutoff = 1790699748.8209887
    monkeypatch.setattr(time, "time", lambda: cutoff - 60)
    rules.update(
        close_at=cutoff,
        draw_at=cutoff + 3600,
        questions=[{"kind": "text", "prompt": "资料"}],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    monkeypatch.setattr(time, "time", lambda: math.nextafter(cutoff, -math.inf))
    assert (
        await store.private_session(sender["bot_id"], sender["user_id"]) == item["id"]
    )
    monkeypatch.setattr(time, "time", lambda: cutoff)
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None


async def test_expired_session_releases_sender_for_another_lottery(
    store, rules, sender, monkeypatch
):
    rules["questions"] = [{"kind": "text", "prompt": "资料"}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.storage.time.time", lambda: item["close_at"]
    )
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None


async def test_closed_activity_allows_notice_edits_but_due_draw_cannot_be_postponed(
    store, rules, monkeypatch
):
    item = await store.save(rules, "admin")
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.storage.time.time", lambda: item["close_at"] + 1
    )
    saved = await store.save(
        {**item, "group_success_notify": False}, "admin", item["id"]
    )
    assert saved["close_at"] == item["close_at"]
    assert not saved["group_success_notify"]
    with pytest.raises(ValueError, match="开奖时间"):
        await store.save({**item, "draw_at": item["close_at"]}, "admin", item["id"])
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.storage.time.time", lambda: item["draw_at"]
    )
    with pytest.raises(ValueError, match="开奖时间"):
        await store.save(
            {
                **item,
                "close_at": item["draw_at"] + 100,
                "draw_at": item["draw_at"] + 200,
            },
            "admin",
            item["id"],
        )


async def test_changed_targets_revoke_queued_announcements(store, rules):
    item = await store.save(rules, "admin")
    await store.action(item["id"], "publish")
    rules["targets"] = [{**rules["targets"][0], "group_id": "8888888"}]
    await store.save(rules, "admin", item["id"])
    assert not await store.deliveries()


async def test_nondecimal_quiz_symbol_does_not_abort_bulk_matching(
    store, rules, sender
):
    rules.update(
        require_correct=False,
        questions=[
            {
                "kind": "quiz",
                "prompt": "选项",
                "options": ["猫", "狗"],
                "answers": ["猫"],
            }
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "❶"}, 0
    )
    result = await store.review(item["id"], {"action": "match"}, "admin")
    assert result["marked_answers"] == 1
    assert (await store.entry(item["id"], sender["user_id"]))[
        "review_status"
    ] == "rejected"


def test_equivalent_choice_options_are_rejected(rules):
    rules["questions"] = [
        {
            "kind": "quiz",
            "prompt": "重复选项",
            "options": ["Cat", "ＣＡＴ"],
            "answers": ["Cat"],
        }
    ]
    with pytest.raises(ValueError, match="重复"):
        validate_lottery(rules)


async def test_private_orphan_sweep_preserves_current_attachments(store, rules, sender):
    rules["questions"] = [{"kind": "image", "prompt": "图片"}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    retained = "a" * 32 + ".jpg"
    orphan = "b" * 32 + ".jpg"
    for filename in (retained, orphan):
        path = store.directory / "uploads" / filename
        path.write_bytes(b"image")
        os.utime(path, (time.time() - 90000,) * 2)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "image", "value": retained}, 0
    )
    assert await store.cleanup_artwork() == 1
    assert (store.directory / "uploads" / retained).is_file()
    assert not (store.directory / "uploads" / orphan).exists()


@pytest.fixture
def tier_rules(rules):
    rules["prize_tiers"] = [
        {"name": "一等奖", "prize": "猫猫玩偶", "count": 1},
        {"name": "二等奖", "prize": "猫猫杯垫", "count": 2},
        {"name": "三等奖", "prize": "猫猫贴纸", "count": 3},
    ]
    return rules


async def test_success_notice_switch_is_per_activity_and_clears_only_unsent_group_success(
    store, rules, sender
):
    first = await store.save(rules, "admin")
    second = await store.save({**rules, "title": "另一场"}, "admin")
    await store.enroll(first["id"], sender)
    await store.enroll(second["id"], sender)
    await store.save({**rules, "group_success_notify": False}, "admin", first["id"])
    await store.close()
    await store.open()
    assert (await store.get(first["id"]))["group_success_notify"] is False
    assert (await store.get(second["id"]))["group_success_notify"] is True
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert [
        message["target"]["channel"]
        for message in messages
        if message["item"]["id"] == first["id"]
    ] == ["private"]
    assert {
        message["target"]["channel"]
        for message in messages
        if message["item"]["id"] == second["id"]
    } == {"group", "private"}
    await store.enroll(first["id"], {**sender, "user_id": "5555555"})
    live = await store.participation_state(first["id"], sender["user_id"])
    assert (live["approved"], live["pending"], live["entry"]["user_id"]) == (
        2,
        0,
        sender["user_id"],
    )


async def test_muted_review_success_preserves_pending_notices_and_live_counts(
    store, rules, sender
):
    rules.update(
        group_success_notify=False,
        require_correct=False,
        questions=[{"kind": "text", "prompt": "资料"}],
    )
    item = await store.save(rules, "admin")
    for index in range(4):
        identity = {**sender, "user_id": str(4444444 + index)}
        await store.enroll(item["id"], identity)
        if index < 3:
            await store.answer(
                item["id"],
                {**identity, "group_id": ""},
                {"kind": "text", "value": f"private-{index}"},
                0,
            )
    assert (await store.participation_state(item["id"]))["pending"] == 3
    assert {
        json.loads(row["body"])["target"]["channel"] for row in await store.deliveries()
    } == {"group", "private"}
    for index, correct in ((0, True), (1, False)):
        await store.review(
            item["id"],
            {
                "action": "mark",
                "user_id": str(4444444 + index),
                "question_index": 0,
                "correct": correct,
            },
            "web:admin",
        )
    live = await store.participation_state(item["id"])
    assert live["entry"] is None and (live["approved"], live["pending"]) == (1, 1)
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    approved = [
        message
        for message in messages
        if message.get("entry", {}).get("user_id") == sender["user_id"]
    ]
    assert len(approved) == 1 and approved[0]["target"]["channel"] == "private"
    assert all("private-" not in json.dumps(message) for message in messages)


@pytest.mark.parametrize("value", [None, "false", 0, 1, [], {}])
@pytest.mark.parametrize("key", ["group_success_notify", "group_pending_notify"])
def test_success_notice_switch_requires_boolean(rules, value, key):
    with pytest.raises(ValueError, match="通知开关"):
        validate_lottery({**rules, key: value})


async def test_pending_notice_switch_mutes_only_group_pending_and_survives_restart(
    store,
    rules,
    sender,
):
    rules.update(require_correct=False, questions=[{"kind": "text", "prompt": "资料"}])
    item = await store.save(rules, "admin")
    other = await store.save(rules, "admin")
    for activity in (item, other):
        await store.enroll(activity["id"], sender)
        await store.answer(
            activity["id"],
            {**sender, "group_id": ""},
            {"kind": "text", "value": "saved"},
            0,
        )
    await store.save({**rules, "group_pending_notify": False}, "admin", item["id"])
    await store.close()
    await store.open()
    assert (await store.get(item["id"]))["group_pending_notify"] is False
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert [
        m["target"]["channel"] for m in messages if m["item"]["id"] == item["id"]
    ] == ["private"]
    assert {
        m["target"]["channel"] for m in messages if m["item"]["id"] == other["id"]
    } == {"group", "private"}
    await store.review(
        item["id"],
        {
            "action": "mark",
            "user_id": sender["user_id"],
            "question_index": 0,
            "correct": True,
        },
        "admin",
    )
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert {
        m["target"]["channel"]
        for m in messages
        if m["item"]["id"] == item["id"] and m["kind"] == "review"
    } == {"group", "private"}
    await store.review(
        item["id"],
        {
            "action": "mark",
            "user_id": sender["user_id"],
            "question_index": 0,
            "correct": None,
        },
        "admin",
    )
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert [
        m["target"]["channel"]
        for m in messages
        if m["item"]["id"] == item["id"]
        and m.get("entry", {}).get("review_status") == "pending"
    ] == ["private"]


@pytest.mark.parametrize("success_notify", [False, True])
@pytest.mark.parametrize("pending_notify", [False, True])
async def test_rejected_review_is_private_for_every_group_notification_policy(
    store, rules, sender, success_notify, pending_notify
):
    rules.update(
        require_correct=False,
        group_success_notify=success_notify,
        group_pending_notify=pending_notify,
        questions=[{"kind": "text", "prompt": "private question"}],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"],
        {**sender, "group_id": ""},
        {"kind": "text", "value": "private answer"},
        0,
    )
    await store.review(
        item["id"],
        {"action": "decision", "user_id": sender["user_id"], "correct": False},
        "admin",
    )
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert len(messages) == 1
    assert messages[0]["kind"] == "review"
    assert messages[0]["target"]["channel"] == "private"
    assert messages[0]["target"]["recipient"] == sender["user_id"]
    assert "private answer" not in json.dumps(messages)
    legacy = deepcopy(messages[0])
    legacy["target"].update(channel="group", recipient=sender["group_id"])
    await store.db.execute(
        "INSERT INTO outbox (id,lottery_id,body) VALUES (?,?,?)",
        ("legacy-rejected-review", item["id"], json.dumps(legacy)),
    )
    await store.save(rules, "admin", item["id"])
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert len(messages) == 1 and messages[0]["target"]["channel"] == "private"


@pytest.mark.parametrize(
    "schedule",
    [
        None,
        [],
        {"mode": True},
        {"mode": "daily"},
        {"mode": "once"},
        {"mode": "repeat", "start_at": "bad"},
    ],
)
def test_announcement_schedule_rejects_invalid_shapes_and_dates(rules, schedule):
    with pytest.raises(ValueError):
        validate_lottery({**rules, "announcement_schedule": schedule})


@pytest.mark.parametrize("interval", [True, 0, -1, 1.5, "60", 10081])
def test_announcement_repeat_interval_is_a_bounded_integer(rules, interval):
    with pytest.raises(ValueError, match="间隔"):
        validate_lottery(
            {
                **rules,
                "announcement_schedule": {
                    "mode": "repeat",
                    "start_at": time.time() + 60,
                    "interval_minutes": interval,
                },
            }
        )


async def test_announcement_once_is_atomic_and_persistent(store, rules):
    start = time.time() + 60
    item = await store.save(
        {**rules, "announcement_schedule": {"mode": "once", "start_at": start}}, "admin"
    )
    assert await store.schedule_announcements(now=start - 1) == []
    outcomes = await asyncio.gather(
        store.schedule_announcements(now=start), store.schedule_announcements(now=start)
    )
    assert outcomes.count([item["id"]]) == 1
    async with store.db.execute(
        "SELECT body FROM outbox WHERE lottery_id=?", (item["id"],)
    ) as cursor:
        messages = [json.loads(row[0]) for row in await cursor.fetchall()]
    assert len(messages) == 2 * len(rules["targets"])
    assert all(message["scheduled"] for message in messages)
    assert {message["kind"] for message in messages} == {
        "announcement",
        "participation_guide",
    }
    await store.close()
    await store.open()
    assert await store.schedule_announcements(now=start + 10) == []
    saved = await store.get(item["id"])
    assert saved["announcement_next_at"] is None
    assert saved["announcement_last_at"] == start


async def test_repeat_skips_missed_ticks_and_deduplicates_per_group(store, rules):
    start = time.time() + 60
    item = await store.save(
        {
            **rules,
            "announcement_schedule": {
                "mode": "repeat",
                "start_at": start,
                "interval_minutes": 1,
            },
        },
        "admin",
    )
    assert await store.schedule_announcements(now=start + 185) == [item["id"]]
    assert (await store.get(item["id"]))["announcement_next_at"] == start + 240
    assert await store.schedule_announcements(now=start + 240) == []
    for _ in range(2):
        for row in await store.deliveries():
            message = json.loads(row["body"])
            if message["target"]["platform_id"] == "napcat":
                await store.delivery_done(row["id"])
    assert await store.schedule_announcements(now=start + 300) == [item["id"]]
    async with store.db.execute(
        "SELECT body FROM outbox WHERE delivered_at IS NULL"
    ) as cursor:
        pending = [json.loads(row[0]) for row in await cursor.fetchall()]
    assert len(pending) == 4
    assert (await store.get(item["id"]))["announcement_next_at"] == start + 360
    assert await store.schedule_announcements(now=rules["close_at"]) == []
    assert (await store.get(item["id"]))["announcement_next_at"] is None


async def test_changing_schedule_revokes_only_automatic_jobs_and_rejects_backdating(
    store, rules, monkeypatch
):
    start = time.time() + 60
    plan = {"mode": "repeat", "start_at": start, "interval_minutes": 1}
    item = await store.save({**rules, "announcement_schedule": plan}, "admin")
    await store.schedule_announcements(now=start)
    monkeypatch.setattr(time, "time", lambda: start + 10)
    edited = await store.save(
        {**rules, "title": "更新标题", "announcement_schedule": plan},
        "admin",
        item["id"],
    )
    assert edited["announcement_next_at"] == start + 60
    assert edited["announcement_last_at"] == start
    with pytest.raises(ValueError, match="晚于当前时间"):
        await store.save(
            {**rules, "announcement_schedule": {**plan, "start_at": start - 1}},
            "admin",
            item["id"],
        )
    await store.action(item["id"], "publish")
    await store.save(
        {**rules, "announcement_schedule": {"mode": "off"}}, "admin", item["id"]
    )
    async with store.db.execute(
        "SELECT body FROM outbox WHERE delivered_at IS NULL"
    ) as cursor:
        pending = [json.loads(row[0]) for row in await cursor.fetchall()]
    assert len(pending) == 2 * len(rules["targets"])
    assert all(not message.get("scheduled") for message in pending)


@pytest.mark.parametrize("action", ["close", "cancel", "draw"])
async def test_ending_activity_revokes_recruitment_plan(store, rules, action):
    start = time.time() + 60
    item = await store.save(
        {
            **rules,
            "announcement_schedule": {
                "mode": "repeat",
                "start_at": start,
                "interval_minutes": 1,
            },
        },
        "admin",
    )
    await store.schedule_announcements(now=start)
    await store.action(item["id"], action)
    assert (await store.get(item["id"]))["announcement_next_at"] is None
    assert await store.schedule_announcements(now=start + 120) == []
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert len(messages) == len(rules["targets"])
    assert all(not message.get("scheduled") for message in messages)


async def test_announcement_schedule_cutoff_and_past_start_are_rejected(store, rules):
    for start in (time.time() - 1, rules["close_at"], rules["draw_at"]):
        with pytest.raises(ValueError):
            await store.save(
                {**rules, "announcement_schedule": {"mode": "once", "start_at": start}},
                "admin",
            )
    assert await store.snapshot() == []


@pytest.mark.parametrize(
    "tiers",
    [
        None,
        [],
        {},
        [None],
        [{"name": "", "prize": "礼物", "count": 1}],
        [{"name": "a" * 31, "prize": "礼物", "count": 1}],
        [{"name": "一等奖", "prize": "", "count": 1}],
        [{"name": "一等奖", "prize": "礼物", "count": True}],
        [{"name": "一等奖", "prize": "礼物", "count": 1.5}],
        [{"name": "一等奖", "prize": "礼物", "count": 0}],
        [{"name": "一等奖", "prize": "礼物", "count": 101}],
        [{"name": "奖", "prize": "礼物", "count": 1}] * 2,
        [{"name": f"奖{index}", "prize": "礼物", "count": 1} for index in range(11)],
        [
            {"name": "一等奖", "prize": "礼物", "count": 51},
            {"name": "二等奖", "prize": "礼物", "count": 50},
        ],
        [{"name": "一等奖", "prize": "礼物", "count": 1, "image": "../private.jpg"}],
    ],
)
def test_award_validation_rejects_malformed_or_excessive_tiers(rules, tiers):
    rules["prize_tiers"] = tiers
    with pytest.raises(ValueError):
        validate_lottery(rules)


async def test_tier_counts_assignment_and_highest_first_shortage(
    store, tier_rules, sender
):
    item = await store.save(tier_rules, "admin")
    assert item["winner_count"] == 6
    assert "一等奖" in item["prize"] and "三等奖" in item["prize"]
    for index in range(4):
        await store.enroll(item["id"], {**sender, "user_id": str(4444444 + index)})
    drawn = await store.action(item["id"], "draw")
    assert len({winner["user_id"] for winner in drawn["winners"]}) == 4
    assert [record["winner_count"] for record in drawn["tier_draws"]] == [1, 2, 1]
    assert [record["eligible_count"] for record in drawn["tier_draws"]] == [4, 3, 1]
    assert [winner["tier_name"] for winner in drawn["winners"]] == [
        "一等奖",
        "二等奖",
        "二等奖",
        "三等奖",
    ]
    assert all(
        winner["prize"] == drawn["prize_tiers"][winner["tier_index"]]["prize"]
        for winner in drawn["winners"]
    )


async def test_early_award_draw_is_atomic_and_remaining_draw_resumes_after_restart(
    store, tier_rules, sender, monkeypatch
):
    item = await store.save(tier_rules, "admin")
    await store.enroll(item["id"], sender)
    results = await asyncio.gather(
        store.action(item["id"], "draw_tier", tier_index=1),
        store.action(item["id"], "draw_tier", tier_index=1),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    partial = await store.get(item["id"])
    assert partial["status"] == "open" and partial["drawn_at"] is None
    assert (
        partial["close_at"] == item["close_at"]
        and partial["draw_at"] == item["draw_at"]
    )
    assert partial["tier_draws"][0]["winner_count"] == 1
    assert partial["winners"][0]["tier_name"] == "二等奖"
    with pytest.raises(ValueError, match="中奖"):
        await store.withdraw(item["id"], sender)
    with pytest.raises(ValueError, match="不能取消"):
        await store.action(item["id"], "cancel")
    for index in range(1, 6):
        await store.enroll(item["id"], {**sender, "user_id": str(4444444 + index)})
    await store.close()
    await store.open()
    monkeypatch.setattr(
        "astrbot_plugin_catlottery.storage.time.time", lambda: item["draw_at"]
    )
    with pytest.raises(ValueError, match="自动开奖"):
        await store.action(item["id"], "draw_tier", tier_index=0)
    final = await store.action(item["id"], "draw", due_only=True)
    assert final["status"] == "drawn"
    assert final["tier_draws"][0] == partial["tier_draws"][0]
    assert partial["winners"][0] in final["winners"]
    assert (
        len(final["winners"])
        == len({winner["user_id"] for winner in final["winners"]})
        == 5
    )
    assert sum(winner["tier_index"] == 1 for winner in final["winners"]) == 1
    notices = [json.loads(row["body"]) for row in await store.deliveries()]
    assert len([notice for notice in notices if notice["kind"] == "tier_result"]) == 2
    assert all(
        notice["item"]["winners"] == partial["winners"]
        for notice in notices
        if notice["kind"] == "tier_result"
    )
    assert len([notice for notice in notices if notice["kind"] == "result"]) == 2


async def test_empty_early_draw_freezes_only_one_award_and_rules(
    store, tier_rules, sender
):
    item = await store.save(tier_rules, "admin")
    early = await store.action(item["id"], "draw_tier", tier_index=0)
    assert early["winners"] == [] and early["tier_draws"][0]["eligible_count"] == 0
    await store.save({**tier_rules, "description": "可编辑说明"}, "admin", item["id"])
    with pytest.raises(ValueError, match="奖项"):
        await store.save(
            {**tier_rules, "prize_tiers": list(reversed(tier_rules["prize_tiers"]))},
            "admin",
            item["id"],
        )
    await store.enroll(item["id"], sender)
    final = await store.action(item["id"], "draw")
    assert final["winners"][0]["tier_index"] == 1
    assert final["tier_draws"][0]["winner_count"] == 0
    with pytest.raises(ValueError):
        await store.action(item["id"], "draw_tier", tier_index=0)


@pytest.mark.parametrize("index", [None, -1, 3, True, 1.5, "0", {}])
async def test_early_award_rejects_invalid_indexes_without_notifications(
    store, tier_rules, index
):
    item = await store.save(tier_rules, "admin")
    with pytest.raises(ValueError, match="奖项"):
        await store.action(item["id"], "draw_tier", tier_index=index)
    assert (await store.get(item["id"]))["tier_draws"] == []
    assert await store.deliveries() == []


async def test_early_winners_review_marks_are_immutable(store, tier_rules, sender):
    tier_rules.update(
        require_correct=False, questions=[{"kind": "text", "prompt": "资料"}]
    )
    item = await store.save(tier_rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "资料"}, 0
    )
    await store.review(
        item["id"],
        {
            "action": "mark",
            "user_id": sender["user_id"],
            "question_index": 0,
            "correct": True,
        },
        "operator",
    )
    await store.action(item["id"], "draw_tier", tier_index=0)
    with pytest.raises(ValueError, match="锁定"):
        await store.review(
            item["id"],
            {
                "action": "mark",
                "user_id": sender["user_id"],
                "question_index": 0,
                "correct": False,
            },
            "operator",
        )
    assert (await store.review(item["id"], {"action": "match"}, "operator"))[
        "marked_answers"
    ] == 0
    assert (await store.entry(item["id"], sender["user_id"]))[
        "review_status"
    ] == "approved"


async def test_legacy_awards_upgrade_without_rewriting_existing_results(
    store, rules, sender
):
    item = await store.save(rules, "admin")
    legacy = {
        key: value
        for key, value in item.items()
        if key not in {"prize_tiers", "cover", "tier_draws"}
    }
    await store.db.execute(
        "UPDATE lotteries SET body=? WHERE id=?", (json.dumps(legacy), item["id"])
    )
    await store.enroll(item["id"], sender)
    await store.save({**rules, "description": "保留旧活动"}, "admin", item["id"])
    assert (await store.get(item["id"]))["prize_tiers"][0]["name"] == "幸运奖"
    legacy.update(
        status="drawn",
        winners=[{"user_id": sender["user_id"], "nickname": sender["nickname"]}],
    )
    await store.db.execute(
        "UPDATE lotteries SET body=? WHERE id=?", (json.dumps(legacy), item["id"])
    )
    assert (await store.get(item["id"]))["winners"] == legacy["winners"]


async def test_artwork_reference_validation_cover_edits_and_shared_cleanup(
    store, tier_rules, sender
):
    cover = "a" * 32 + ".jpg"
    prize = "b" * 32 + ".jpg"
    unused = "c" * 32 + ".jpg"
    tier_rules["cover"] = cover
    tier_rules["prize_tiers"][0]["image"] = prize
    with pytest.raises(ValueError, match="不存在"):
        await store.save(tier_rules, "admin")
    for filename in (cover, prize, unused):
        path = store.directory / "artwork" / filename
        path.write_bytes(b"image")
        os.utime(path, (time.time() - 172800, time.time() - 172800))
    item = await store.save(tier_rules, "admin")
    await store.action(item["id"], "publish")
    await store.enroll(item["id"], sender)
    await store.save({**tier_rules, "cover": ""}, "admin", item["id"])
    assert await store.cleanup_artwork() == 1
    assert (store.directory / "artwork" / cover).is_file()
    with pytest.raises(ValueError, match="奖项"):
        altered = deepcopy(tier_rules)
        altered["prize_tiers"][0]["image"] = cover
        await store.save(altered, "admin", item["id"])
    other = await store.save(tier_rules, "admin")
    await store.action(item["id"], "cancel")
    await store.delete(item["id"])
    assert (store.directory / "artwork" / cover).is_file()
    assert (store.directory / "artwork" / prize).is_file()
    await store.action(other["id"], "cancel")
    await store.delete(other["id"])
    assert not (store.directory / "artwork" / cover).exists()
    assert not (store.directory / "artwork" / prize).exists()


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
        "avatar_cache_hours": 24,
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


async def test_form_navigation_updates_only_selected_question_and_survives_restart(
    store, rules, sender
):
    rules.update(
        require_correct=False,
        questions=[{"kind": "text", "prompt": f"问题 {i}"} for i in range(3)],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.private_session(sender["bot_id"], sender["user_id"], item["id"])
    await store.queue_question(item["id"], sender["bot_id"], sender["user_id"])
    with pytest.raises(ValueError):
        await store.form_position(sender["bot_id"], sender["user_id"], 1)
    identity = {**sender, "group_id": ""}
    await store.answer(
        item["id"], identity, {"kind": "text", "value": "  first  answer\n"}, 0
    )
    assert await store.form_position(sender["bot_id"], sender["user_id"], -1) == 0
    await store.close()
    await store.open()
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == 0
    await store.answer(
        item["id"], identity, {"kind": "text", "value": " revised  answer "}, 0
    )
    entry = await store.entry(item["id"], sender["user_id"])
    assert (
        len(entry["answers"]) == 1
        and entry["answers"][0]["value"] == " revised  answer "
    )
    with pytest.raises(ValueError):
        await store.answer(item["id"], identity, {"kind": "text", "value": "stale"}, 0)
    await store.private_session(sender["bot_id"], sender["user_id"], "")
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    with pytest.raises(ValueError, match="暂停"):
        await store.answer(
            item["id"], identity, {"kind": "text", "value": "late after cancel"}, 1
        )
    assert len((await store.entry(item["id"], sender["user_id"]))["answers"]) == 1
    await store.private_session(sender["bot_id"], sender["user_id"], item["id"])
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == 1
    await store.answer(item["id"], identity, {"kind": "text", "value": "second"}, 1)
    await store.answer(item["id"], identity, {"kind": "text", "value": "third"}, 2)
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    assert {json.loads(row["body"])["kind"] for row in await store.deliveries()} == {
        "submitted"
    }


async def test_private_cursor_migrates_legacy_sessions_and_rejects_switched_activity(
    store, rules, sender
):
    rules["questions"] = [{"kind": "text", "prompt": "姓名"}]
    first = await store.save(rules, "admin")
    second = await store.save(rules, "admin")
    await store.enroll(first["id"], sender)
    await store.enroll(second["id"], sender)
    await store.db.execute("DROP TABLE sessions")
    await store.db.execute(
        "CREATE TABLE sessions (bot_id TEXT,user_id TEXT,lottery_id TEXT,PRIMARY KEY(bot_id,user_id))"
    )
    await store.db.execute(
        "INSERT INTO sessions VALUES (?,?,?)",
        (sender["bot_id"], sender["user_id"], first["id"]),
    )
    await store.close()
    await store.open()
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == 0
    await store.private_session(sender["bot_id"], sender["user_id"], second["id"])
    with pytest.raises(ValueError, match="切换"):
        await store.answer(
            first["id"],
            {**sender, "group_id": ""},
            {"kind": "text", "value": "wrong form"},
            0,
        )
    assert (await store.entry(first["id"], sender["user_id"]))["answers"] == []


async def test_paged_management_search_filters_and_bulk_review_are_atomic(
    store, rules, sender
):
    rules.update(require_correct=False, questions=[{"kind": "text", "prompt": "资料"}])
    item = await store.save(rules, "admin")
    for index in range(65):
        identity = {
            **sender,
            "user_id": str(4444444 + index),
            "nickname": f"猫猫 {index}" if index else "猫%_!",
        }
        await store.enroll(item["id"], identity)
        if index < 64:
            await store.answer(
                item["id"],
                {**identity, "group_id": ""},
                {"kind": "text", "value": "private text"},
                0,
            )
    page = await store.manage_entries(item["id"], page=2, page_size=20)
    assert page["total"] == 65 and len(page["entries"]) == 20
    assert page["entries"][0]["user_id"] == str(4444444 + 20)
    assert all("answers" not in entry for entry in page["entries"])
    assert page["summary"] == {
        "total": 65,
        "incomplete": 1,
        "approved": 0,
        "pending": 64,
        "rejected": 0,
    }
    literal = await store.manage_entries(item["id"], page=1, query="%_!")
    assert literal["total"] == 1
    selected = ["4444444", "4444445"]
    await store.review(
        item["id"],
        {"action": "bulk", "user_ids": selected, "correct": True},
        "operator",
    )
    approved = await store.manage_entries(item["id"], page=1, status="approved")
    assert [entry["user_id"] for entry in approved["entries"]] == selected
    assert (await store.entry(item["id"], "4444446"))["review_status"] == "pending"
    for invalid in ["9999999"]:
        with pytest.raises(ValueError):
            await store.review(
                item["id"],
                {
                    "action": "bulk",
                    "user_ids": [selected[0], invalid],
                    "correct": False,
                },
                "operator",
            )
        assert (await store.entry(item["id"], selected[0]))[
            "review_status"
        ] == "approved"
    await store.review(
        item["id"],
        {"action": "bulk", "user_ids": [str(4444444 + 64)], "correct": True},
        "operator",
    )
    unfinished = await store.entry(item["id"], str(4444444 + 64))
    assert unfinished["review_status"] == "approved" and unfinished["answers"] == []
    assert (await store.manage_entries(item["id"], page=10000))["page"] == 4


@pytest.mark.parametrize("hours", [0, 169, True, "24", 24.5, None])
async def test_avatar_lifetime_validation_is_strict(store, hours):
    with pytest.raises(ValueError):
        await store.settings({"manager_ids": [], "avatar_cache_hours": hours})
    assert (await store.settings())["avatar_cache_hours"] == 24


async def test_announcement_text_waits_for_image_and_retries_independently(
    store, rules
):
    item = await store.save(rules, "admin")
    await store.action(item["id"], "publish")
    images = await store.deliveries()
    assert len(images) == 2 and all(
        json.loads(row["body"])["kind"] == "announcement" for row in images
    )
    await store.delivery_done(images[0]["id"])
    rows = await store.deliveries()
    guide = next(
        row for row in rows if json.loads(row["body"])["kind"] == "participation_guide"
    )
    assert json.loads(guide["body"])["depends_on"] == images[0]["id"]
    await store.delivery_done(guide["id"], "ConnectionError")
    await store.close()
    await store.open()
    assert images[0]["id"] not in {row["id"] for row in await store.deliveries()}
    details = await store.manage_entries(item["id"])
    assert (
        next(row for row in details["deliveries"] if row["id"] == guide["id"])[
            "attempts"
        ]
        == 1
    )


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
        {
            "user_id": sender["user_id"],
            "nickname": sender["nickname"],
            "tier_index": 0,
            "tier_name": "幸运奖",
            "prize": rules["prize"],
        }
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


@pytest.fixture
async def managed_form(store, rules, sender):
    """Create a submitted three-question form with one private image."""
    rules.update(
        require_correct=False,
        questions=[
            {"kind": "text", "prompt": "第一题"},
            {"kind": "mixed", "prompt": "图文资料"},
            {"kind": "text", "prompt": "最后一题"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    filename = "1" * 32 + ".jpg"
    (store.directory / "uploads" / filename).write_bytes(b"original private image")
    for index, answer in enumerate(
        [
            {"kind": "text", "value": "  first  answer\n"},
            {"kind": "mixed", "value": "middle", "image": filename},
            {"kind": "text", "value": "last answer"},
        ]
    ):
        await store.answer(item["id"], {**sender, "group_id": ""}, answer, index)
    return item, await store.entry(item["id"], sender["user_id"])


async def test_admin_edit_resets_whole_review_and_preserves_exact_text(
    store, sender, managed_form
):
    item, entry = managed_form
    await store.review(
        item["id"],
        {"action": "decision", "user_id": sender["user_id"], "correct": True},
        "web:admin",
    )
    entry = await store.entry(item["id"], sender["user_id"])
    text = "  new  words\nsecond  line  "
    result = await store.manage_answers(
        item["id"],
        {
            "action": "edit_answer",
            "user_id": sender["user_id"],
            "revision": entry["answer_revision"],
            "question_index": 0,
            "answer": {"kind": "text", "value": text, "correct": True},
            "notify": True,
        },
        "web:editor",
    )
    saved = await store.entry(item["id"], sender["user_id"])
    assert saved["answers"][0]["value"] == text
    assert saved["answers"][0]["edited_by"] == "web:editor"
    assert saved["review_status"] == "pending" and "review_decision" not in saved
    assert all(answer["correct"] is None for answer in saved["answers"])
    assert result["private_notices"] == 1
    messages = [json.loads(row["body"]) for row in await store.deliveries()]
    assert len(messages) == 1 and messages[0]["kind"] == "answer_changed"
    assert messages[0]["target"]["channel"] == "private"
    assert (
        "question_index" not in messages[0]["entry"]
        and "answers" not in messages[0]["entry"]
    )
    assert text not in str(messages)
    with pytest.raises(ValueError, match="更新"):
        await store.manage_answers(
            item["id"],
            {
                "action": "edit_answer",
                "user_id": sender["user_id"],
                "revision": entry["answer_revision"],
                "question_index": 0,
                "answer": {"kind": "text", "value": "stale"},
                "notify": False,
            },
            "other",
        )


async def test_deleted_middle_answer_can_be_refilled_without_losing_later_answers(
    store, sender, managed_form
):
    from astrbot_plugin_catlottery.storage import form_progress

    item, entry = managed_form
    await store.manage_answers(
        item["id"],
        {
            "action": "delete_answer",
            "user_id": sender["user_id"],
            "revision": entry["answer_revision"],
            "question_index": 1,
            "confirmed": True,
            "notify": False,
        },
        "web:admin",
    )
    deleted = await store.entry(item["id"], sender["user_id"])
    assert deleted["answers"][1]["kind"] == "deleted"
    assert deleted["answers"][2]["value"] == "last answer"
    assert form_progress(deleted, 3) == (2, 1)
    assert deleted["review_status"] == "incomplete"
    assert not list((store.directory / "uploads").glob("*.jpg"))
    assert not await store.deliveries()
    await store.private_session(sender["bot_id"], sender["user_id"], item["id"])
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == 1
    with pytest.raises(ValueError):
        await store.form_position(sender["bot_id"], sender["user_id"], 1)
    filename = "2" * 32 + ".jpg"
    (store.directory / "uploads" / filename).write_bytes(b"replacement")
    _, saved = await store.answer(
        item["id"],
        {**sender, "group_id": ""},
        {"kind": "mixed", "value": "replacement", "image": filename},
        1,
    )
    assert saved["status"] == "complete" and len(saved["answers"]) == 3
    assert saved["answers"][2]["value"] == "last answer"
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None


@pytest.mark.parametrize("notify", [True, False])
async def test_bulk_data_deletion_requires_confirmation_and_is_atomic(
    store, sender, managed_form, notify
):
    item, entry = managed_form
    other = {**sender, "user_id": "8888888"}
    await store.enroll(item["id"], other)
    await store.answer(
        item["id"],
        {**other, "group_id": ""},
        {"kind": "text", "value": "second participant"},
        0,
    )
    ids = [sender["user_id"], other["user_id"]]
    payload = {
        "action": "delete_entries",
        "user_ids": ids,
        "revisions": {ids[0]: entry["answer_revision"], ids[1]: 0},
        "notify": notify,
        "confirmed": True,
    }
    with pytest.raises(ValueError, match="更新"):
        await store.manage_answers(item["id"], payload, "web:admin")
    assert (await store.entry(item["id"], ids[0])) == entry
    payload["revisions"][ids[1]] = 1
    payload["confirmed"] = False
    with pytest.raises(ValueError, match="确认"):
        await store.manage_answers(item["id"], payload, "web:admin")
    payload["confirmed"] = True
    assert (await store.manage_answers(item["id"], payload, "web:admin"))[
        "changed_entries"
    ] == 2
    for user in ids:
        saved = await store.entry(item["id"], user)
        assert saved["status"] == "pending" and saved["review_status"] == "incomplete"
        assert all(answer["kind"] == "deleted" for answer in saved["answers"])
    assert len(await store.deliveries()) == (2 if notify else 0)
    await store.restart_forms(item["id"])
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == 0


async def test_private_image_edit_rejects_reuse_and_cleans_replaced_attachment(
    store, sender, managed_form
):
    item, entry = managed_form
    filename = "2" * 32 + ".jpg"
    (store.directory / "uploads" / filename).write_bytes(b"new private image")
    payload = {
        "action": "edit_answer",
        "user_id": sender["user_id"],
        "revision": entry["answer_revision"],
        "question_index": 0,
        "answer": {
            "kind": "text",
            "value": "optional image",
            "image": "1" * 32 + ".jpg",
        },
        "notify": False,
    }
    with pytest.raises(ValueError, match="其他答案"):
        await store.manage_answers(item["id"], payload, "admin")
    payload.update(
        question_index=1, answer={"kind": "mixed", "value": "new", "image": filename}
    )
    await store.manage_answers(item["id"], payload, "admin")
    assert not (store.directory / "uploads" / ("1" * 32 + ".jpg")).exists()
    payload["revision"] += 1
    payload["answer"] = {"kind": "mixed", "value": "image required"}
    with pytest.raises(ValueError, match="图片"):
        await store.manage_answers(item["id"], payload, "admin")
    assert (store.directory / "uploads" / filename).exists()


async def test_admin_can_restore_deleted_answer_before_draw_but_not_after(
    store, sender, managed_form, monkeypatch
):
    item, entry = managed_form
    payload = {
        "action": "delete_answer",
        "user_id": sender["user_id"],
        "revision": entry["answer_revision"],
        "question_index": 0,
        "notify": True,
        "confirmed": True,
    }
    await store.manage_answers(item["id"], payload, "admin")
    monkeypatch.setattr(time, "time", lambda: item["close_at"] + 1)
    payload.update(
        action="edit_answer",
        revision=entry["answer_revision"] + 1,
        answer={"kind": "text", "value": "restored"},
    )
    await store.manage_answers(item["id"], payload, "admin")
    saved = await store.entry(item["id"], sender["user_id"])
    assert saved["status"] == "complete" and saved["review_status"] == "pending"
    monkeypatch.setattr(time, "time", lambda: item["draw_at"])
    with pytest.raises(ValueError, match="锁定"):
        await store.manage_answers(
            item["id"], {**payload, "revision": saved["answer_revision"]}, "admin"
        )
    assert (await store.entry(item["id"], sender["user_id"])) == saved


async def test_admin_edit_still_enforces_instant_quiz_and_winner_freeze(
    store, rules, sender
):
    rules["questions"] = [{"kind": "quiz", "prompt": "题目", "answers": ["yes"]}]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "yes"}, 0
    )
    payload = {
        "action": "edit_answer",
        "user_id": sender["user_id"],
        "revision": 1,
        "question_index": 0,
        "answer": {"kind": "text", "value": "no"},
        "notify": False,
    }
    with pytest.raises(ValueError, match="不正确"):
        await store.manage_answers(item["id"], payload, "admin")
    await store.action(item["id"], "draw_tier", tier_index=0)
    with pytest.raises(ValueError, match="中奖|锁定"):
        await store.manage_answers(
            item["id"], {**payload, "answer": {"kind": "text", "value": "yes"}}, "admin"
        )


async def test_restart_forms_resets_delivery_backoff_and_never_creates_private_enrollment(
    store, rules, sender, monkeypatch
):
    rules["questions"] = [
        {"kind": "text", "prompt": "第一题"},
        {"kind": "text", "prompt": "第二题"},
    ]
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"], {**sender, "group_id": ""}, {"kind": "text", "value": "saved"}, 0
    )
    await store.queue_question(item["id"], sender["bot_id"], sender["user_id"])
    old = (await store.deliveries())[0]
    await store.delivery_done(old["id"], "offline")
    assert not await store.deliveries()
    await store.private_session(sender["bot_id"], sender["user_id"], "")
    assert (
        await store.restart_forms(
            bot_id=sender["bot_id"],
            user_id="9999999",
            platform_id=sender["platform_id"],
        )
    )["restarted_users"] == 0
    assert (
        await store.restart_forms(
            bot_id=sender["bot_id"], user_id=sender["user_id"], platform_id="other"
        )
    )["restarted_users"] == 0
    assert (
        await store.restart_forms(
            bot_id=sender["bot_id"],
            user_id=sender["user_id"],
            platform_id=sender["platform_id"],
        )
    )["restarted_users"] == 1
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == 1
    assert (await store.entry(item["id"], sender["user_id"]))["answers"][0][
        "value"
    ] == "saved"
    await store.restart_forms(item["id"])
    assert len(await store.deliveries()) == 1
    monkeypatch.setattr(time, "time", lambda: item["close_at"])
    with pytest.raises(ValueError, match="截止"):
        await store.restart_forms(item["id"])
    assert (
        await store.restart_forms(
            bot_id=sender["bot_id"],
            user_id=sender["user_id"],
            platform_id=sender["platform_id"],
        )
    )["restarted_users"] == 0


async def test_overall_review_has_no_per_question_manual_marks_and_rejects_stale_revision(
    store, sender, managed_form
):
    item, entry = managed_form
    await store.review(
        item["id"],
        {
            "action": "decision",
            "user_id": sender["user_id"],
            "correct": False,
            "revision": entry["answer_revision"],
        },
        "web:admin",
    )
    saved = await store.entry(item["id"], sender["user_id"])
    assert saved["review_status"] == "rejected" and saved["review_decision"] is False
    assert all(answer.get("correct") is None for answer in saved["answers"])
    with pytest.raises(ValueError, match="更新"):
        await store.review(
            item["id"],
            {
                "action": "decision",
                "user_id": sender["user_id"],
                "correct": True,
                "revision": entry["answer_revision"],
            },
            "web:other",
        )
    assert (await store.entry(item["id"], sender["user_id"])) == saved


@pytest.mark.parametrize(
    "change",
    [
        {"revision": True},
        {"revision": -1},
        {"notify": "false"},
        {"confirmed": "true"},
        {"question_index": True},
        {"question_index": 999},
        {"user_id": "quoted sender"},
    ],
)
async def test_invalid_admin_delete_payload_never_mutates_data(
    store, sender, managed_form, change
):
    item, entry = managed_form
    payload = {
        "action": "delete_answer",
        "user_id": sender["user_id"],
        "question_index": 0,
        "revision": entry["answer_revision"],
        "notify": False,
        "confirmed": True,
        **change,
    }
    with pytest.raises(ValueError):
        await store.manage_answers(item["id"], payload, "web:admin")
    assert await store.entry(item["id"], sender["user_id"]) == entry
    assert (store.directory / "uploads" / ("1" * 32 + ".jpg")).exists()


async def test_edit_and_draw_serialize_without_awarding_an_invalidated_entry(
    store, sender, managed_form
):
    item, entry = managed_form
    await store.review(
        item["id"],
        {"action": "decision", "user_id": sender["user_id"], "correct": True},
        "web:admin",
    )
    entry = await store.entry(item["id"], sender["user_id"])
    payload = {
        "action": "edit_answer",
        "user_id": sender["user_id"],
        "question_index": 0,
        "revision": entry["answer_revision"],
        "notify": False,
        "answer": {"kind": "text", "value": "requires review again"},
    }
    edit, draw = await asyncio.gather(
        store.manage_answers(item["id"], payload, "web:admin"),
        store.action(item["id"], "draw"),
        return_exceptions=True,
    )
    saved = await store.entry(item["id"], sender["user_id"])
    if isinstance(edit, ValueError):
        assert draw["winners"][0]["user_id"] == sender["user_id"] and saved == entry
    else:
        assert draw["winners"] == [] and saved["review_status"] == "pending"


async def test_restarting_a_form_preserves_active_activity_when_multiple_are_pending(
    store, rules, sender
):
    rules["questions"] = [{"kind": "text", "prompt": "资料"}]
    first = await store.save(rules, "admin")
    await store.enroll(first["id"], sender)
    second = await store.save({**rules, "title": "second"}, "admin")
    await store.enroll(second["id"], sender)
    await store.restart_forms(
        bot_id=sender["bot_id"],
        user_id=sender["user_id"],
        platform_id=sender["platform_id"],
    )
    assert (
        await store.private_session(sender["bot_id"], sender["user_id"]) == second["id"]
    )
    assert len(await store.deliveries()) == 1
    await store.restart_forms(first["id"])
    assert (
        await store.private_session(sender["bot_id"], sender["user_id"]) == first["id"]
    )
    jobs = await store.deliveries()
    assert len(jobs) == 1 and jobs[0]["lottery_id"] == first["id"]


async def test_removing_optional_admin_image_only_deletes_the_unreferenced_file(
    store, sender, managed_form
):
    item, entry = managed_form
    filename = "3" * 32 + ".jpg"
    (store.directory / "uploads" / filename).write_bytes(b"optional attachment")
    payload = {
        "action": "edit_answer",
        "user_id": sender["user_id"],
        "question_index": 0,
        "revision": entry["answer_revision"],
        "notify": False,
        "answer": {"kind": "text", "value": "text", "image": filename},
    }
    await store.manage_answers(item["id"], payload, "admin")
    payload["revision"] += 1
    payload["answer"].pop("image")
    await store.manage_answers(item["id"], payload, "admin")
    assert not (store.directory / "uploads" / filename).exists()
    assert (store.directory / "uploads" / ("1" * 32 + ".jpg")).exists()


async def test_admin_deletion_pauses_old_prompt_before_the_user_can_reply(
    store, rules, sender
):
    rules.update(
        require_correct=False,
        questions=[
            {"kind": "text", "prompt": "first"},
            {"kind": "text", "prompt": "second"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    await store.answer(
        item["id"],
        {**sender, "group_id": ""},
        {"kind": "text", "value": "first answer"},
        0,
    )
    await store.manage_answers(
        item["id"],
        {
            "action": "delete_answer",
            "user_id": sender["user_id"],
            "question_index": 0,
            "revision": 1,
            "confirmed": True,
            "notify": False,
        },
        "admin",
    )
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    with pytest.raises(ValueError, match="暂停"):
        await store.answer(
            item["id"],
            {**sender, "group_id": ""},
            {"kind": "text", "value": "reply to old prompt"},
            0,
        )
    assert (await store.entry(item["id"], sender["user_id"]))["answers"][0][
        "kind"
    ] == "deleted"


@pytest.mark.parametrize("require_correct", [False, True])
@pytest.mark.parametrize("answer_count", [0, 1])
@pytest.mark.parametrize("correct", [False, True])
async def test_manual_decision_concludes_incomplete_form_without_fabricating_answers(
    store, rules, sender, require_correct, answer_count, correct
):
    rules.update(
        require_correct=require_correct,
        group_success_notify=False,
        group_pending_notify=False,
        questions=[
            {"kind": "text", "prompt": "第一题"},
            {"kind": "mixed", "prompt": "尚未提交的图文"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    if answer_count:
        await store.answer(
            item["id"],
            {**sender, "group_id": ""},
            {"kind": "text", "value": "  原样答案  "},
            0,
        )
    before = await store.entry(item["id"], sender["user_id"])
    result = await store.review(
        item["id"],
        {
            "action": "decision",
            "user_id": sender["user_id"],
            "correct": correct,
            "revision": before["answer_revision"],
        },
        "web:reviewer",
    )
    saved = await store.entry(item["id"], sender["user_id"])
    assert saved["answers"] == before["answers"]
    assert saved["status"] == "complete"
    assert saved["review_status"] == ("approved" if correct else "rejected")
    assert (
        saved["review_decision"] is correct and saved["reviewed_by"] == "web:reviewer"
    )
    assert saved["answer_revision"] == before["answer_revision"] + 1
    assert result["changed_entries"] == 1 and result["approved_entries"] == int(correct)
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    assert (await store.restart_forms(item["id"]))["restarted_users"] == 0
    notices = [json.loads(row["body"]) for row in await store.deliveries()]
    assert len(notices) == 1 and notices[0]["kind"] == "review"
    assert notices[0]["target"]["channel"] == "private"
    assert "原样答案" not in str(notices) and "answers" not in notices[0]["entry"]
    summary = (await store.manage_entries(item["id"]))["summary"]
    assert summary["incomplete"] == 0 and summary["approved"] == int(correct)
    assert summary["rejected"] == int(not correct)
    await store.close()
    await store.open()
    assert (await store.entry(item["id"], sender["user_id"]))["review_status"] == saved[
        "review_status"
    ]
    if not correct:
        restarted, _ = await store.enroll(item["id"], sender, restart_existing=True)
        assert restarted["answers"] == [] and restarted["review_status"] == "incomplete"
        assert "review_decision" not in restarted
    draw = await store.action(item["id"], "draw")
    assert draw["eligible_count"] == int(correct)
    assert [winner["user_id"] for winner in draw["winners"]] == (
        [sender["user_id"]] if correct else []
    )


@pytest.mark.parametrize("require_correct", [False, True])
@pytest.mark.parametrize("index", [0, 2])
async def test_admin_can_fill_any_missing_question_preserving_gaps_and_revision(
    store, rules, sender, require_correct, index
):
    rules.update(
        require_correct=require_correct,
        questions=[
            {"kind": "text", "prompt": f"第 {number} 题"} for number in range(3)
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    text = "  人工补填\n保留空格  "
    payload = {
        "action": "edit_answer",
        "user_id": sender["user_id"],
        "question_index": index,
        "revision": 0,
        "answer": {"kind": "text", "value": text},
        "notify": False,
    }
    await store.manage_answers(item["id"], payload, "web:editor")
    saved = await store.entry(item["id"], sender["user_id"])
    assert saved["answers"][index]["value"] == text
    assert saved["answers"][index]["edited_by"] == "web:editor"
    assert all(answer["kind"] == "deleted" for answer in saved["answers"][:index])
    assert saved["status"] == "pending" and saved["review_status"] == "incomplete"
    assert await store.private_session(sender["bot_id"], sender["user_id"]) is None
    with pytest.raises(ValueError, match="更新"):
        await store.review(
            item["id"],
            {
                "action": "decision",
                "user_id": sender["user_id"],
                "correct": True,
                "revision": 0,
            },
            "web:stale",
        )
    assert (await store.entry(item["id"], sender["user_id"])) == saved
    await store.private_session(sender["bot_id"], sender["user_id"], item["id"])
    assert await store.form_position(sender["bot_id"], sender["user_id"]) == (
        1 if index == 0 else 0
    )
    await store.review(
        item["id"],
        {
            "action": "decision",
            "user_id": sender["user_id"],
            "correct": True,
            "revision": 1,
        },
        "web:reviewer",
    )
    assert (await store.entry(item["id"], sender["user_id"]))[
        "review_status"
    ] == "approved"
    payload.update(revision=2, answer={"kind": "text", "value": "再次修改"})
    await store.manage_answers(item["id"], payload, "web:editor")
    edited = await store.entry(item["id"], sender["user_id"])
    assert edited["review_status"] == "incomplete" and "review_decision" not in edited
    assert (await store.action(item["id"], "draw"))["eligible_count"] == 0


async def test_mixed_complete_and_incomplete_bulk_review_rolls_back_stale_or_missing_selection(
    store, rules, sender
):
    rules.update(require_correct=False, questions=[{"kind": "text", "prompt": "资料"}])
    item = await store.save(rules, "admin")
    other = {**sender, "user_id": "8888888"}
    await store.enroll(item["id"], sender)
    await store.enroll(item["id"], other)
    await store.answer(
        item["id"], {**other, "group_id": ""}, {"kind": "text", "value": "已提交"}, 0
    )
    ids = [sender["user_id"], other["user_id"]]
    payload = {
        "action": "bulk",
        "user_ids": ids,
        "correct": True,
        "revisions": {ids[0]: 0, ids[1]: 0},
    }
    before = [await store.entry(item["id"], user) for user in ids]
    notices = await store.deliveries()
    with pytest.raises(ValueError, match="更新"):
        await store.review(item["id"], payload, "web:reviewer")
    assert [await store.entry(item["id"], user) for user in ids] == before
    assert await store.deliveries() == notices
    payload["revisions"][ids[1]] = 1
    with pytest.raises(ValueError, match="已删除"):
        await store.review(
            item["id"], {**payload, "user_ids": [*ids, "9999999"]}, "web:reviewer"
        )
    assert [await store.entry(item["id"], user) for user in ids] == before
    assert (await store.review(item["id"], payload, "web:reviewer"))[
        "approved_entries"
    ] == 2
    details = await store.manage_entries(item["id"], page=1, status="approved")
    assert {entry["user_id"] for entry in details["entries"]} == set(ids)
    assert sorted(entry["answer_count"] for entry in details["entries"]) == [0, 1]


async def test_unfilled_image_slot_validation_rolls_back_without_placeholder_answers(
    store, rules, sender
):
    rules.update(
        require_correct=False,
        questions=[
            {"kind": "text", "prompt": "文字"},
            {"kind": "image", "prompt": "照片"},
        ],
    )
    item = await store.save(rules, "admin")
    await store.enroll(item["id"], sender)
    before = await store.entry(item["id"], sender["user_id"])
    with pytest.raises(ValueError):
        await store.manage_answers(
            item["id"],
            {
                "action": "edit_answer",
                "user_id": sender["user_id"],
                "question_index": 1,
                "revision": 0,
                "notify": False,
                "answer": {"kind": "image", "value": "a" * 32 + ".jpg"},
            },
            "web:editor",
        )
    assert await store.entry(item["id"], sender["user_id"]) == before
    assert (await store.manage_entries(item["id"]))["summary"]["incomplete"] == 1
