"""Draw matching pastel QQ message cards using Pillow, without a browser."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

CardSection = tuple[str, str] | tuple[str, str, Path]

FONT_PATH = Path(__file__).parent / "pages" / "manage" / "assets" / "NotoSansSC.ttf"
LOGO_PATH = Path(__file__).parent / "logo.png"


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Wrap Chinese and Latin copy by actual glyph width.

    Args:
        text: Content, including intentional newlines.
        font: The bundled cross-platform Chinese font.
        width: Maximum line width in pixels.

    Returns:
        Lines that fit the available drawing width.
    """
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for char in paragraph:
            if font.getlength(line + char) > width and line:
                if char in "，。！？；：、,.!?;:）】》" and len(line) > 1:
                    lines.append(line[:-1])
                    line = line[-1] + char
                else:
                    lines.append(line)
                    line = char
            else:
                line += char
        lines.append(line)
    return lines or [""]


def render_announcement(
    item: dict, directory: Path, *, participation_counts: dict | None = None
) -> list[bytes]:
    """Render landscape announcements with bounded award tiles and clear participation.

    Args:
        item: Public lottery snapshot, including cover and ordered awards.
        directory: Plugin-owned sanitized artwork directory.
        participation_counts: Live approved and pending totals at delivery time.

    Returns:
        Landscape PNG pages, containing up to three awards per page. Long
        descriptions link to the complete activity details instead of overflowing.
    """
    from .storage import date_text, prize_tiers

    tiers = prize_tiers(item)
    title_font = ImageFont.truetype(str(FONT_PATH), 42)
    heading_font = ImageFont.truetype(str(FONT_PATH), 26)
    body_font = ImageFont.truetype(str(FONT_PATH), 23)
    small_font = ImageFont.truetype(str(FONT_PATH), 19)
    pages = []
    for offset in range(0, len(tiers), 3):
        tile_count = len(tiers[offset : offset + 3])
        tile_width = (1216 - 20 * (tile_count - 1)) // tile_count
        compact = all(
            not tier.get("image")
            and len(wrap(tier["prize"], body_font, tile_width - 48)) <= 2
            for tier in tiers[offset : offset + 3]
        )
        shift = 122 if compact else 0
        canvas = Image.new("RGB", (1280, 860 - shift), "#fff4f6")
        draw = ImageDraw.Draw(canvas)

        def draw_lines(text, position, font, width, count, color="#633c47", spacing=34):
            """Draw a bounded block, marking abbreviated copy with an ellipsis.

            Args:
                text: Original content.
                position: Top-left coordinates.
                font: Bundled font at the target size.
                width: Available pixel width.
                count: Maximum visible lines.
                color: Text color.
                spacing: Line height in pixels.
            """
            lines = wrap(text, font, width)
            if len(lines) > count:
                lines = lines[:count]
                while lines[-1] and font.getlength(lines[-1] + "…") > width:
                    lines[-1] = lines[-1][:-1]
                lines[-1] += "…"
            for index, line in enumerate(lines):
                draw.text(
                    (position[0], position[1] + index * spacing),
                    line,
                    font=font,
                    fill=color,
                    anchor="lt",
                )

        draw.rounded_rectangle((24, 22, 1256, 224), radius=30, fill="#ffdde7")
        draw.rounded_rectangle((48, 42, 170, 77), radius=17, fill="white")
        draw.text((65, 49), "抽奖公告", font=small_font, fill="#c83c6b", anchor="lt")
        if participation_counts is not None:
            draw.text(
                (194, 49),
                f"成功参与 {participation_counts['approved']} 人  ·  待审核 {participation_counts['pending']} 人",
                font=small_font,
                fill="#923750",
                anchor="lt",
            )
        draw_lines(item["title"], (50, 94), title_font, 932, 2, "#923750", 54)
        draw.text(
            (51, 193),
            f"活动 {item['id']}  ·  共 {item['winner_count']} 个名额",
            font=small_font,
            fill="#956070",
            anchor="lt",
        )
        images = [
            (
                directory / item["cover"] if item.get("cover") else LOGO_PATH,
                (1048, 52, 1230, 194),
                False,
            )
        ]
        for index, tier in enumerate(tiers[offset : offset + 3]):
            x = 32 + index * (tile_width + 20)
            draw.rounded_rectangle(
                (x, 246, x + tile_width, 548 - shift),
                radius=24,
                fill="white",
                outline="#f2c7d5",
                width=1,
            )
            draw.rounded_rectangle((x + 22, 268, x + 27, 294), radius=2, fill="#db5780")
            draw_lines(
                tier["name"], (x + 39, 269), heading_font, tile_width - 62, 1, "#c83c6b"
            )
            image_path = directory / tier.get("image", "")
            has_image = bool(tier.get("image")) and image_path.is_file()
            if has_image:
                images.append(
                    (image_path, (x + 24, 312, x + tile_width - 24, 422), True)
                )
            draw_lines(
                tier["prize"],
                (x + 24, 438 if has_image else 324),
                body_font,
                tile_width - 48,
                2 if has_image else 4,
            )
            drawn = any(
                record["tier_index"] == offset + index
                for record in item.get("tier_draws", [])
            )
            draw.text(
                (x + 24, 513 - shift),
                f"{tier['count']} 位  ·  {'已提前揭晓' if drawn else '等待好运'}",
                font=small_font,
                fill="#a56d7d",
                anchor="lt",
            )
        draw.rounded_rectangle(
            (32, 570 - shift, 585, 724 - shift), radius=24, fill="white"
        )
        draw.text(
            (57, 592 - shift),
            "活动时间 · 北京时间",
            font=heading_font,
            fill="#c83c6b",
            anchor="lt",
        )
        draw.text(
            (57, 640 - shift),
            "报名截止  " + date_text(item["close_at"]),
            font=body_font,
            fill="#633c47",
            anchor="lt",
        )
        draw.text(
            (57, 678 - shift),
            "自动开奖  " + date_text(item["draw_at"]),
            font=body_font,
            fill="#633c47",
            anchor="lt",
        )
        draw.rounded_rectangle(
            (607, 570 - shift, 1248, 724 - shift), radius=24, fill="#ffe2ec"
        )
        draw.text(
            (632, 590 - shift),
            "在本群发送，即可报名",
            font=heading_font,
            fill="#ac345b",
            anchor="lt",
        )
        draw.text(
            (632, 632 - shift),
            f"/抽奖 参与 {item['id']}",
            font=heading_font,
            fill="#c83c6b",
            anchor="lt",
        )
        mode = (
            f"私聊回答 {len(item['questions'])} 题 · "
            + (
                "答对后获得资格"
                if item.get("require_correct", True)
                else "提交后由管理员审核"
            )
            if item["questions"]
            else "无需填写资料 · 报名即可成功参与"
        )
        draw_lines(mode, (632, 682 - shift), small_font, 585, 1)
        draw_lines(
            item.get("description")
            or "每个 QQ 号最多中奖一次，仅成功参与者进入开奖名单",
            (48, 747 - shift),
            small_font,
            1184,
            1,
            "#956070",
        )
        draw.line((48, 783 - shift, 1232, 783 - shift), fill="#eebdcc")
        footer = f"完整规则与奖品说明：/抽奖 详情 {item['id']}"
        if len(tiers) > 3:
            footer += f"   ·   奖项 {offset // 3 + 1}/{(len(tiers) + 2) // 3} 页"
        draw.text(
            (48, 802 - shift), footer, font=small_font, fill="#aa7183", anchor="lt"
        )
        for path, box, left_aligned in images:
            if not path.is_file():
                continue
            try:
                with Image.open(path) as original:
                    with original.convert("RGB") as picture:
                        picture.thumbnail(
                            (box[2] - box[0], box[3] - box[1]), Image.Resampling.LANCZOS
                        )
                        canvas.paste(
                            picture,
                            (
                                box[0]
                                if left_aligned
                                else box[0] + (box[2] - box[0] - picture.width) // 2,
                                box[1] + (box[3] - box[1] - picture.height) // 2,
                            ),
                        )
            except OSError:
                continue
        output = BytesIO()
        canvas.save(output, "PNG", optimize=True)
        canvas.close()
        pages.append(output.getvalue())
    return pages


