"""Platform registry. Importing this package registers the built-in platforms."""

from vcp.submit.platforms.base import (
    PLATFORMS,
    Platform,
    PlatformSubmission,
    Runner,
    UploadResult,
    default_runner,
    get_platform,
    redact,
    register_platform,
)
from vcp.submit.platforms.kaggle import KagglePlatform
from vcp.submit.platforms.manual import ManualPlatform

register_platform(ManualPlatform())
register_platform(KagglePlatform())

__all__ = [
    "PLATFORMS",
    "KagglePlatform",
    "ManualPlatform",
    "Platform",
    "PlatformSubmission",
    "Runner",
    "UploadResult",
    "default_runner",
    "get_platform",
    "redact",
    "register_platform",
]
