import random
from math import isfinite
from pathlib import Path
from typing import Annotated, Self

import tyro
from pydantic import BaseModel, Field, field_validator, model_validator

BLACK = Path('__black__')


MAX_XFADE_DURATION = 59.999


class Media(BaseModel):
    path: Path
    duration: float = Field(gt=0, allow_inf_nan=False)
    is_still: bool = False


class Scene(BaseModel):
    media: Media
    duration: float = Field(gt=0, allow_inf_nan=False)


class Transition(BaseModel):
    duration: float = Field(ge=0, le=MAX_XFADE_DURATION, allow_inf_nan=False)


class TitleEvent(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    duration: float = Field(gt=0, allow_inf_nan=False)


class Render(BaseModel, frozen=True):
    inputs: list[Path]
    output: Path | None = None
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
    plan: Annotated[bool, tyro.conf.arg(aliases=['-p'])] = False
    plan_only: Annotated[bool, tyro.conf.arg(aliases=['-P'])] = False

    @field_validator(
        'duration',
        'still_duration',
        'start_black_duration',
        'title_interval',
        'title_duration',
    )
    @classmethod
    def positive_time(cls, value: float) -> float:
        if not isfinite(value) or value <= 0:
            raise ValueError('time must be finite and positive')
        return value

    @field_validator('title_jitter', 'title_fade')
    @classmethod
    def nonnegative_time(cls, value: float) -> float:
        if not isfinite(value) or value < 0:
            raise ValueError('time must be finite and nonnegative')
        return value


class RenderPlan(BaseModel):
    render: Render | None = None
    scenes: list[Scene]
    transitions: list[Transition] = Field(default_factory=list)
    title_events: list[TitleEvent] = Field(default_factory=list)

    @model_validator(mode='after')
    def valid_timeline(self) -> Self:
        if not self.scenes or len(self.transitions) != len(self.scenes) - 1:
            raise ValueError('plan needs scenes and one transition per adjacent pair')
        for index, scene in enumerate(self.scenes):
            before = self.transitions[index - 1].duration if index else 0
            after = (
                self.transitions[index].duration if index < len(self.transitions) else 0
            )
            if before + after > scene.duration:
                raise ValueError('transition overlaps exceed scene duration')
        if self.render is not None:
            if timeline_duration(self.scenes, self.transitions) < self.render.duration:
                raise ValueError('scene timeline is shorter than the render duration')
            if self.title_events and self.render.title_card is None:
                raise ValueError('title events require a title card')
            if any(
                e.start + e.duration > self.render.duration for e in self.title_events
            ):
                raise ValueError('title event extends outside the render duration')
        return self


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
    elapsed = title_overlay_start
    while elapsed < config.duration:
        next_media = choose_media(rng, media, current.media)
        next_scene = Scene(media=next_media, duration=next_media.duration)
        transition = Transition(duration=fade_duration(current, next_scene))
        previous_duration = current.duration
        incoming = transitions[-1].duration if transitions else 0
        current.duration = max(current.duration, incoming + transition.duration)
        next_scene.duration = max(next_scene.duration, transition.duration)
        elapsed += (
            current.duration
            - previous_duration
            + next_scene.duration
            - transition.duration
        )
        scenes.append(next_scene)
        transitions.append(transition)
        current = next_scene

    if config.title_card is not None:
        title_events = title_schedule(config, rng, earliest_start=title_overlay_start)

    return RenderPlan(
        render=config.model_copy(update={'plan': False, 'plan_only': False}),
        scenes=scenes,
        transitions=transitions,
        title_events=title_events,
    )


def title_schedule(
    config: Render, rng: random.Random, *, earliest_start: float
) -> list[TitleEvent]:
    events: list[TitleEvent] = []
    nominal = config.title_interval
    while nominal < config.duration:
        jitter = rng.uniform(-config.title_jitter, config.title_jitter)
        start = max(earliest_start, nominal + jitter)
        if start < config.duration:
            events.append(
                TitleEvent(
                    start=start,
                    duration=min(config.title_duration, config.duration - start),
                )
            )
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


def black_media(duration: float) -> Media:
    return Media(path=BLACK, duration=duration, is_still=True)


def work_width(config: Render) -> int:
    return config.width * config.work_scale


def work_height(config: Render) -> int:
    return config.height * config.work_scale
