"""Scoped Edit Applicator — Phase 9.

For large files (over LARGE_FILE_THRESHOLD lines) the Worker requests the LLM
to return *scoped edits* instead of a complete file replacement.  A scoped edit
targets a named function/class **or** an explicit line range and supplies only
the replacement text for that region.

Schema (each edit is a dict with one of two forms):

  Function-scoped:
    { "function": "<name>", "replacement": "<new body>" }

  Line-range:
    { "lines": [<start>, <end>], "replacement": "<new text>" }

  ``replacement`` may be an empty string to delete the targeted region.

The :class:`ScopedEditApplicator` validates that the target exists in the file
before applying, and returns a ``ScopedEditResult`` indicating success or the
reason for failure.  On failure the caller (Worker) falls back to the standard
complete-content mode.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LARGE_FILE_THRESHOLD = 200  # lines; files at or above this use scoped-edit mode


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ScopedEdit:
    """A single targeted replacement within a file.

    Exactly one of ``function`` or ``lines`` is set.
    """
    replacement: str
    function: str | None = None       # target function or class name
    lines: tuple[int, int] | None = None  # 1-based [start, end] inclusive

    def is_function_scoped(self) -> bool:
        return self.function is not None

    def is_line_scoped(self) -> bool:
        return self.lines is not None


@dataclass
class ScopedEditResult:
    success: bool
    lines: list[str] = field(default_factory=list)   # result lines on success
    error: str = ""


# ---------------------------------------------------------------------------
# Parser: JSON edit list → list[ScopedEdit]
# ---------------------------------------------------------------------------

def parse_scoped_edits(raw: Any) -> list[ScopedEdit]:
    """Convert a raw (already-deserialized) JSON value into ScopedEdit objects.

    Accepts either a list of edit dicts or a dict with a top-level ``"edits"``
    key containing such a list.  Unknown keys are ignored gracefully.

    Raises
    ------
    ValueError
        If the structure is unrecognisable or a required field is missing.
    """
    if isinstance(raw, dict):
        if "edits" in raw:
            raw = raw["edits"]
        else:
            # Single edit wrapped in a dict
            raw = [raw]

    if not isinstance(raw, list):
        raise ValueError(f"Expected a list of edits, got {type(raw).__name__}")

    edits: list[ScopedEdit] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"Edit #{i} is not a dict: {item!r}")
        replacement = item.get("replacement", "")
        if not isinstance(replacement, str):
            raise ValueError(f"Edit #{i}: 'replacement' must be a string")

        if "function" in item:
            name = item["function"]
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Edit #{i}: 'function' must be a non-empty string")
            edits.append(ScopedEdit(function=name.strip(), replacement=replacement))

        elif "lines" in item:
            coords = item["lines"]
            if (
                not isinstance(coords, (list, tuple))
                or len(coords) != 2
                or not all(isinstance(c, int) for c in coords)
            ):
                raise ValueError(
                    f"Edit #{i}: 'lines' must be [start, end] integer pair, got {coords!r}"
                )
            start, end = int(coords[0]), int(coords[1])
            if start < 1 or end < start:
                raise ValueError(
                    f"Edit #{i}: invalid line range [{start}, {end}] — "
                    "start must be ≥ 1 and end ≥ start"
                )
            edits.append(ScopedEdit(lines=(start, end), replacement=replacement))

        else:
            raise ValueError(
                f"Edit #{i}: must have either 'function' or 'lines' key"
            )

    return edits


# ---------------------------------------------------------------------------
# Applicator
# ---------------------------------------------------------------------------

# Matches Python ``def <name>`` / ``class <name>`` at any indentation level.
_DEF_RE = re.compile(r"^(\s*)(?:def|class)\s+(\w+)\s*[:(]", re.MULTILINE)

# Matches JavaScript/TypeScript method or function declarations, e.g.:
#   methodName(...) {          ← class method
#   async methodName(...) {
#   function methodName(...) {
#   const methodName = (...) => {
#   methodName = (...) => {    ← class field arrow function
_JS_DEF_RE = re.compile(
    r"^(\s*)(?:"
    r"(?:async\s+)?(\w+)\s*\([^)]*\)\s*\{"           # method / function shorthand
    r"|function\s+(\w+)\s*\("                          # named function declaration
    r"|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\(?.*?\)?\s*=>"  # arrow function
    r")",
    re.MULTILINE,
)


def _find_definition_range(lines: list[str], name: str) -> tuple[int, int] | None:
    """Return the 1-based [start, end] (inclusive) line range for a function,
    method, or class definition named *name*.

    Supports Python (indentation-delimited) and JavaScript/TypeScript
    (brace-delimited).  Returns ``None`` if the name is not found.
    """
    text = "\n".join(lines)

    # Collect all candidate matches from both regimes.
    # Each entry: (start_pos_in_text, indent_len, captured_name)
    candidates: list[tuple[int, int, str]] = []

    for m in _DEF_RE.finditer(text):
        candidates.append((m.start(), len(m.group(1)), m.group(2)))

    for m in _JS_DEF_RE.finditer(text):
        # group(2) = shorthand method, group(3) = named function, group(4) = arrow
        captured = m.group(2) or m.group(3) or m.group(4)
        if captured:
            candidates.append((m.start(), len(m.group(1)), captured))

    for pos, indent, found_name in candidates:
        if found_name != name:
            continue

        start_line = text[:pos].count("\n") + 1

        # Detect language regime from the definition line
        def_line = lines[start_line - 1]
        is_brace_delimited = "{" in def_line or (
            start_line < len(lines) and "{" in lines[start_line]
        )

        end_line = start_line

        if is_brace_delimited:
            # Walk forward counting braces to find the matching closing brace.
            depth = 0
            for lineno in range(start_line, len(lines) + 1):
                raw = lines[lineno - 1]
                depth += raw.count("{") - raw.count("}")
                end_line = lineno
                if depth <= 0 and lineno > start_line:
                    break
        else:
            # Python: body ends when a non-blank, non-comment line returns to
            # the definition's indentation level or less.
            for lineno in range(start_line, len(lines) + 1):
                raw = lines[lineno - 1]
                stripped = raw.rstrip()
                if lineno == start_line:
                    end_line = lineno
                    continue
                if not stripped or stripped.lstrip().startswith("#"):
                    end_line = lineno
                    continue
                line_indent = len(raw) - len(raw.lstrip())
                if line_indent > indent:
                    end_line = lineno
                else:
                    break

        return (start_line, end_line)

    return None


class ScopedEditApplicator:
    """Applies a list of :class:`ScopedEdit` objects to a file's lines.

    Usage::

        applicator = ScopedEditApplicator(lines)
        result = applicator.apply(edits)
        if result.success:
            new_lines = result.lines
        else:
            # Fall back to complete-content mode
            ...
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def apply(self, edits: list[ScopedEdit]) -> ScopedEditResult:
        """Apply *edits* to the file lines in order.

        Each edit is validated before being applied.  If any edit fails
        validation the entire application is aborted and the original lines
        are left intact.

        Returns
        -------
        ScopedEditResult
            ``success=True`` with the new lines on success, or
            ``success=False`` with an error message on failure.
        """
        # Sort edits in reverse order so later line numbers don't shift
        # after earlier edits are applied.  Function-scoped edits are
        # resolved to line ranges first, then sorted together.
        resolved: list[tuple[int, int, str]] = []  # (start_1based, end_1based, replacement)

        current_lines = list(self._lines)

        for edit in edits:
            if edit.is_function_scoped():
                assert edit.function is not None
                rng = _find_definition_range(current_lines, edit.function)
                if rng is None:
                    return ScopedEditResult(
                        success=False,
                        error=(
                            f"Definition '{edit.function}' not found in file — "
                            "cannot apply scoped edit."
                        ),
                    )
                resolved.append((rng[0], rng[1], edit.replacement))

            else:
                assert edit.lines is not None
                start, end = edit.lines
                n = len(current_lines)
                # Clamp gracefully: LLMs often miscount line numbers by a
                # small margin, especially when appending to the end of a
                # file.  Clamping produces the correct semantic result
                # (append / replace-to-EOF) rather than hard-failing.
                if start > n:
                    # Insert past EOF — treat as append at end.
                    start = n + 1
                    end = n + 1
                elif end > n:
                    # Replacement extends beyond EOF — clamp to file length.
                    end = n
                resolved.append((start, end, edit.replacement))

        # Check for overlapping ranges
        sorted_ranges = sorted(resolved, key=lambda x: x[0])
        for i in range(len(sorted_ranges) - 1):
            a_end = sorted_ranges[i][1]
            b_start = sorted_ranges[i + 1][0]
            if b_start <= a_end:
                return ScopedEditResult(
                    success=False,
                    error=(
                        f"Scoped edits overlap: range ending at line {a_end} "
                        f"conflicts with range starting at line {b_start}."
                    ),
                )

        # Apply in reverse order (highest line numbers first) so earlier
        # edits don't shift the indices of later ones.
        result_lines = list(current_lines)
        for start, end, replacement in sorted(resolved, key=lambda x: x[0], reverse=True):
            replacement_lines = replacement.splitlines()
            # 1-based → 0-based slice
            result_lines = (
                result_lines[: start - 1]
                + replacement_lines
                + result_lines[end:]
            )

        return ScopedEditResult(success=True, lines=result_lines)


# ---------------------------------------------------------------------------
# Definition integrity check (anti-hallucination guard)
# ---------------------------------------------------------------------------

def _extract_definitions(lines: list[str]) -> set[str]:
    """Return the set of function/method/class names present in *lines*."""
    text = "\n".join(lines)
    names: set[str] = {m.group(2) for m in _DEF_RE.finditer(text)}
    for m in _JS_DEF_RE.finditer(text):
        captured = m.group(2) or m.group(3) or m.group(4)
        if captured:
            names.add(captured)
    return names


def check_definition_integrity(
    original_lines: list[str],
    proposed_lines: list[str],
) -> list[str]:
    """Return names that exist in the original but have silently vanished from
    the proposal.

    A name is considered *silently vanished* if it:
      - appears as a definition in *original_lines*, AND
      - does NOT appear as a definition in *proposed_lines*.

    This catches the common LLM hallucination pattern where the model returns
    a truncated file that drops existing functions without including them as
    ``-`` removal lines in a diff.

    Returns an empty list when the proposal is clean.
    """
    original_defs = _extract_definitions(original_lines)
    proposed_defs = _extract_definitions(proposed_lines)
    return sorted(original_defs - proposed_defs)
