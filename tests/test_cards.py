"""Verify long Chinese cards remain valid PNGs with dynamic layout."""

from io import BytesIO

import pytest
from astrbot_plugin_catlottery.cards import (
    FONT_PATH,
    render,
    render_announcement,
    render_pages,
    wrap,
)
from PIL import Image, ImageFont


def test_landscape_pages_bound_long_copy_and_preserve_all_award_images(tmp_path):
    import time

    filename = "a" * 32 + ".jpg"
    with Image.new("RGB", (320, 240), "#e62080") as picture:
        picture.save(tmp_path / filename)
    item = {
        "id": "a1234567",
        "title": "很长的活动名称" * 13,
        "description": "说明" * 750,
        "prize_tiers": [
            {
                "name": f"第 {index + 1} 项",
                "prize": "很长奖品" * 75,
                "count": 1,
                "image": filename,
            }
            for index in range(10)
        ],
        "winner_count": 10,
        "cover": filename,
        "questions": [],
        "close_at": time.time() + 3600,
        "draw_at": time.time() + 7200,
    }
    pages = render_announcement(item, tmp_path)
    assert len(pages) == 4
    for png in pages:
        with Image.open(BytesIO(png)) as picture:
            assert picture.size == (1280, 860)
            assert picture.getpixel((1140, 125))[0] > 200
            assert picture.getpixel((1140, 125))[1] < 100
    font = ImageFont.truetype(str(FONT_PATH), 24)
    lines = wrap(
        "一二三四五六七八九。", font, int(font.getlength("一二三四五六七八九"))
    )
    assert "".join(lines) == "一二三四五六七八九。"
    assert all(not line.startswith("。") for line in lines)


def test_chinese_help_and_long_titles_render_without_browser():
    png = render(
        "把好运留给你，喵！" * 5,
        "QQ群抽奖 · UTC+8",
        [("使用帮助", "/抽奖 参与 a1234567\n" + "蓝粉猫猫图案和中文说明" * 30)],
    )
    with Image.open(BytesIO(png)) as image:
        assert image.format == "PNG"
        assert image.width == 860
        assert image.height > 800


@pytest.mark.parametrize("size", [(160, 160), (100, 100), (200, 100), (100, 200)])
def test_enrollment_receipt_uses_the_selected_qq_avatar(tmp_path, size):
    path = tmp_path / "4444444.jpg"
    with Image.new("RGB", size, "#247abd") as avatar:
        avatar.save(path)
    pages = render_pages(
        "参与成功", "QQ 4444444", [("报名确认", "报名已保存")], avatar_path=path
    )
    with Image.open(BytesIO(pages[0])) as image:
        color = image.getpixel((740, 115))
        assert color[2] > 150 and color[0] < 70
        for position in [(680, 116), (815, 116), (748, 46), (748, 184)]:
            color = image.getpixel(position)
            assert color[2] > 150 and color[0] < 70
        assert image.getpixel((673, 41)) != image.getpixel((748, 46))


def test_large_winner_list_is_paginated_into_readable_images():
    pages = render_pages(
        "开奖啦！",
        "跨群抽奖",
        [
            (
                "中奖名单",
                "\n".join(
                    f"{index + 1}. 猫猫的长昵称 · QQ {1000000 + index}"
                    for index in range(100)
                ),
            )
        ],
    )
    assert len(pages) > 1
    for png in pages:
        with Image.open(BytesIO(png)) as image:
            assert image.width == 860
            assert image.height <= 2300


def test_cover_and_award_images_survive_pagination_without_hiding_long_winners(
    tmp_path,
):
    sections = []
    for index, color in enumerate(["#e62080", "#1b73de", "#24a780"]):
        path = tmp_path / f"award{index}.jpg"
        Image.new("RGB", (800, 450), color).save(path, "PNG")
        sections.append(
            (
                f"第 {index + 1} 个奖项",
                "\n".join(
                    f"猫猫{member} · QQ {1000000 + member}" for member in range(70)
                ),
                path,
            )
        )
    pages = render_pages("本次抽奖", "封面与分级奖品", sections)
    colors = set()
    for png in pages:
        with Image.open(BytesIO(png)) as picture:
            assert picture.width == 860 and picture.height <= 2300
            colors.update(
                color for _, color in picture.getcolors(picture.width * picture.height)
            )
    assert {(230, 32, 128), (27, 115, 222), (36, 167, 128)} <= colors


def test_missing_artwork_keeps_public_text_and_valid_card(tmp_path):
    pages = render_pages(
        "空奖项",
        "无人参与",
        [("一等奖", "本奖项无人符合资格", tmp_path / "missing.jpg")],
    )
    assert len(pages) == 1
    with Image.open(BytesIO(pages[0])) as picture:
        assert picture.format == "PNG"


def test_corrupt_optional_artwork_and_avatar_do_not_block_result_cards(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"incomplete image")
    pages = render_pages(
        "开奖结果",
        "活动 a1234567",
        [("一等奖", "小云朵 · QQ 4444444", broken)],
        avatar_path=broken,
    )
    with Image.open(BytesIO(pages[0])) as picture:
        assert picture.format == "PNG"
        assert picture.height <= 2300
