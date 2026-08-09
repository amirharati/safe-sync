"""Path filtering helpers for filesystem watch events and reconciliation."""

from __future__ import annotations

from pathlib import Path, PurePath


IGNORED_WATCH_PARTS = {
    ".safe-sync-work",
    ".git",
    "node_modules",
    ".pnpm-store",
    ".bun",
    "Pods",
    ".venv",
    "venv",
    "__pypackages__",
    ".eggs",
    "dist",
    "build",
    "out",
    "coverage",
    ".nyc_output",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".webpack",
    ".serverless",
    ".aws-sam",
    ".expo",
    ".cache",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".parcel-cache",
    ".turbo",
    ".vite",
    ".gradle",
    "target",
    ".dart_tool",
    "DerivedData",
    "CMakeFiles",
    "buck-out",
    "bazel-bin",
    "bazel-out",
    "bazel-testlogs",
    ".stack-work",
    ".cabal-sandbox",
    ".ipynb_checkpoints",
    "__pycache__",
}

IGNORED_WATCH_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".o",
    ".obj",
    ".so",
    ".dylib",
    ".dll",
    ".class",
    ".tmp",
    ".temp",
    ".swp",
    ".swo",
    ".pid",
    ".sock",
}

IGNORED_WATCH_FILES = {".DS_Store", ".fseventsd", ".packages", "Thumbs.db", "desktop.ini"}


def _has_ignored_pair(parts: tuple[str, ...]) -> bool:
    pairs = set(zip(parts, parts[1:]))
    return (".yarn", "cache") in pairs or (".yarn", "unplugged") in pairs or ("vendor", "bundle") in pairs or (".angular", "cache") in pairs


def should_ignore_watch_event(path: str | Path) -> bool:
    candidate = PurePath(path)
    parts = candidate.parts
    name = candidate.name
    if any(part in IGNORED_WATCH_PARTS or part.startswith("cmake-build-") or part.endswith(".egg-info") for part in parts):
        return True
    if _has_ignored_pair(parts):
        return True
    if name in IGNORED_WATCH_FILES or name.startswith("._") or name.startswith("~$"):
        return True
    return candidate.suffix in IGNORED_WATCH_SUFFIXES
