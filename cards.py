"""Draw matching pastel QQ message cards using Pillow, without a browser."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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
                lines.append(line)
                line = char
            else:
                line += char
        lines.append(line)
    return lines or [""]


def render(
    title: str, subtitle: str, sections: list[CardSection], badge: str = "喵喵抽奖"
) -> bytes:
    """Render a complete cat-themed PNG with dynamically sized text blocks.

    Args:
        title: Primary message, such as registration or drawing results.
        subtitle: Secondary context, including the activity identifier.
        sections: Labels and text, optionally followed by a sanitized artwork path.
        badge: Short category printed above the title.

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
    y = 91
    for line in title_lines:
        draw.text((46, y), line, font=title_font, fill="#923750", anchor="lt")
        y += 54
    for line in subtitle_lines:
        draw.text((48, y + 14), line, font=small_font, fill="#956070", anchor="lt")
        y += 29
    # Use the generated brand mark consistently in the Page and message cards.
    draw.rounded_rectangle((672, 40, 824, 192), radius=24, fill="#ffffff")
    with Image.open(LOGO_PATH) as logo:
        logo.thumbnail((138, 138), Image.Resampling.LANCZOS)
        canvas.paste(logo.convert("RGB"), (679, 47))
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
            if command:
                draw.rounded_rectangle(
                    (
                        59,
                        line_y - 5,
                        59 + min(735, int(body_font.getlength(line)) + 16),
                        line_y + 30,
                    ),
                    radius=8,
                    fill="#fff0f5",
                )
            draw.text(
                (65, line_y),
                line,
                font=body_font,
                fill="#b83b65" if command else "#633c47",
                anchor="lt",
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
    title: str, subtitle: str, sections: list[CardSection], badge: str = "喵喵抽奖"
) -> list[bytes]:
    """Paginate complete content so large winner lists remain readable in QQ.

    Args:
        title: Primary message title.
        subtitle: Activity and identity context.
        sections: Complete labeled content, including long participant lists.
        badge: Category shown on every page.

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
        )
        for index, content in enumerate(pages)
    ]
