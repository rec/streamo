#!/usr/bin/env python3
import random
import re
import sys
import tempfile
from pathlib import Path

import tyro
from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel
from reccy.runtime.process import run_silent

BLACK = Path("__black__")
IMAGE_SUFFIXES = {".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
MARKDOWN_SUFFIXES = {".markdown", ".md"}
MAX_XFADE_DURATION = 59.999


class Media(BaseModel):
    path: Path
    duration: float
    is_still: bool = False


class Scene(BaseModel):
    media: Media
    duration: float


class Transition(BaseModel):
    duration: float


class TitleEvent(BaseModel):
    start: float
    duration: float


class RenderPlan(BaseModel):
    scenes: list[Scene]
    transitions: list[Transition]
    title_events: list[TitleEvent]


class TitleLine(BaseModel):
    text: str = ""
    level: int = 0
    bullet: bool = False
    blank: bool = False


class Render(BaseModel, frozen=True):
    inputs: list[Path]
    output: Path
    duration: float = 3600.0
    seed: int | None = None
    title_card: Path | None = None
    width: int = 640
    height: int = 360
    fps: int = 24
    work_scale: int = 2
    work_fps: int = 30
    still_duration: float = 30.0
    start_black_duration: float = 8.0
    title_interval: float = 180.0
    title_jitter: float = 30.0
    title_duration: float = 8.0
    title_fade: float = 4.0


def render(config: Render) -> None:
    validate_config(config)
    if config.title_card is not None and is_markdown(config.title_card):
        with tempfile.TemporaryDirectory(prefix="streamo-title-") as directory:
            title_card = Path(directory) / "title-card.png"
            render_markdown_title_card(
                config.title_card,
                title_card,
                width=work_width(config),
                height=work_height(config),
            )
            render_prepared(config.model_copy(update={"title_card": title_card}))
    else:
        render_prepared(config)


def render_prepared(config: Render) -> None:
    media = [probe_media(path, config.still_duration) for path in config.inputs]
    plan = build_plan(config, media)
    print_render_schedule(config, plan)
    command = ffmpeg_command(config, plan)
    run_silent(command)


def validate_config(config: Render) -> None:
    if not config.inputs:
        sys.exit("at least one input is required")
    if config.title_card is not None and not config.title_card.exists():
        sys.exit(f"{config.title_card} does not exist")
    if config.duration <= 0:
        sys.exit("duration must be positive")
    if (
        config.width <= 0
        or config.height <= 0
        or config.fps <= 0
        or config.work_scale <= 0
        or config.work_fps <= 0
    ):
        sys.exit("width, height, fps, work_scale, and work_fps must be positive")
    if config.still_duration <= 0:
        sys.exit("still_duration must be positive")
    if config.title_interval <= 0 or config.title_jitter < 0:
        sys.exit(
            "title_interval must be positive and title_jitter must not be negative"
        )
    if config.title_duration <= 0 or config.title_fade < 0:
        sys.exit("title_duration must be positive and title_fade must not be negative")


def is_markdown(path: Path | None) -> bool:
    return path is not None and path.suffix.lower() in MARKDOWN_SUFFIXES


def render_markdown_title_card(
    input_path: Path, output_path: Path, *, width: int, height: int
) -> None:
    image = Image.new("RGB", (width, height), color=(8, 8, 10))
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
        if match := re.match(r"^(#{1,6})\s+(.+)$", line):
            lines.append(
                TitleLine(
                    text=clean_markdown_text(match.group(2)),
                    level=len(match.group(1)),
                )
            )
        elif match := re.match(r"^[-*+]\s+(.+)$", line):
            lines.append(
                TitleLine(text=clean_markdown_text(match.group(1)), bullet=True)
            )
        elif match := re.match(r"^\d+[.)]\s+(.+)$", line):
            lines.append(
                TitleLine(text=clean_markdown_text(match.group(1)), bullet=True)
            )
        else:
            lines.append(TitleLine(text=clean_markdown_text(line.lstrip("> "))))
    return lines or [TitleLine(text=input_title_fallback(text))]


def input_title_fallback(text: str) -> str:
    return text.strip() or "Streamo"


def clean_markdown_text(text: str) -> str:
    text = re.sub(r"!\[([^]]*)]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"[*_~]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def layout_title_lines(
    draw: ImageDraw.ImageDraw, lines: list[TitleLine], *, width: int, height: int
) -> list[tuple[str, ImageFont.ImageFont, int, tuple[int, int, int]]]:
    margin = max(width // 12, 32)
    max_width = width - margin * 2
    layout: list[tuple[str, ImageFont.ImageFont, int, tuple[int, int, int]]] = []

    for line in lines:
        if line.blank:
            layout.append(("", body_font(height), height // 22, (230, 230, 235)))
            continue
        font = font_for_line(line, height)
        color = (245, 245, 248) if line.level else (220, 220, 226)
        for wrapped in wrap_text(draw, line, font, max_width):
            layout.append((wrapped, font, line_height(font), color))
    return layout


def font_for_line(line: TitleLine, height: int) -> ImageFont.ImageFont:
    if line.level == 1:
        return load_font(max(height // 7, 32))
    if line.level == 2:
        return load_font(max(height // 10, 26))
    return body_font(height)


def body_font(height: int) -> ImageFont.ImageFont:
    return load_font(max(height // 15, 20))


def load_font(size: int) -> ImageFont.ImageFont:
    for path in font_paths():
        if path.exists():
            return ImageFont.truetype(path.as_posix(), size=size)
    return ImageFont.load_default(size=size)


def font_paths() -> list[Path]:
    return [
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Supplemental/Helvetica.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    ]


def wrap_text(
    draw: ImageDraw.ImageDraw,
    line: TitleLine,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    prefix = "• " if line.bullet else ""
    words = line.text.split()
    if not words:
        return [prefix.rstrip()]

    wrapped: list[str] = []
    current = prefix + words[0]
    hanging = "  " if line.bullet else ""
    for word in words[1:]:
        candidate = f"{current} {word}"
        if text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            wrapped.append(current)
            current = hanging + word
    wrapped.append(current)
    return wrapped


def line_height(font: ImageFont.ImageFont) -> int:
    _, top, _, bottom = font.getbbox("Ag")
    return int((bottom - top) * 1.35)


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    return int(draw.textlength(text, font=font))


def probe_media(path: Path, still_duration: float) -> Media:
    if not path.exists():
        sys.exit(f"{path} does not exist")
    is_still = path.suffix.lower() in IMAGE_SUFFIXES
    duration = still_duration if is_still else probe_duration(path)
    if duration <= 0:
        sys.exit(f"{path} has no positive duration")
    return Media(path=path, duration=duration, is_still=is_still)


def probe_duration(path: Path) -> float:
    result = run_silent(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nokey=1:noprint_wrappers=1",
            path.as_posix(),
        ],
        text=True,
    )
    return float(result.stdout.strip())


def build_plan(config: Render, media: list[Media]) -> RenderPlan:
    rng = random.Random(config.seed)
    scenes = [
        Scene(
            media=black_media(config.start_black_duration),
            duration=config.start_black_duration,
        )
    ]
    transitions: list[Transition] = []
    title_events: list[TitleEvent] = []

    if config.title_card is not None:
        title = Media(
            path=config.title_card, duration=config.title_duration, is_still=True
        )
        scenes.append(Scene(media=title, duration=config.title_duration))
        transitions.append(
            Transition(duration=_clamp_fade(config.title_fade, scenes[-2], scenes[-1]))
        )
        stretch_scenes_for_transitions(scenes, transitions)
        scenes.append(
            Scene(
                media=black_media(config.start_black_duration),
                duration=config.start_black_duration,
            )
        )
        transitions.append(
            Transition(duration=_clamp_fade(config.title_fade, scenes[-2], scenes[-1]))
        )
        stretch_scenes_for_transitions(scenes, transitions)

    title_overlay_start = timeline_duration(scenes, transitions)
    current = scenes[-1]
    while timeline_duration(scenes, transitions) < config.duration:
        next_media = choose_media(rng, media, current.media)
        next_scene = Scene(media=next_media, duration=next_media.duration)
        transition = Transition(duration=fade_duration(current, next_scene))
        scenes.append(next_scene)
        transitions.append(transition)
        stretch_scenes_for_transitions(scenes, transitions)
        current = next_scene

    if config.title_card is not None:
        title_events = title_schedule(config, rng, earliest_start=title_overlay_start)

    return RenderPlan(scenes=scenes, transitions=transitions, title_events=title_events)


def title_schedule(
    config: Render, rng: random.Random, *, earliest_start: float
) -> list[TitleEvent]:
    events: list[TitleEvent] = []
    nominal = config.title_interval
    while nominal < config.duration:
        jitter = rng.uniform(-config.title_jitter, config.title_jitter)
        start = max(earliest_start, nominal + jitter)
        if start < config.duration:
            events.append(TitleEvent(start=start, duration=config.title_duration))
        nominal += config.title_interval
    return events


def choose_media(rng: random.Random, media: list[Media], current: Media) -> Media:
    choices = [m for m in media if m.path != current.path]
    return rng.choice(choices or media)


def fade_duration(current: Scene, next_scene: Scene) -> float:
    return min(
        MAX_XFADE_DURATION,
        max(natural_duration(current), natural_duration(next_scene)) / 2,
    )


def natural_duration(scene: Scene) -> float:
    return scene.media.duration


def stretch_scenes_for_transitions(
    scenes: list[Scene], transitions: list[Transition]
) -> None:
    for index, scene in enumerate(scenes):
        overlap_duration = 0.0
        if index > 0:
            overlap_duration += transitions[index - 1].duration
        if index < len(transitions):
            overlap_duration += transitions[index].duration
        scene.duration = max(scene.duration, natural_duration(scene), overlap_duration)


def _clamp_fade(fade: float, current: Scene, next_scene: Scene) -> float:
    limit = min(current.duration, next_scene.duration) / 2
    return max(0.0, min(fade, limit))


def timeline_duration(scenes: list[Scene], transitions: list[Transition]) -> float:
    return sum(s.duration for s in scenes) - sum(t.duration for t in transitions)


def print_render_schedule(config: Render, plan: RenderPlan) -> None:
    for start, scene in scene_start_times(plan):
        if scene.media.path != BLACK:
            print(f"{format_time(start)} {scene.media.path.name}")
    if config.title_card is not None:
        for event in plan.title_events:
            print(f"{format_time(event.start)} {config.title_card.name}")


def scene_start_times(plan: RenderPlan) -> list[tuple[float, Scene]]:
    starts = [(0.0, plan.scenes[0])]
    elapsed = plan.scenes[0].duration
    for index, scene in enumerate(plan.scenes[1:], start=1):
        transition = plan.transitions[index - 1]
        start = max(0.0, elapsed - transition.duration)
        starts.append((start, scene))
        elapsed += scene.duration - transition.duration
    return starts


def format_time(seconds: float) -> str:
    milliseconds = int(round(seconds * 1000))
    minutes, milliseconds = divmod(milliseconds, 60_000)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{minutes}:{seconds:02}.{milliseconds:03}"


def black_media(duration: float) -> Media:
    return Media(path=BLACK, duration=duration, is_still=True)


def work_width(config: Render) -> int:
    return config.width * config.work_scale


def work_height(config: Render) -> int:
    return config.height * config.work_scale


def ffmpeg_command(config: Render, plan: RenderPlan) -> list[str]:
    command = ["ffmpeg", "-hide_banner", "-y"]

    for scene in plan.scenes:
        command.extend(input_args(scene))

    for event in plan.title_events:
        if config.title_card is not None:
            command.extend(
                [
                    "-loop",
                    "1",
                    "-t",
                    f"{event.duration:.6f}",
                    "-i",
                    config.title_card.as_posix(),
                ]
            )

    filter_complex, output_label = filter_graph(config, plan)
    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            output_label,
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-t",
            f"{config.duration:.6f}",
            config.output.as_posix(),
        ]
    )
    return command


def input_args(scene: Scene) -> list[str]:
    duration = f"{scene.duration:.6f}"
    if scene.media.path == BLACK:
        return [
            "-f",
            "lavfi",
            "-t",
            duration,
            "-i",
            f"color=c=black:s=16x16:r=1:d={duration}",
        ]
    if scene.media.is_still:
        return ["-loop", "1", "-t", duration, "-i", scene.media.path.as_posix()]
    return ["-stream_loop", "-1", "-t", duration, "-i", scene.media.path.as_posix()]


def filter_graph(config: Render, plan: RenderPlan) -> tuple[str, str]:
    filters: list[str] = []
    for index, scene in enumerate(plan.scenes):
        filters.append(normalize_filter(index, scene, config))

    current_label = "v0"
    elapsed = plan.scenes[0].duration
    for index, transition in enumerate(plan.transitions, start=1):
        offset = max(0.0, elapsed - transition.duration)
        next_label = f"x{index}"
        filters.append(
            f"[{current_label}][v{index}]"
            f"xfade=transition=fade:duration={transition.duration:.6f}:"
            f"offset={offset:.6f}[{next_label}]"
        )
        current_label = next_label
        elapsed += plan.scenes[index].duration - transition.duration

    title_input = len(plan.scenes)
    for index, event in enumerate(plan.title_events):
        title_label = f"title{index}"
        filters.append(title_filter(config, title_input + index, event, title_label))
        next_label = f"overlay{index}"
        filters.append(
            f"[{current_label}][{title_label}]"
            f"overlay=(W-w)/2:(H-h)/2:eof_action=pass[{next_label}]"
        )
        current_label = next_label

    filters.append(
        f"[{current_label}]"
        f"scale={config.width}:{config.height},"
        f"fps={config.fps},format=yuv420p[out]"
    )
    return ";".join(filters), "[out]"


def normalize_filter(index: int, scene: Scene, config: Render) -> str:
    return (
        f"[{index}:v]"
        f"scale={work_width(config)}:{work_height(config)}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={work_width(config)}:{work_height(config)}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={config.work_fps},format=yuv420p,"
        f"trim=duration={scene.duration:.6f},setpts=PTS-STARTPTS[v{index}]"
    )


def title_filter(
    config: Render, input_index: int, event: TitleEvent, output_label: str
) -> str:
    fade_out_start = max(0.0, event.duration - config.title_fade)
    return (
        f"[{input_index}:v]"
        f"scale={work_width(config)}:{work_height(config)}:"
        "force_original_aspect_ratio=decrease,"
        f"pad={work_width(config)}:{work_height(config)}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={config.work_fps},"
        "format=rgba,"
        f"fade=t=in:st=0:d={config.title_fade:.6f}:alpha=1,"
        f"fade=t=out:st={fade_out_start:.6f}:d={config.title_fade:.6f}:alpha=1,"
        f"setpts=PTS-STARTPTS+{event.start:.6f}/TB[{output_label}]"
    )


def main() -> None:
    render(tyro.cli(Render))


if __name__ == "__main__":
    main()
