import random
import subprocess as sp
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from scripts import render
from scripts.render import (
    Media,
    Render,
    RenderPlan,
    Scene,
    TitleEvent,
    Transition,
    build_plan,
    fade_duration,
    ffmpeg_command,
    filter_graph,
    format_time,
    print_render_schedule,
    render_markdown_title_card,
    scene_start_times,
    stretch_scenes_for_transitions,
    timeline_duration,
    title_schedule,
)


def test_fade_duration_uses_half_longer_video() -> None:
    first = Scene(media=Media(path=Path("a.mp4"), duration=20), duration=20)
    second = Scene(media=Media(path=Path("b.mp4"), duration=10), duration=10)

    assert fade_duration(first, second) == 10


def test_fade_duration_uses_half_longer_media_for_video_and_still() -> None:
    video = Scene(media=Media(path=Path("a.mp4"), duration=20), duration=20)
    still = Scene(
        media=Media(path=Path("b.png"), duration=30, is_still=True), duration=30
    )

    assert fade_duration(video, still) == 15
    assert fade_duration(still, video) == 15


def test_fade_duration_uses_half_longer_still_for_two_stills() -> None:
    first = Scene(
        media=Media(path=Path("a.png"), duration=30, is_still=True), duration=30
    )
    second = Scene(
        media=Media(path=Path("b.png"), duration=8, is_still=True), duration=8
    )

    assert fade_duration(first, second) == 15


def test_fade_duration_is_capped_below_ffmpeg_limit() -> None:
    first = Scene(media=Media(path=Path("a.mp4"), duration=200), duration=200)
    second = Scene(media=Media(path=Path("b.mp4"), duration=10), duration=10)

    assert fade_duration(first, second) == render.MAX_XFADE_DURATION


def test_build_plan_is_seeded_and_avoids_immediate_repeats() -> None:
    config = Render(
        inputs=[Path("a.mp4"), Path("b.mp4")],
        output=Path("out.mp4"),
        duration=40,
        seed=1,
    )
    media = [
        Media(path=Path("a.mp4"), duration=10),
        Media(path=Path("b.mp4"), duration=10),
    ]

    plan = build_plan(config, media)
    paths = [scene.media.path for scene in plan.scenes[1:]]

    for first, second in zip(paths, paths[1:], strict=False):
        assert first != second
    for index, transition in enumerate(plan.transitions):
        assert plan.scenes[index].duration >= transition.duration
        assert plan.scenes[index + 1].duration >= transition.duration
    assert timeline_duration(plan.scenes, plan.transitions) >= 40


def test_stretch_scenes_loops_short_media_for_adjacent_fades() -> None:
    scenes = [
        Scene(media=Media(path=Path("a.mp4"), duration=30), duration=30),
        Scene(media=Media(path=Path("b.mp4"), duration=8), duration=8),
        Scene(media=Media(path=Path("c.mp4"), duration=20), duration=20),
    ]
    transitions = [Transition(duration=15), Transition(duration=10)]

    stretch_scenes_for_transitions(scenes, transitions)

    assert scenes[0].duration == 30
    assert scenes[1].duration == 25
    assert scenes[2].duration == 20


def test_build_plan_inserts_title_events_by_interval() -> None:
    config = Render(
        inputs=[Path("a.mp4")],
        output=Path("out.mp4"),
        duration=40,
        seed=1,
        title_card=Path("title.png"),
        title_interval=10,
        title_jitter=0,
    )
    media = [Media(path=Path("a.mp4"), duration=10)]

    plan = build_plan(config, media)

    assert [event.start for event in plan.title_events] == [16.0, 20.0, 30.0]
    assert [scene.media.path for scene in plan.scenes[:3]] == [
        Path("__black__"),
        Path("title.png"),
        Path("__black__"),
    ]


def test_scene_start_times_use_fade_start_offsets() -> None:
    plan = RenderPlan(
        scenes=[
            Scene(
                media=Media(path=Path("__black__"), duration=8, is_still=True),
                duration=8,
            ),
            Scene(media=Media(path=Path("a.mp4"), duration=10), duration=10),
            Scene(media=Media(path=Path("b.mp4"), duration=12), duration=12),
        ],
        transitions=[Transition(duration=4), Transition(duration=5)],
        title_events=[],
    )

    entries = [
        (start, scene.media.path.name) for start, scene in scene_start_times(plan)
    ]
    assert entries == [
        (0.0, "__black__"),
        (4, "a.mp4"),
        (9, "b.mp4"),
    ]


def test_title_schedule_applies_jitter_after_intro() -> None:
    config = Render(
        inputs=[Path("a.mp4")],
        output=Path("out.mp4"),
        duration=80,
        seed=1,
        title_card=Path("title.png"),
        title_interval=30,
        title_jitter=5,
    )
    rng = random.Random(1)

    events = title_schedule(config, rng, earliest_start=16)

    assert [round(event.start, 3) for event in events] == [26.344, 63.474]


