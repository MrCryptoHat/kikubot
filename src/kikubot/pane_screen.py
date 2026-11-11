"""The agent's terminal as text for the inline ``screen`` query.

A client app draws the screen itself instead of reading the /agent photo, so
this hands it the pane exactly as tmux holds it: ``capture-pane -p -e`` (the
text the photo is drawn from, colours as SGR escapes), the cursor and the pane
size, optionally with lines of history above the visible screen.

- ``SCREEN_FORMAT`` / ``parse_screen_output`` — one tmux call captures the
  pane and then prints this format line; both transports parse it here.
- ``screen_messages`` — the inline answer: a meta JSON, then the text cut into
  ``{"d": "..."}`` JSON chunks that each fit one ``message_text``. JSON keeps
  ESC and leading spaces intact through Telegram; characters Telegram might
  drop or alter (controls, format and private-use characters) are escaped.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

SCREEN_FORMAT = (
    "#{cursor_x} #{cursor_y} #{pane_width} #{pane_height} "
    "#{cursor_flag} #{history_size}"
)
_META_RE = re.compile(r"^(\d+) (\d+) (\d+) (\d+) ([01]) (\d+)$")

# History lines one query may ask for. The answer trims the oldest of them to
# fit anyway (about 1500 lines fill it); this bounds the capture itself.
MAX_BACK = 3000
# Inline answers carry at most 50 results: the meta plus 49 chunks.
MAX_PARTS = 49
# message_text holds 4096 characters (counted here in UTF-16 units, which is
# never fewer); the margin covers the envelope and an escape that won't split.
_CHUNK_BUDGET = 4000
_ENVELOPE_HEAD = '{"d":"'
_ENVELOPE_TAIL = '"}'

_SHORT_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}
_ESCAPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


@dataclass(frozen=True)
class PaneScreen:
    """A pane capture: ``text`` holds ``back`` history lines, then the screen.

    Every line ends with a newline, a blank last row included, so the text
    holds exactly ``back + rows`` newlines.
    """

    text: str
    cols: int
    rows: int
    cursor: tuple[int, int] | None  # None while the program hides it
    back: int  # history lines leading ``text``
    history: int  # history lines tmux holds above the screen
    captured_at: float = 0.0  # unix seconds the capture was taken (0 = unknown)

    def digest(self) -> str:
        """Short hash of what a client draws: text, cursor and size.

        A client that already shows this capture sends it back (``h=``) and
        gets a meta-only answer instead of the same screen again.
        """
        h = hashlib.sha1(usedforsecurity=False)
        h.update(f"{self.cols}x{self.rows}|{self.cursor}|{self.back}|".encode())
        h.update(self.text.encode("utf-8", "replace"))
        return h.hexdigest()[:12]


def parse_screen_output(stdout: str) -> PaneScreen | None:
    """Split ``capture-pane -p [-S -N] ; display-message -p SCREEN_FORMAT``.

    The capture prints one line per row, each ending in a newline, and the
    format line comes last. None when the format line is missing or garbled.
    """
    body, sep, meta = stdout.rstrip("\n").rpartition("\n")
    m = _META_RE.match(meta)
    if not m:
        return None
    cx, cy, cols, rows, visible, history = (int(g) for g in m.groups())
    lines = body.split("\n") if sep else []
    # tmux prints exactly the requested rows; anything before the screen's
    # own rows is history.
    if len(lines) < rows:
        lines += [""] * (rows - len(lines))
    return PaneScreen(
        text="".join(f"{line}\n" for line in lines),
        cols=cols,
        rows=rows,
        cursor=(cx, cy) if visible else None,
        back=len(lines) - rows,
        history=history,
    )


@lru_cache(maxsize=8192)
def _escape(ch: str) -> str:
    short = _SHORT_ESCAPES.get(ch)
    if short is not None:
        return short
    if unicodedata.category(ch) not in _ESCAPED_CATEGORIES:
        return ch
    cp = ord(ch)
    if cp > 0xFFFF:
        cp -= 0x10000
        return f"\\u{0xD800 + (cp >> 10):04x}\\u{0xDC00 + (cp & 0x3FF):04x}"
    return f"\\u{cp:04x}"


def _units(piece: str) -> int:
    """Length in UTF-16 code units."""
    return len(piece) + sum(1 for c in piece if ord(c) > 0xFFFF)


def _cost(text: str) -> int:
    return sum(_units(_escape(c)) for c in text)


def _chunks(text: str) -> list[str]:
    """``text`` as JSON-escaped pieces of at most ``_CHUNK_BUDGET`` units."""
    out: list[str] = []
    cur: list[str] = []
    size = 0
    for ch in text:
        piece = _escape(ch)
        n = _units(piece)
        if size + n > _CHUNK_BUDGET and cur:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(piece)
        size += n
    if cur:
        out.append("".join(cur))
    return out


def _fit(screen: PaneScreen, max_parts: int) -> tuple[str, int]:
    """Keep the newest lines that fit ``max_parts`` chunks, the screen first.

    Returns the text and how many history lines it still leads with. Walks
    from the bottom and stops at the limit, so a long history costs only the
    lines that are kept. An escape is never split, so a chunk may fall short
    of the budget by one escape; the limit leaves room for that.
    """
    limit = max_parts * (_CHUNK_BUDGET - 12)
    text = screen.text
    ends = text.endswith("\n")
    lines = (text[:-1] if ends else text).split("\n") if text else []
    total = 0
    keep = 0
    for i, line in enumerate(reversed(lines)):
        cost = _cost(line) + 2  # + the escaped newline
        if i >= screen.rows and total + cost > limit:
            break
        total += cost
        keep += 1
    kept = lines[len(lines) - keep :]
    tail = "\n" if ends and kept else ""
    return "\n".join(kept) + tail, max(0, keep - screen.rows)


def screen_messages(
    screen: PaneScreen | None,
    *,
    ts: int,
    panel: int | None,
    max_parts: int = MAX_PARTS,
    extra: dict[str, object] | None = None,
    same: bool = False,
) -> list[tuple[str, str]]:
    """``(result id, message_text)`` pairs: ``screen``, then ``screen.1``…

    A screen that couldn't be captured (agent not running) answers with the
    meta alone, ``parts`` 0 and ``error``; so does one the client already
    shows (``same``: its ``h=`` matched). ``tms`` is the capture time in unix
    milliseconds, for ordering answers within a second. ``extra`` fields join
    the meta (the ``keys`` query's ``seq``/``sent``/``error``…) and win over
    its own.
    """
    tms = int((screen.captured_at if screen and screen.captured_at else ts) * 1000)
    if screen is None:
        meta = {
            "v": 1,
            "cols": 0,
            "rows": 0,
            "cursor": None,
            "ts": ts,
            "tms": tms,
            "parts": 0,
            "panel": panel,
            "error": "unavailable",
            **(extra or {}),
        }
        return [("screen", json.dumps(meta, separators=(",", ":")))]
    base = {
        "v": 1,
        "cols": screen.cols,
        "rows": screen.rows,
        "cursor": list(screen.cursor) if screen.cursor else None,
        "ts": ts,
        "tms": tms,
        "hash": screen.digest(),
    }
    if same:
        meta = {
            **base,
            "parts": 0,
            "panel": panel,
            "same": True,
            "history": screen.history,
            **(extra or {}),
        }
        return [("screen", json.dumps(meta, separators=(",", ":")))]
    text, back = _fit(screen, max_parts)
    parts = _chunks(text)
    truncated = len(parts) > max_parts
    parts = parts[:max_parts]
    meta = {
        **base,
        "parts": len(parts),
        "panel": panel,
        "back": back,
        "history": screen.history,
        **({"truncated": True} if truncated else {}),
        **(extra or {}),
    }
    out = [("screen", json.dumps(meta, separators=(",", ":")))]
    out += [
        (f"screen.{i}", f"{_ENVELOPE_HEAD}{p}{_ENVELOPE_TAIL}")
        for i, p in enumerate(parts, 1)
    ]
    return out
