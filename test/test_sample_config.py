from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import SecretStr

from streamo.provider_config import (
    CustomService,
    EncodingProfile,
    RtmpIngest,
    StreamMetadata,
)

if TYPE_CHECKING:
    from reccy.pytest_plugin import SampleConfigRegression


class ExampleService(CustomService, frozen=True):
    service: Literal['custom'] = 'custom'
    ingest: RtmpIngest = RtmpIngest(
        protocol='rtmps',
        server_url='rtmps://stream.example.com/live',
        stream_key=SecretStr('example-stream-key'),
    )
    encoding: EncodingProfile = EncodingProfile()
    metadata: StreamMetadata = StreamMetadata(
        privacy='unlisted', scheduled_start=datetime(2026, 8, 12, 20, tzinfo=UTC)
    )


def test_sample_config(sample_config_regression: 'SampleConfigRegression') -> None:
    sample_config_regression(ExampleService)
