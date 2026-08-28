"""Infrastructure adapters for durable package-management state."""

from .json_registry import (
    JsonPluginRegistry,
    RegistryNotInitializedError,
    RegistryRevisionConflict,
)
from .removal_runtime import (
    build_candidate_removal_coordinator,
    candidate_removal_coordinator,
    deferred_profile_cleanup_record_path,
)

__all__ = [
    "JsonPluginRegistry",
    "RegistryNotInitializedError",
    "RegistryRevisionConflict",
    "build_candidate_removal_coordinator",
    "candidate_removal_coordinator",
    "deferred_profile_cleanup_record_path",
]
