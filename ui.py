"""
textquest.ui
============
Terminal rendering: ANSI colors, a small [tag]markup[/] language,
visible-width-aware word wrapping, and an optional typewriter effect.

Markup examples (usable in any game text):
    "You found the [yellow]Golden Key[/]!"
    "[bold red]DANGER[/] ahead."
    "[dim]The wind whispers...[/]"

Tags may combine styles separated by spaces: [bold underline cyan]...[/]
A single [/] closes the most recent tag (tags can nest).
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time

# On Windows, ANSI escapes need virtual-terminal mode enabled explicitly
if os.name == "nt":  # pragma: no cover
    try:
        import ctypes
        _k32 = ctypes.windll.kernel32
        _k32.SetConsoleMode(_k32.GetStdHandle(-11), 7)
    except Exception:
        pass


def _unicode_ok() -> bool:
    enc = getattr(sys.stdout, "encoding", "") or "ascii"
    try:
        "🎲🏆•─═▸✓⚠".encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# Symbols with ASCII fallbacks for terminals that can't render unicode
SYM = {
    "dice": "🎲", "trophy": "🏆", "lock": "🔒", "dot": "•", "arrow": "▸",
    "check": "✓", "cross": "✗", "warn": "⚠", "rule": "─", "rule2": "═",
    "tree_last": "└─▶ ", "tree_mid": "├─▶ ", "tree_pipe": "│   ",
    "tree_arrow": "─▶", "more": "…", "to": "→",
} if _unicode_ok() else {
    "dice": "(d)", "trophy": "(*)", "lock": "(x)", "dot": "*", "arrow": ">",
    "check": "+", "cross": "x", "warn": "!", "rule": "-", "rule2": "=",
    "tree_last": "`-> ", "tree_mid": "|-> ", "tree_pipe": "|   ",
    "tree_arrow": "->", "more": "...", "to": "->",
}

RESET = "\033[0m"

STYLES = {
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "underline": "4",
    "blink": "5",
    "reverse": "7",
    "strike": "9",
    # foreground colors
    "black": "30",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "37",
    "gray": "90",
    "grey": "90",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_blue": "94",
    "bright_magenta": "95",
    "bright_cyan": "96",
    "bright_white": "97",
    # background colors
    "on_black": "40",
    "on_red": "41",
    "on_green": "42",
    "on_yellow": "43",
    "on_blue": "44",
    "on_magenta": "45",
    "on_cyan": "46",
    "on_white": "47",
}

_TAG_RE = re.compile(r"\[(/?)([a-zA-Z_ ]*)\]")
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _supports_color(stream=None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class Terminal:
    """All output goes through here so games/engine never touch print() directly."""

    def __init__(self, use_color: bool | None = None, typewriter_cps: float = 0.0,
                 width: int | None = None):
        self.use_color = _supports_color() if use_color is None else use_color
        # characters per second for typewriter effect; 0 disables it
        self.typewriter_cps = typewriter_cps
        self._width_override = width
        self.eof = False  # set when stdin is exhausted (non-interactive runs)
        # True whenever something was printed since the last mark(); used to
        # pause ("press Enter") before clearing so players can read output
        self.output_since_mark = False

    # ------------------------------------------------------------------ #
    # width / layout
    # ------------------------------------------------------------------ #
    @property
    def width(self) -> int:
        if self._width_override:
            return self._width_override
        try:
            return min(shutil.get_terminal_size().columns, 100)
        except OSError:
            return 80

    # ------------------------------------------------------------------ #
    # markup -> ANSI
    # ------------------------------------------------------------------ #
    def render_markup(self, text: str) -> str:
        """Convert [tag]...[/] markup into ANSI escape codes (or strip if no color)."""
        out: list[str] = []
        stack: list[list[str]] = []  # stack of style-code lists
        pos = 0
        for m in _TAG_RE.finditer(text):
            out.append(text[pos:m.start()])
            pos = m.end()
            closing, body = m.group(1), m.group(2).strip()
            if closing:
                if stack:
                    stack.pop()
                if self.use_color:
                    out.append(RESET)
                    # re-apply remaining styles on the stack
                    for codes in stack:
                        out.append("\033[" + ";".join(codes) + "m")
            else:
                codes = [STYLES[w] for w in body.split() if w in STYLES]
                if not codes:
                    # unknown tag: keep it verbatim so authors notice typos
                    out.append(m.group(0))
                    continue
                stack.append(codes)
                if self.use_color:
                    out.append("\033[" + ";".join(codes) + "m")
        out.append(text[pos:])
        result = "".join(out)
        if self.use_color and stack:
            result += RESET
        return result

    # ------------------------------------------------------------------ #
    # wrapping (aware of invisible ANSI codes)
    # ------------------------------------------------------------------ #
    @staticmethod
    def visible_len(s: str) -> int:
        return len(_ANSI_RE.sub("", s))

    def wrap(self, text: str, indent: str = "") -> str:
        """Word-wrap text that may contain ANSI codes, preserving blank lines."""
        maxw = self.width - len(indent)
        wrapped_lines: list[str] = []
        for paragraph in text.split("\n"):
            if not paragraph.strip():
                wrapped_lines.append("")
                continue
            line = ""
            for word in paragraph.split(" "):
                if not line:
                    line = word
                elif self.visible_len(line) + 1 + self.visible_len(word) <= maxw:
                    line += " " + word
                else:
                    wrapped_lines.append(indent + line)
                    line = word
            if line:
                wrapped_lines.append(indent + line)
        return "\n".join(wrapped_lines)

    # ------------------------------------------------------------------ #
    # output
    # ------------------------------------------------------------------ #
    def echo(self, text: str = "", wrap: bool = True, indent: str = "",
             typewriter: bool | None = None, markup: bool = True) -> None:
        self.output_since_mark = True
        # markup=False prints text verbatim — needed for ASCII art, which is
        # full of '[' characters the markup parser must not touch
        rendered = self.render_markup(text) if markup else text
        if wrap:
            rendered = self.wrap(rendered, indent=indent)
        use_tw = self.typewriter_cps > 0 if typewriter is None else typewriter
        if use_tw and sys.stdout.isatty():
            delay = 1.0 / max(self.typewriter_cps, 1)
            i = 0
            while i < len(rendered):
                # never delay in the middle of an ANSI escape sequence
                m = _ANSI_RE.match(rendered, i)
                if m:
                    sys.stdout.write(m.group(0))
                    i = m.end()
                    continue
                sys.stdout.write(rendered[i])
                sys.stdout.flush()
                time.sleep(delay)
                i += 1
            sys.stdout.write("\n")
        else:
            print(rendered)

    def rule(self, char: str | None = None, style: str = "dim") -> None:
        char = char or SYM["rule"]
        self.echo(f"[{style}]{char * self.width}[/]", wrap=False)

    def title(self, text: str) -> None:
        self.echo(f"[bold bright_white]{text}[/]", wrap=False)

    def clear(self) -> None:
        if sys.stdout.isatty():
            os.system("cls" if os.name == "nt" else "clear")

    def mark(self) -> None:
        """Forget any pending output (called right after the player acts)."""
        self.output_since_mark = False

    def pause(self, force: bool = False) -> None:
        """Wait for Enter so the player can read what's on screen.
        Skipped in non-interactive runs (pipes, tests)."""
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return
        if force or self.output_since_mark:
            try:
                input(self.render_markup("[dim]— press Enter —[/] "))
            except EOFError:
                self.eof = True

    def page_break(self) -> None:
        """Pause if there's unread output, then wipe the screen."""
        self.pause()
        self.clear()
        self.mark()

    def prompt(self, label: str = "> ") -> str:
        try:
            return input(self.render_markup(label))
        except EOFError:
            self.eof = True
            return "quit"
