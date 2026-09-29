import base64
import enum
import io
import threading
from pathlib import Path

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field
from reccy.runtime.files import atomic_output

from .images import image_paths, load_image


class ImageDecision(enum.StrEnum):
    approved = enum.auto()
    rejected = enum.auto()


class ImageDecisions(BaseModel, frozen=True):
    decisions: dict[str, ImageDecision] = Field(default_factory=dict)

    model_config = ConfigDict(extra='forbid')


class ImageReview(BaseModel, frozen=True):
    id: str
    decision: ImageDecision = Field(strict=False)

    model_config = ConfigDict(extra='forbid', strict=True)


class ImageQueue(BaseModel, frozen=True):
    after: str = ''
    limit: int = Field(default=50, ge=1, le=100)

    model_config = ConfigDict(extra='forbid', strict=True)


class ImagePreview(BaseModel, frozen=True):
    id: str

    model_config = ConfigDict(extra='forbid', strict=True)


class ImageApproval:
    def __init__(self, image_dir: Path, required: bool) -> None:
        self.image_dir = image_dir
        self.required = required
        self.path = image_dir / '.streamo-image-approval.json'
        self.lock = threading.Lock()
        self.records = (
            ImageDecisions.model_validate_json(self.path.read_bytes())
            if self.path.exists()
            else ImageDecisions()
        )

    def allows(self, path: Path) -> bool:
        if path.parent != self.image_dir:
            return True
        with self.lock:
            decision = self.records.decisions.get(path.name)
            return decision == ImageDecision.approved or (
                decision is None and not self.required
            )

    def queue(self, request: ImageQueue) -> dict[str, object]:
        after = self.image_name(request.after) if request.after else ''
        paths = [p for p in image_paths(self.image_dir) if p.name > after]
        selected = paths[: request.limit]
        with self.lock:
            return {
                'approval_required': self.required,
                'images': [
                    {
                        'id': f'incoming/{p.name}',
                        'state': self.records.decisions.get(
                            p.name, 'pending' if self.required else 'approved'
                        ),
                    }
                    for p in selected
                ],
                'next_after': f'incoming/{selected[-1].name}'
                if len(paths) > len(selected)
                else None,
            }

    def review(self, request: ImageReview) -> dict[str, object]:
        path = self.image_path(request.id)
        with self.lock:
            records = ImageDecisions(
                decisions={**self.records.decisions, path.name: request.decision}
            )
            with atomic_output(self.path) as temporary:
                temporary.write_text(records.model_dump_json(indent=2))
            self.records = records
        return {'id': request.id, 'state': request.decision.value}

    def preview(self, request: ImagePreview) -> dict[str, object]:
        path = self.image_path(request.id)
        image = Image.fromarray(load_image(path, 320, 180))
        output = io.BytesIO()
        image.save(output, format='PNG')
        return {
            'id': request.id,
            'data_url': 'data:image/png;base64,'
            + base64.b64encode(output.getvalue()).decode(),
        }

    def image_path(self, image_id: str) -> Path:
        path = self.image_dir / self.image_name(image_id)
        if path not in image_paths(self.image_dir):
            raise ValueError('Image ID does not exist in the incoming inbox')
        return path

    @staticmethod
    def image_name(image_id: str) -> str:
        parts = image_id.split('/')
        if len(parts) != 2 or parts[0] != 'incoming' or parts[1] in {'', '.', '..'}:
            raise ValueError('Image ID must identify an incoming image')
        if Path(parts[1]).name != parts[1]:
            raise ValueError('Image ID must contain a filename')
        return parts[1]
