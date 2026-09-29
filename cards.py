"""Draw matching pastel QQ message cards using Pillow, without a browser."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

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
    title: str, subtitle: str, sections: list[tuple[str, str]], badge: str = "喵喵抽奖"
) -> bytes:
    """Render a complete cat-themed PNG with dynamically sized text blocks.

    Args:
        title: Primary message, such as registration or drawing results.
        subtitle: Secondary context, including the activity identifier.
        sections: Label and text pairs for rules, commands, or winner rows.
        badge: Short category printed above the title.

    Returns:
        PNG bytes suitable for AstrBot Image.fromBytes and OneBot base64.
    """
    title_font = ImageFont.truetype(str(FONT_PATH), 38)
    body_font = ImageFont.truetype(str(FONT_PATH), 25)
    label_font = ImageFont.truetype(str(FONT_PATH), 21)
    small_font = ImageFont.truetype(str(FONT_PATH), 18)
    title_lines = wrap(title, title_font, 570)
    subtitle_lines = wrap(subtitle, small_font, 565)
    header_height = 136 + len(title_lines) * 51 + len(subtitle_lines) * 29
    blocks = []
    for label, text in sections:
        lines = wrap(text, body_font, 712)
        labels = wrap(label, label_font, 728)
        blocks.append((labels, lines, 50 + 28 * len(labels) + 37 * len(lines)))
    height = header_height + sum(block[2] + 16 for block in blocks) + 86
    canvas = Image.new("RGB", (860, height), "#f5faff")
    draw = ImageDraw.Draw(canvas)
    # A restrained blue-to-pink header matches the Page's stationery palette.
    for y in range(header_height):
        ratio = y / max(header_height, 1)
        draw.line(
            (0, y, 860, y),
            fill=(int(223 + ratio * 26), int(241 - ratio * 6), int(255 - ratio * 8)),
        )
    draw.ellipse((-100, -140, 195, 150), fill="#e6f2ff")
    draw.rounded_rectangle(
        (42, 30, 42 + int(small_font.getlength(badge)) + 34, 64),
        radius=17,
        fill="#ffffff",
    )
    draw.text((59, 34), badge, font=small_font, fill="#536d9d")
    y = 84
    for line in title_lines:
        draw.text((46, y), line, font=title_font, fill="#2e4166")
        y += 51
    for line in subtitle_lines:
        draw.text((48, y + 10), line, font=small_font, fill="#63748e")
        y += 29
    # Use the generated brand mark consistently in the Page and message cards.
    draw.rounded_rectangle((669, 48, 831, 210), radius=22, fill="#ffffff")
    with Image.open(LOGO_PATH) as logo:
        logo.thumbnail((150, 150), Image.Resampling.LANCZOS)
        canvas.paste(logo.convert("RGB"), (675, 54))
    y = header_height
    for labels, lines, block_height in blocks:
        draw.rounded_rectangle(
            (32, y, 828, y + block_height), radius=22, fill="#dbe8f4"
        )
        draw.rounded_rectangle(
            (32, y - 2, 828, y + block_height - 2), radius=22, fill="#ffffff"
        )
        draw.rounded_rectangle((49, y + 23, 54, y + 46), radius=2, fill="#d8a9c8")
        for number, label in enumerate(labels):
            draw.text(
                (67, y + 15 + number * 28), label, font=label_font, fill="#7a6489"
            )
        line_y = y + 26 + len(labels) * 28
        for line in lines:
            draw.text((65, line_y), line, font=body_font, fill="#334968")
            line_y += 37
        y += block_height + 16
    draw.line((46, height - 59, 814, height - 59), fill="#dce8f3", width=1)
    draw.text(
        (49, height - 44),
        "喵喵抽奖  ·  每一份期待，都认真收好",
        font=small_font,
        fill="#7586a2",
    )
    output = BytesIO()
    canvas.save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_pages(
    title: str, subtitle: str, sections: list[tuple[str, str]], badge: str = "喵喵抽奖"
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
    body_font = ImageFont.truetype(str(FONT_PATH), 25)
    label_font = ImageFont.truetype(str(FONT_PATH), 21)
    title_font = ImageFont.truetype(str(FONT_PATH), 38)
    small_font = ImageFont.truetype(str(FONT_PATH), 18)
    # Reserve an extra subtitle line for page numbering before splitting blocks.
    header = (
        176
        + len(wrap(title, title_font, 570)) * 51
        + len(wrap(subtitle, small_font, 565)) * 29
    )
    pages: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    used = 0
    for label, text in sections:
        lines = wrap(text, body_font, 712)
        remaining = lines
        continued = False
        while remaining:
            page_label = label + ("（续）" if continued else "")
            label_height = 50 + 28 * len(wrap(page_label, label_font, 728)) + 16
            count = int((2300 - header - 86 - used - label_height) // 37)
            if count < 1 and current:
                pages.append(current)
                current = []
                used = 0
                continue
            count = max(1, count)
            portion = remaining[:count]
            current.append((page_label, "\n".join(portion)))
            used += label_height + 37 * len(portion)
            remaining = remaining[count:]
            if remaining:
                pages.append(current)
                current = []
                used = 0
                continued = True
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
