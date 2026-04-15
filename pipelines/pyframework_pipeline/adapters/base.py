from typing import Protocol


class FrameworkAdapter(Protocol):
    """Minimal adapter contract for framework-specific pipeline steps."""

    framework_id: str

    def describe(self) -> str:
        """Return a human-readable adapter description."""
