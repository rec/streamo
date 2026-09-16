#!/usr/bin/env python3
import json
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import cast

import tyro
from pydantic import ValidationError
from reccy.runtime.process import run_silent

from .media_output import new_media_output
from .render_plan import (
    BLACK,
    Media,
    Render,
    RenderPlan,
    Scene,
    TitleEvent,
    build_plan,
    work_height,
    work_width,
)
from .title_card import is_markdown, render_markdown_title_card

IMAGE_SUFFIXES = {'.avif', '.bmp', '.gif', '.jpeg', '.jpg', '.png', '.webp'}


def render(config: Render) -> None:
    if config.inputs and config.inputs[0].suffix.lower() == '.toml':
        render_plan_file(config)
        return
    validate_config(config)
    media = [probe_media(path, config.still_duration) for path in config.inputs]
    plan = build_plan(config, media)
    if config.plan or config.plan_only:
        print(plan_toml(plan), end='')
    if config.plan_only:
        return
    execute_plan(config, plan)


def render_plan_file(config: Render) -> None:
    if len(config.inputs) != 1:
        sys.exit('a render plan must be the only input')
    if config.plan or config.plan_only:
        sys.exit('--plan and --plan-only cannot be used with a render plan')
    path = config.inputs[0]
    try:
        plan = RenderPlan.model_validate(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        sys.exit(f'{path} is not a valid render plan: {error}')
    if plan.render is None:
        sys.exit(f'{path} does not contain render settings')
    base = path.expanduser().resolve().parent
    settings = plan.render
    plan.render = settings.model_copy(
        update={
            'inputs': [(base / p.expanduser()).resolve() for p in settings.inputs],
            'output': None
            if settings.output is None
            else (base / settings.output.expanduser()).resolve(),
            'title_card': None
            if settings.title_card is None
            else (base / settings.title_card.expanduser()).resolve(),
        }
    )
    for scene in plan.scenes:
        if scene.media.path != BLACK:
            scene.media.path = (base / scene.media.path.expanduser()).resolve()
    validate_config(plan.render)
    execute_plan(plan.render, plan)


def execute_plan(config: Render, plan: RenderPlan) -> None:
    if config.title_card is not None and is_markdown(config.title_card):
        with tempfile.TemporaryDirectory(prefix='streamo-title-') as directory:
            title_card = Path(directory) / 'title-card.png'
            render_markdown_title_card(
                config.title_card,
                title_card,
                width=work_width(config),
                height=work_height(config),
            )
            prepared_config = config.model_copy(update={'title_card': title_card})
            prepared_plan = plan.model_copy(
                update={
                    'scenes': [
                        s
                        if s.media.path != config.title_card
                        else s.model_copy(
                            update={
                                'media': s.media.model_copy(update={'path': title_card})
                            }
                        )
                        for s in plan.scenes
                    ]
                }
            )
            execute_prepared_plan(prepared_config, prepared_plan)
    else:
        execute_prepared_plan(config, plan)


def execute_prepared_plan(config: Render, plan: RenderPlan) -> None:
    print_render_schedule(config, plan)
    assert config.output is not None
    with new_media_output(config.output) as temporary:
        command = ffmpeg_command(config.model_copy(update={'output': temporary}), plan)
        index = command.index('-filter_complex')
        graph = temporary.with_suffix('.filters')
        graph.write_text(command[index + 1])
        command[index : index + 2] = ['-filter_complex_script', str(graph)]
        run_silent(command)


def validate_config(config: Render) -> None:
    if not config.inputs:
        sys.exit('at least one input is required')
    if config.output is None:
        sys.exit('output is required')
    if config.title_card is not None and not config.title_card.exists():
        sys.exit(f'{config.title_card} does not exist')
    if config.duration <= 0:
        sys.exit('duration must be positive')
    if (
        config.width <= 0
        or config.height <= 0
        or config.fps <= 0
        or config.work_scale <= 0
        or config.work_fps <= 0
    ):
        sys.exit('width, height, fps, work_scale, and work_fps must be positive')
    if config.still_duration <= 0:
        sys.exit('still_duration must be positive')
    if config.title_interval <= 0 or config.title_jitter < 0:
        sys.exit(
            'title_interval must be positive and title_jitter must not be negative'
        )
    if config.title_duration <= 0 or config.title_fade < 0:
        sys.exit('title_duration must be positive and title_fade must not be negative')


def probe_media(path: Path, still_duration: float) -> Media:
    if not path.exists():
        sys.exit(f'{path} does not exist')
    is_still = path.suffix.lower() in IMAGE_SUFFIXES
    duration = still_duration if is_still else probe_duration(path)
    if duration <= 0:
        sys.exit(f'{path} has no positive duration')
    return Media(path=path, duration=duration, is_still=is_still)


def probe_duration(path: Path) -> float:
    result = run_silent(
        [
            'ffprobe',
            '-v',
            'error',
            '-select_streams',
            'v:0',
            '-show_entries',
            'format=duration',
            '-of',
            'default=nokey=1:noprint_wrappers=1',
            path.as_posix(),
        ],
        text=True,
    )
    return float(cast(str, result.stdout).strip())


def print_render_schedule(config: Render, plan: RenderPlan) -> None:
    for start, scene in scene_start_times(plan):
        if scene.media.path != BLACK:
            print(f'{format_time(start)} {scene.media.path.name}')
    if config.title_card is not None:
        for event in plan.title_events:
            print(f'{format_time(event.start)} {config.title_card.name}')


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
    return f'{minutes}:{seconds:02}.{milliseconds:03}'


def plan_toml(plan: RenderPlan) -> str:
    if plan.render is None:
        raise ValueError('render settings are required to write a plan')
    render = plan.render.model_dump(
        mode='json', exclude={'plan', 'plan_only'}, exclude_none=True
    )
    render['inputs'] = [p.expanduser().resolve().as_posix() for p in plan.render.inputs]
    for name in ('output', 'title_card'):
        if (path := getattr(plan.render, name)) is not None:
            render[name] = path.expanduser().resolve().as_posix()
    lines = ['[render]']
    lines.extend(f'{key} = {toml_value(value)}' for key, value in render.items())
    for scene in plan.scenes:
        media_path = scene.media.path
        if media_path != BLACK:
            media_path = media_path.expanduser().resolve()
        lines.extend(
            [
                '',
                '[[scenes]]',
                f'duration = {toml_value(scene.duration)}',
                '[scenes.media]',
                f'path = {toml_value(media_path)}',
                f'duration = {toml_value(scene.media.duration)}',
                f'is_still = {toml_value(scene.media.is_still)}',
            ]
        )
    for transition in plan.transitions:
        lines.extend(
            [
                '',
                '[[transitions]]',
                f'duration = {toml_value(transition.duration)}',
            ]
        )
    for event in plan.title_events:
        lines.extend(
            [
                '',
                '[[title_events]]',
                f'start = {toml_value(event.start)}',
                f'duration = {toml_value(event.duration)}',
            ]
        )
    return '\n'.join(lines) + '\n'


def toml_value(value: object) -> str:
    if isinstance(value, Path):
        return json.dumps(value.as_posix(), ensure_ascii=False)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return f'[{", ".join(toml_value(item) for item in value)}]'
    raise TypeError(f'unsupported TOML value {value!r}')


def ffmpeg_command(config: Render, plan: RenderPlan) -> list[str]:
    assert config.output is not None
    command = ['ffmpeg', '-hide_banner', '-y']

    for scene in plan.scenes:
        command.extend(input_args(scene))

    for event in plan.title_events:
        if config.title_card is not None:
            command.extend(
                [
                    '-loop',
                    '1',
                    '-t',
                    f'{event.duration:.6f}',
                    '-i',
                    config.title_card.as_posix(),
                ]
            )

    filter_complex, output_label = filter_graph(config, plan)
    command.extend(
        [
            '-filter_complex',
            filter_complex,
            '-map',
            output_label,
            '-an',
            '-c:v',
            'libx264',
            '-pix_fmt',
            'yuv420p',
            '-movflags',
            '+faststart',
            '-t',
            f'{config.duration:.6f}',
            config.output.as_posix(),
        ]
    )
    return command


def input_args(scene: Scene) -> list[str]:
    duration = f'{scene.duration:.6f}'
    if scene.media.path == BLACK:
        return [
            '-f',
            'lavfi',
            '-t',
            duration,
            '-i',
            f'color=c=black:s=16x16:r=1:d={duration}',
        ]
    if scene.media.is_still:
        return ['-loop', '1', '-t', duration, '-i', scene.media.path.as_posix()]
    return ['-stream_loop', '-1', '-t', duration, '-i', scene.media.path.as_posix()]


def filter_graph(config: Render, plan: RenderPlan) -> tuple[str, str]:
    filters: list[str] = []
    for index, scene in enumerate(plan.scenes):
        filters.append(normalize_filter(index, scene, config))

    current_label = 'v0'
    elapsed = plan.scenes[0].duration
    for index, transition in enumerate(plan.transitions, start=1):
        offset = max(0.0, elapsed - transition.duration)
        next_label = f'x{index}'
        filters.append(
            f'[{current_label}][v{index}]'
            f'xfade=transition=fade:duration={transition.duration:.6f}:'
            f'offset={offset:.6f}[{next_label}]'
        )
        current_label = next_label
        elapsed += plan.scenes[index].duration - transition.duration

    title_input = len(plan.scenes)
    for index, event in enumerate(plan.title_events):
        title_label = f'title{index}'
        filters.append(title_filter(config, title_input + index, event, title_label))
        next_label = f'overlay{index}'
        filters.append(
            f'[{current_label}][{title_label}]'
            f'overlay=(W-w)/2:(H-h)/2:eof_action=pass[{next_label}]'
        )
        current_label = next_label

    filters.append(
        f'[{current_label}]'
        f'scale={config.width}:{config.height},'
        f'fps={config.fps},format=yuv420p[out]'
    )
    return ';'.join(filters), '[out]'


def normalize_filter(index: int, scene: Scene, config: Render) -> str:
    return (
        f'[{index}:v]'
        f'scale={work_width(config)}:{work_height(config)}:'
        'force_original_aspect_ratio=decrease,'
        f'pad={work_width(config)}:{work_height(config)}:(ow-iw)/2:(oh-ih)/2,'
        f'setsar=1,fps={config.work_fps},format=yuv420p,'
        f'trim=duration={scene.duration:.6f},setpts=PTS-STARTPTS[v{index}]'
    )


def title_filter(
    config: Render, input_index: int, event: TitleEvent, output_label: str
) -> str:
    fade_out_start = max(0.0, event.duration - config.title_fade)
    return (
        f'[{input_index}:v]'
        f'scale={work_width(config)}:{work_height(config)}:'
        'force_original_aspect_ratio=decrease,'
        f'pad={work_width(config)}:{work_height(config)}:(ow-iw)/2:(oh-ih)/2,'
        f'fps={config.work_fps},'
        'format=rgba,'
        f'fade=t=in:st=0:d={config.title_fade:.6f}:alpha=1,'
        f'fade=t=out:st={fade_out_start:.6f}:d={config.title_fade:.6f}:alpha=1,'
        f'setpts=PTS-STARTPTS+{event.start:.6f}/TB[{output_label}]'
    )


def main() -> None:
    render(tyro.cli(Render))


if __name__ == '__main__':
    main()
