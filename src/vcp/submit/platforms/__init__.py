"""Platform registry. Importing this package registers the built-in platforms."""

from vcp.submit.platforms.base import (
    PLATFORMS,
    Platform,
    PlatformList,
    PlatformSubmission,
    Runner,
    UploadResult,
    as_platform_list,
    default_runner,
    get_platform,
    merge_warnings,
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
    "PlatformList",
    "PlatformSubmission",
    "Runner",
    "UploadResult",
    "as_platform_list",
    "default_runner",
    "get_platform",
    "merge_warnings",
    "redact",
    "register_platform",
]
