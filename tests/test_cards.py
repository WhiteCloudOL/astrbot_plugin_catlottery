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
