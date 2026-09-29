"""Verify long Chinese cards remain valid PNGs with dynamic layout."""

from io import BytesIO

from astrbot_plugin_catlottery.cards import render, render_pages
from PIL import Image


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
