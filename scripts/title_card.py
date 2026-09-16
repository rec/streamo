import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel

type Font = ImageFont.ImageFont | ImageFont.FreeTypeFont

MARKDOWN_SUFFIXES = {'.markdown', '.md'}


class TitleLine(BaseModel):
    text: str = ''
    level: int = 0
    bullet: bool = False
    blank: bool = False


def is_markdown(path: Path | None) -> bool:
    return path is not None and path.suffix.lower() in MARKDOWN_SUFFIXES


def render_markdown_title_card(
    input_path: Path, output_path: Path, *, width: int, height: int
) -> None:
    image = Image.new('RGB', (width, height), color=(8, 8, 10))
    draw = ImageDraw.Draw(image)
    lines = parse_markdown_title(input_path.read_text())
    layout = layout_title_lines(draw, lines, width=width, height=height)
    y = max((height - sum(x[2] for x in layout)) // 2, height // 12)

    for text, font, line_height, color in layout:
        if text:
            x = (width - text_width(draw, text, font)) // 2
            draw.text((x, y), text, font=font, fill=color)
        y += line_height

    image.save(output_path)


def parse_markdown_title(text: str) -> list[TitleLine]:
    lines: list[TitleLine] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if lines and not lines[-1].blank:
                lines.append(TitleLine(blank=True))
            continue
        if match := re.match(r'^(#{1,6})\s+(.+)$', line):
            lines.append(
                TitleLine(
                    text=clean_markdown_text(match.group(2)),
                    level=len(match.group(1)),
                )
            )
        elif match := re.match(r'^[-*+]\s+(.+)$', line):
            lines.append(
                TitleLine(text=clean_markdown_text(match.group(1)), bullet=True)
            )
        elif match := re.match(r'^\d+[.)]\s+(.+)$', line):
            lines.append(
                TitleLine(text=clean_markdown_text(match.group(1)), bullet=True)
            )
        else:
            lines.append(TitleLine(text=clean_markdown_text(line.lstrip('> '))))
    return lines or [TitleLine(text=input_title_fallback(text))]


def input_title_fallback(text: str) -> str:
    return text.strip() or 'streamO'


def clean_markdown_text(text: str) -> str:
    text = re.sub(r'!\[([^]]*)]\([^)]+\)', r'\1', text)
    text = re.sub(r'\[([^]]+)]\([^)]+\)', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'[*_~]+', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def layout_title_lines(
    draw: ImageDraw.ImageDraw, lines: list[TitleLine], *, width: int, height: int
) -> list[tuple[str, Font, int, tuple[int, int, int]]]:
    margin = max(width // 12, 32)
    max_width = width - margin * 2
    layout: list[tuple[str, Font, int, tuple[int, int, int]]] = []

    for line in lines:
        if line.blank:
            layout.append(('', body_font(height), height // 22, (230, 230, 235)))
            continue
        font = font_for_line(line, height)
        color = (245, 245, 248) if line.level else (220, 220, 226)
        for wrapped in wrap_text(draw, line, font, max_width):
            layout.append((wrapped, font, line_height(font), color))
    return layout


def font_for_line(line: TitleLine, height: int) -> Font:
    if line.level == 1:
        return load_font(max(height // 7, 32))
    if line.level == 2:
        return load_font(max(height // 10, 26))
    return body_font(height)


def body_font(height: int) -> Font:
    return load_font(max(height // 15, 20))


def load_font(size: int) -> Font:
    for path in font_paths():
        if path.exists():
            return ImageFont.truetype(path.as_posix(), size=size)
    return ImageFont.load_default(size=size)


def font_paths() -> list[Path]:
    return [
        Path('/System/Library/Fonts/Supplemental/Arial.ttf'),
        Path('/System/Library/Fonts/Supplemental/Helvetica.ttf'),
        Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'),
        Path('C:/Windows/Fonts/arial.ttf'),
    ]


def wrap_text(
    draw: ImageDraw.ImageDraw,
    line: TitleLine,
    font: Font,
    max_width: int,
) -> list[str]:
    prefix = '• ' if line.bullet else ''
    words = line.text.split()
    if not words:
        return [prefix.rstrip()]

    wrapped: list[str] = []
    current = prefix + words[0]
    hanging = '  ' if line.bullet else ''
    for word in words[1:]:
        candidate = f'{current} {word}'
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            wrapped.append(current)
            current = hanging + word
    wrapped.append(current)
    return wrapped


def line_height(font: Font) -> int:
    _, top, _, bottom = font.getbbox('Ag')
    return int((bottom - top) * 1.35)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: Font) -> int:
    return int(draw.textlength(text, font=font))
