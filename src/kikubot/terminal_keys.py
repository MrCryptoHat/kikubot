"""Key tokens for the inline ``keys`` query: parsing, allow-list, exit guards.

``keys`` is the one inline query that WRITES into an agent's pane, so its input
is checked here before anything is sent:

- every token is a tmux key name from ``NAMED_KEYS`` or ``=``-prefixed,
  percent-encoded literal text without control characters; the query must end
  with the terminator ``.``, so a half-typed or cut query sends nothing;
- ``Enter`` (and ``C-m``/``C-j``), ``C-c`` and ``C-d`` may only be the last
  key of a query: an Enter
  can submit a command that quits the CLI, and text sent after it would reach
  the shell left behind, where it runs as a host command;
- ``exit_armed`` spots a CLI waiting for a second Ctrl-C/Ctrl-D to quit, so the
  keys path never completes that quit (ending an agent is the panel's job);
- ``Clear`` empties the input box (the bot picks the keys per runtime), last
  key only as well.

The session side (lock, agent-running check, sending) is
``SessionManager.send_key_tokens``.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import unquote

from .terminal_parser import strip_escapes

TERMINATOR = "."
# Not a tmux key: the bot empties the input box its own way for the runtime
# (SessionManager.clear_input). Last key only, like Enter.
CLEAR_KEY = "Clear"

NAMED_KEYS = frozenset(
    {
        "Up",
        "Down",
        "Left",
        "Right",
        "Enter",
        "Escape",
        "Tab",
        "BTab",
        "BSpace",
        "DC",
        "Space",
        "Home",
        "End",
        "PPage",
        "NPage",
        "C-Space",
        *(f"F{n}" for n in range(1, 13)),
        *(f"C-{c}" for c in "abcdefghijklmnopqrstuvwxy"),  # no C-z: it suspends
        *(f"M-{c}" for c in "abcdefghijklmnopqrstuvwxyz"),
    }
)
# Keys that can end the CLI (or submit something that does): last key only.
# C-m and C-j reach the CLI as Enter and as a newline-submit.
FINAL_KEYS = frozenset({"Enter", "C-m", "C-j", "C-c", "C-d"})
EXIT_KEYS = frozenset({"C-c", "C-d"})

# A CLI asking for the second press that quits it ("Press Ctrl-C again to
# exit", codex's "ctrl + c again to quit").
_EXIT_HINT_RE = re.compile(
    r"ctrl\s*[-+]?\s*[cd]\b[^\n]{0,30}\bagain\b[^\n]{0,30}\b(?:exit|quit)",
    re.IGNORECASE,
)


class KeysError(Exception):
    """A ``keys`` query refused; ``code`` goes into the answer's meta."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class KeyToken:
    key: str  # a tmux key name, or the text to type
    literal: bool


def _decode_literal(raw: str) -> str:
    try:
        text = unquote(raw, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as e:
        raise KeysError("bad-key") from e
    if not text or any(unicodedata.category(c) == "Cc" for c in text):
        raise KeysError("bad-key")
    return text


def parse_tokens(raw_tokens: list[str]) -> list[KeyToken]:
    """Checked tokens of a ``keys`` query, or ``KeysError("bad-key")``."""
    if len(raw_tokens) < 2 or raw_tokens[-1] != TERMINATOR:
        raise KeysError("bad-key")
    tokens: list[KeyToken] = []
    for raw in raw_tokens[:-1]:
        if raw.startswith("="):
            tokens.append(KeyToken(_decode_literal(raw[1:]), literal=True))
        elif raw in NAMED_KEYS or raw == CLEAR_KEY:
            tokens.append(KeyToken(raw, literal=False))
        else:
            raise KeysError("bad-key")
    for tok in tokens[:-1]:
        if not tok.literal and (tok.key in FINAL_KEYS or tok.key == CLEAR_KEY):
            raise KeysError("bad-key")
    return tokens


def exit_armed(pane_text: str) -> bool:
    """Whether the CLI waits for a second Ctrl-C/Ctrl-D to quit."""
    tail = "\n".join(strip_escapes(pane_text).rstrip().split("\n")[-8:])
    return bool(_EXIT_HINT_RE.search(tail))
