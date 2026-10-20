"""Image transform descriptions for a configured photo sequence."""

from pydantic import BaseModel, ConfigDict, Field


class ImageTransform(BaseModel, frozen=True):
    """Reach this state over duration seconds, holding the first state initially.

    Offset is in pixels from page center; rotation is clockwise in radians.
    """

    duration: float = Field(ge=0)
    offset: tuple[int, int] | None = None
    zoom: float | None = Field(default=None, gt=0)
    opacity: float | None = Field(default=None, ge=0, le=1)
    rotation: float | None = None

    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
