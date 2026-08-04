"""Media: perceptual hashing of posted imagery, and content generation.

Re-exports from `generator` are lazy, and deliberately so. `generator` imports
the metric registry, and `metrics.community` imports `media.phash` — so eagerly
importing the generator here makes `import botsensai.metrics` a cycle that fails
at interpreter level. Deferring the import to attribute access keeps
`from botsensai.media import ContentGenerator` working for every existing caller
while letting a metric reach the hashing code.
"""

from typing import TYPE_CHECKING, Any

__all__ = ["FORBIDDEN_PATTERNS", "ContentGenerator", "Evidence"]

if TYPE_CHECKING:  # pragma: no cover - import-time only
    from botsensai.media.generator import FORBIDDEN_PATTERNS, ContentGenerator, Evidence


def __getattr__(name: str) -> Any:
    if name in __all__:
        from botsensai.media import generator

        return getattr(generator, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