def test_print_render_schedule_prints_non_black_entries_and_titles(
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = RenderPlan(
        scenes=[
            Scene(
                media=Media(path=Path("__black__"), duration=8, is_still=True),
                duration=8,
            ),
            Scene(media=Media(path=Path("a.mp4"), duration=10), duration=10),
            Scene(media=Media(path=Path("b.mp4"), duration=12), duration=12),
        ],
        transitions=[Transition(duration=4), Transition(duration=5)],
        title_events=[TitleEvent(start=11, duration=8)],
    )

    config = Render(
        inputs=[Path("a.mp4")],
        output=Path("out.mp4"),
        title_card=Path("show-title.png"),
    )

    print_render_schedule(config, plan)

    assert capsys.readouterr().out.splitlines() == [
        "0:04.000 a.mp4",
        "0:09.000 b.mp4",
        "0:11.000 show-title.png",
    ]


def test_format_time_uses_minutes_seconds_and_milliseconds() -> None:
    assert format_time(64.1434) == "1:04.143"


def test_ffmpeg_command_uses_inputs_xfade_and_title_overlay() -> None:
    config = Render(
        inputs=[Path("a.mp4"), Path("b.png")],
        output=Path("out.mp4"),
        duration=20,
        seed=1,
        title_card=Path("title.png"),
    )
    plan = RenderPlan(
        scenes=[
            Scene(
                media=Media(path=Path("__black__"), duration=4, is_still=True),
                duration=4,
            ),
            Scene(media=Media(path=Path("a.mp4"), duration=10), duration=10),
            Scene(
                media=Media(path=Path("b.png"), duration=30, is_still=True),
                duration=30,
            ),
        ],
        transitions=[Transition(duration=2), Transition(duration=5)],
        title_events=[TitleEvent(start=8, duration=8)],
    )

    command = ffmpeg_command(config, plan)
    graph = command[command.index("-filter_complex") + 1]

    assert "-y" in command
    assert "-n" not in command
    assert "a.mp4" in command
    assert "b.png" in command
    assert "title.png" in command
    assert "-stream_loop" in command
    assert "-loop" in command
    assert "scale=1280:720" in graph
    assert "fps=30" in graph
    assert "scale=640:360,fps=24,format=yuv420p[out]" in graph
    assert "xfade=transition=fade" in graph
    assert "overlay=(W-w)/2:(H-h)/2:eof_action=pass" in graph
    assert command[command.index("-t", command.index("-movflags")) + 1] == "20.000000"
    assert command[-1] == "out.mp4"


def test_filter_graph_maps_final_label() -> None:
    config = Render(
        inputs=[Path("a.mp4")],
        output=Path("out.mp4"),
        duration=5,
        seed=1,
    )
    plan = build_plan(config, [Media(path=Path("a.mp4"), duration=10)])

    graph, output = filter_graph(config, plan)

    assert "scale=1280:720" in graph
    assert "scale=640:360" in graph
    assert output == "[out]"


def test_probe_media_uses_still_duration_for_images(
    monkeypatch, tmp_path: Path
) -> None:
    image = tmp_path / "still.png"
    image.touch()
    monkeypatch.setattr(render, "probe_duration", lambda path: 999)

    media = render.probe_media(image, 30)

    assert media.duration == 30
    assert media.is_still


def test_render_markdown_title_card_writes_png(tmp_path: Path) -> None:
    source = tmp_path / "title.md"
    output = tmp_path / "title.png"
    source.write_text("# Show Title\n\n- First set\n- Second set")

    render_markdown_title_card(source, output, width=320, height=180)

    with Image.open(output) as image:
        assert image.size == (320, 180)
        assert image.format == "PNG"


def test_render_uses_temporary_png_for_markdown_title_card(
    monkeypatch, tmp_path: Path
) -> None:
    title_card = tmp_path / "title.md"
    title_card.write_text("# Show Title")
    configs: list[Render] = []

    def render_prepared(config: Render) -> None:
        assert config.title_card is not None
        configs.append(config)
        assert config.title_card.suffix == ".png"
        assert config.title_card.exists()
        with Image.open(config.title_card) as image:
            assert image.size == (1280, 720)

    monkeypatch.setattr(render, "render_prepared", render_prepared)

    render.render(
        Render(
            inputs=[Path("input.mp4")],
            output=tmp_path / "out.mp4",
            title_card=title_card,
        )
    )

    assert configs
    assert configs[0].title_card != title_card
    assert configs[0].title_card is not None
    assert not configs[0].title_card.exists()


def test_render_visual_bed_regression(tmp_path: Path) -> None:
    fixtures = Path(__file__).parent / "fixtures" / "render"
    output = tmp_path / "visual-bed.mp4"
    expected = Path(__file__).parent / "test_render" / "test_render_visual_bed.mp4"

    render_test_visual_bed(fixtures, output)

    expected_frames = decode_video_frames(expected, width=160, height=90)
    actual_frames = decode_video_frames(output, width=160, height=90)
    assert actual_frames.shape == expected_frames.shape
    assert np.abs(actual_frames.astype(int) - expected_frames.astype(int)).mean() < 0.5


def render_test_visual_bed(fixtures: Path, output: Path) -> None:
    render.render(
        visual_bed_config(
            fixtures=fixtures,
            output=output,
        )
    )


def visual_bed_config(fixtures: Path, output: Path) -> Render:
    return Render(
        inputs=[
            fixtures / "blue-circle.mp4",
            fixtures / "red-diamond.mp4",
        ],
        output=output,
        duration=6,
        seed=1,
        title_card=fixtures / "title.md",
        width=160,
        height=90,
        fps=6,
        start_black_duration=1,
        title_duration=1,
        title_fade=0.5,
        title_interval=999,
        title_jitter=0,
    )


def decode_video_frames(path: Path, *, width: int, height: int) -> np.ndarray:
    result = sp.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            path.as_posix(),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ],
        check=True,
        stdout=sp.PIPE,
    )
    return np.frombuffer(result.stdout, dtype=np.uint8).reshape((-1, height, width, 3))