def render(
    title: str,
    subtitle: str,
    sections: list[CardSection],
    badge: str = "喵喵抽奖",
    *,
    avatar_path: Path | None = None,
    participation_counts: dict | None = None,
    bold_title: bool = False,
) -> bytes:
    """Render a complete cat-themed PNG with dynamically sized text blocks.

    Args:
        title: Primary message, such as registration or drawing results.
        subtitle: Secondary context, including the activity identifier.
        sections: Labels and text, optionally followed by a sanitized artwork path.
        badge: Short category printed above the title.
        avatar_path: Optional cached QQ avatar for enrollment receipts.
        participation_counts: Optional live enrollment totals in the card header.
        bold_title: Emphasize the title with a one-pixel stroke in the bundled font.

    Returns:
        PNG bytes suitable for AstrBot Image.fromBytes and OneBot base64.
    """
    title_font = ImageFont.truetype(str(FONT_PATH), 38)
    body_font = ImageFont.truetype(str(FONT_PATH), 24)
    label_font = ImageFont.truetype(str(FONT_PATH), 21)
    small_font = ImageFont.truetype(str(FONT_PATH), 18)
    title_lines = wrap(title, title_font, 570)
    subtitle_lines = wrap(subtitle, small_font, 565)
    header_height = max(238, 132 + len(title_lines) * 54 + len(subtitle_lines) * 29)
    blocks = []
    for section in sections:
        label, text = section[:2]
        lines = wrap(text, body_font, 720) if text else []
        labels = wrap(label, label_font, 728)
        picture = None
        if len(section) == 3 and section[2].is_file():
            with Image.open(section[2]) as source:
                source.thumbnail((732, 320), Image.Resampling.LANCZOS)
                picture = source.convert("RGB")
        image_height = picture.height + 20 if picture is not None else 0
        blocks.append(
            (
                labels,
                lines,
                picture,
                44 + 30 * len(labels) + 38 * len(lines) + image_height,
            )
        )
    height = header_height + sum(block[3] + 18 for block in blocks) + 86
    canvas = Image.new("RGB", (860, height), "#fff3f6")
    draw = ImageDraw.Draw(canvas)
    # Pink stationery uses explicit top anchors to avoid font-bearing layout drift.
    for y in range(header_height):
        ratio = y / max(header_height, 1)
        draw.line(
            (0, y, 860, y),
            fill=(255, int(213 + ratio * 20), int(225 + ratio * 16)),
        )
    draw.ellipse((-100, -140, 195, 150), fill="#ffd8e3")
    draw.rounded_rectangle(
        (42, 30, 42 + int(small_font.getlength(badge)) + 34, 64),
        radius=17,
        fill="#ffffff",
    )
    draw.text((59, 38), badge, font=small_font, fill="#c13e68", anchor="lt")
    if participation_counts is not None:
        draw.text(
            (96 + int(small_font.getlength(badge)), 38),
            f"成功参与 {participation_counts['approved']} 人 · 待审核 {participation_counts['pending']} 人",
            font=small_font,
            fill="#923750",
            anchor="lt",
        )
    y = 91
    for line in title_lines:
        draw.text(
            (46, y),
            line,
            font=title_font,
            fill="#923750",
            anchor="lt",
            stroke_width=1 if bold_title else 0,
            stroke_fill="#923750",
        )
        y += 54
    for line in subtitle_lines:
        draw.text((48, y + 14), line, font=small_font, fill="#956070", anchor="lt")
        y += 29
    # Participant avatars fill the rounded frame; the brand mark keeps its padding.
    draw.rounded_rectangle((672, 40, 824, 192), radius=24, fill="#ffffff")
    has_avatar = bool(avatar_path and avatar_path.is_file())
    with Image.open(avatar_path if has_avatar else LOGO_PATH) as logo:
        with logo.convert("RGB") as picture:
            if has_avatar:
                with (
                    ImageOps.fit(
                        picture, (152, 152), Image.Resampling.LANCZOS
                    ) as fitted,
                    Image.new("L", (152, 152), 0) as mask,
                ):
                    ImageDraw.Draw(mask).rounded_rectangle(
                        (0, 0, 151, 151), radius=24, fill=255
                    )
                    canvas.paste(fitted, (672, 40), mask)
            else:
                picture.thumbnail((138, 138), Image.Resampling.LANCZOS)
                canvas.paste(
                    picture,
                    (
                        672 + (152 - picture.width) // 2,
                        40 + (152 - picture.height) // 2,
                    ),
                )
    y = header_height + 10
    for labels, lines, picture, block_height in blocks:
        draw.rounded_rectangle(
            (32, y + 2, 828, y + block_height + 2), radius=22, fill="#f1ceda"
        )
        draw.rounded_rectangle(
            (32, y, 828, y + block_height), radius=22, fill="#ffffff"
        )
        draw.rounded_rectangle((49, y + 22, 54, y + 44), radius=2, fill="#df688f")
        for number, label in enumerate(labels):
            draw.text(
                (67, y + 21 + number * 30),
                label,
                font=label_font,
                fill="#ba3c65",
                anchor="lt",
            )
        line_y = y + 34 + len(labels) * 30
        for line in lines:
            command = line.startswith("/抽奖")
            friend_hint = line.startswith("请先添加我")
            if command or friend_hint:
                draw.rounded_rectangle(
                    (
                        59,
                        line_y - 5,
                        59 + min(735, int(body_font.getlength(line)) + 16),
                        line_y + 30,
                    ),
                    radius=8,
                    fill="#ffe0eb" if friend_hint else "#fff0f5",
                )
            draw.text(
                (65, line_y),
                line,
                font=body_font,
                fill="#b83b65" if command or friend_hint else "#633c47",
                anchor="lt",
                stroke_width=1 if friend_hint else 0,
                stroke_fill="#b83b65",
            )
            line_y += 38
        if picture is not None:
            canvas.paste(picture, ((860 - picture.width) // 2, line_y + 10))
            picture.close()
        y += block_height + 18
    draw.line((46, height - 59, 814, height - 59), fill="#f0ccd8", width=1)
    draw.text(
        (49, height - 44),
        "喵喵抽奖  ·  每一份期待，都认真收好",
        font=small_font,
        fill="#aa7183",
        anchor="lt",
    )
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_pages(
    title: str,
    subtitle: str,
    sections: list[CardSection],
    badge: str = "喵喵抽奖",
    *,
    avatar_path: Path | None = None,
    participation_counts: dict | None = None,
    bold_title: bool = False,
) -> list[bytes]:
    """Paginate complete content so large winner lists remain readable in QQ.

    Args:
        title: Primary message title.
        subtitle: Activity and identity context.
        sections: Complete labeled content, including long participant lists.
        badge: Category shown on every page.
        avatar_path: Optional cached participant avatar for every receipt page.
        participation_counts: Live enrollment totals repeated on every page.
        bold_title: Emphasize the main title consistently on every page.

    Returns:
        Ordered PNG pages with a bounded height and no dropped text.
    """
    body_font = ImageFont.truetype(str(FONT_PATH), 24)
    label_font = ImageFont.truetype(str(FONT_PATH), 21)
    title_font = ImageFont.truetype(str(FONT_PATH), 38)
    small_font = ImageFont.truetype(str(FONT_PATH), 18)
    # Reserve an extra subtitle line for page numbering before splitting blocks.
    header = (
        172
        + len(wrap(title, title_font, 570)) * 54
        + len(wrap(subtitle, small_font, 565)) * 29
    )
    header = max(278, header)
    pages: list[list[CardSection]] = []
    current: list[CardSection] = []
    used = 0
    for section in sections:
        label, text = section[:2]
        lines = wrap(text, body_font, 720)
        if len(section) == 3:
            # Put photographs on a complete block; only its text may need continuation.
            image_height = 340 if section[2].is_file() else 0
            block_height = (
                44
                + 30 * len(wrap(label, label_font, 728))
                + 38 * len(lines)
                + image_height
                + 18
            )
            if block_height <= 2300 - header - 86:
                if current and used + block_height > 2300 - header - 86:
                    pages.append(current)
                    current = []
                    used = 0
                current.append(section)
                used += block_height
                continue
        remaining = lines
        continued = False
        while remaining:
            page_label = label + ("（续）" if continued else "")
            label_height = 44 + 30 * len(wrap(page_label, label_font, 728)) + 18
            count = int((2300 - header - 86 - used - label_height) // 38)
            if count < 1 and current:
                pages.append(current)
                current = []
                used = 0
                continue
            count = max(1, count)
            portion = remaining[:count]
            current.append((page_label, "\n".join(portion)))
            used += label_height + 38 * len(portion)
            remaining = remaining[count:]
            if remaining:
                pages.append(current)
                current = []
                used = 0
                continued = True
        if len(section) == 3:
            pages.append(current)
            current = [(label + "（图片）", "", section[2])]
            used = 44 + 30 * len(wrap(label + "（图片）", label_font, 728)) + 340 + 18
    if current or not pages:
        pages.append(current)
    return [
        render(
            title,
            subtitle + (f" · 第 {index + 1}/{len(pages)} 页" if len(pages) > 1 else ""),
            content,
            badge,
            avatar_path=avatar_path,
            participation_counts=participation_counts,
            bold_title=bold_title,
        )
        for index, content in enumerate(pages)
    ]
