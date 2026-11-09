"""Parse the Cocoa RTF used by GoodNotes text boxes into styled runs.

GoodNotes writes its text boxes as a Cocoa RTF subset.  This module splits a
box into visual *lines* (``\\par``/``\\n``/``\\line``), and each line into
styled *runs* — maximal spans sharing ``bold`` / ``italic`` / ``underline`` /
``strike`` / colour.  A run carries the attributes in effect while its text was
emitted (so a trailing ``\\b`` does not retroactively bold the previous word).

Two GoodNotes quirks are handled:
* the box opens with a run of blank lines (``\\par``) before the real text;
  leading/trailing blank lines are trimmed.
* colours resolve against ``\\colortbl`` (``\\cfN`` -> Nth entry), defaulting
  to black.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class TextRun:
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: Tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)
    # Font family name (e.g. "Futura", "Helvetica Neue") when known, else None.
    # Used only by the optional embedded-font path; the base-14 emitter
    # ignores it.
    font_family: Optional[str] = None
    # Explicit size in canvas units (text objects carry one per run); None =
    # the box's size.
    size: Optional[float] = None


@dataclass
class TextLine:
    """One visual line of a text box: an ordered list of styled runs."""
    runs: List[TextRun] = field(default_factory=list)
    align: str = "left"  # "left" | "center" | "right" | "justify"

    def text(self) -> str:
        return "".join(r.text for r in self.runs)


# RTF tokens.  Raw CR/LF in the source are not text (RTF ignores them); a
# backslash followed by a line break is a paragraph break, like ``\par``.
_TOKEN = re.compile(
    r"\\(?P<word>[a-zA-Z]+)(?P<num>-?\d*) ?|\\'(?P<hex>[0-9a-fA-F]{2})|"
    r"(?P<parbreak>\\\r?\n)|\\(?P<sym>[^a-zA-Z0-9'])|"
    r"(?P<char>[^\{}\\\r\n])|(?P<nl>\r?\n)|(?P<open>{)|(?P<close>})"
)


# Destination groups whose content is not body text.  Any ``{\\* ...}`` group
# is ignorable by definition; ``\\listtext`` holds a pre-rendered list marker
# that Cocoa repeats in the paragraph text itself.
_DESTINATIONS = ("fonttbl", "colortbl", "stylesheet", "listtable",
                 "listoverridetable", "listtext", "info", "pict")
_GROUP_START = re.compile(r"\{\\(\*|[a-zA-Z]+)")


def _strip_table_groups(s: str) -> str:
    """Remove non-text destination groups (font/colour/list tables, ``\\*``
    groups, ``\\listtext``), honouring escaped braces."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        m = _GROUP_START.match(s, i)
        if m and (m.group(1) == "*" or m.group(1) in _DESTINATIONS):
            depth = 0
            while i < n:
                c = s[i]
                if c == "\\":
                    i += 2  # skip the escaped character (incl. \\{ and \\})
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _parse_colortbl(s: str) -> List[Tuple[float, float, float]]:
    """Return RGB (0..1) entries from ``\\colortbl`` (empty -> [] => black)."""
    entries: List[Tuple[float, float, float]] = []
    if "\\colortbl" in s:
        i = s.find("\\colortbl")
        j = s.find("}", i)
        seg = s[i:j] if j != -1 else s[i:]
        for cm in re.finditer(r"\\red(\d+)\\green(\d+)\\blue(\d+)", seg):
            entries.append((int(cm.group(1)) / 255.0,
                            int(cm.group(2)) / 255.0,
                            int(cm.group(3)) / 255.0))
    return entries


def parse_rtf_runs(rtf) -> List[TextLine]:
    """Parse GoodNotes RTF into a list of :class:`TextLine` (styled runs)."""
    s = rtf.decode("cp1252") if isinstance(rtf, (bytes, bytearray)) else rtf
    colors = _parse_colortbl(s)
    s = _strip_table_groups(s)

    bold = italic = underline = strike = False
    cf = 2  # default colour index (GoodNotes uses ``\\cf2`` = black)
    uc = 1  # fallback characters that follow a ``\\uN`` escape (``\\ucN``)
    skip = 0

    def color() -> Tuple[float, float, float, float]:
        if 1 <= cf <= len(colors):
            return (*colors[cf - 1], 1.0)
        return (0.0, 0.0, 0.0, 1.0)

    lines: List[TextLine] = [TextLine()]
    cur = TextRun("")  # the in-progress run (created with the flags in effect)

    def new_cur():
        nonlocal cur
        cur = TextRun("", bold, italic, underline, strike, color())

    def finish_run():
        if cur.text:
            lines[-1].runs.append(cur)
        new_cur()

    for tok in _TOKEN.finditer(s):
        word = tok.group("word")
        if word:
            num = tok.group("num")
            changed = False
            if word == "b":
                bold = num != "0"; changed = True
            elif word == "i":
                italic = num != "0"; changed = True
            elif word == "ul":
                underline = num != "0"; changed = True
            elif word == "ulnone":
                underline = False; changed = True
            elif word in ("strike", "strikeout", "strikethrough"):
                strike = num != "0"; changed = True
            elif word == "cf":
                cf = int(num) if num else cf; changed = True
            elif word in ("par", "n", "line"):
                finish_run()
                lines.append(TextLine())
                continue
            elif word == "tab":
                cur.text += "\t"
            elif word == "uc":
                uc = int(num) if num else 1
            elif word == "u" and num:
                cur.text += chr(int(num) % 65536)
                skip = uc
            else:
                continue  # \\fs, \\fN, \\sl, \\ansi, ... (not run attributes)
            if changed:
                finish_run()  # close the run so the new attrs start a new one
            continue
        if tok.group("parbreak") is not None:
            finish_run()
            lines.append(TextLine())
            continue
        if skip and (tok.group("hex") is not None or tok.group("char") is not None):
            skip -= 1  # the ANSI fallback of a preceding \\uN
            continue
        if tok.group("hex") is not None:
            cur.text += bytes.fromhex(tok.group("hex")).decode("cp1252", "replace")
        elif tok.group("char") is not None:
            cur.text += tok.group("char")
        elif tok.group("sym") is not None:
            sym = tok.group("sym")
            if sym in "{}\\":
                cur.text += sym
            elif sym == "~":
                cur.text += "\u00a0"
        # raw line breaks and group braces carry no text
    finish_run()

    # trim blank padding lines, keep at least one
    first = next((i for i, ln in enumerate(lines) if ln.text().strip() != ""), None)
    last = next((i for i in range(len(lines) - 1, -1, -1)
                 if lines[i].text().strip() != ""), None)
    if first is None:
        return [TextLine()]
    out = lines[first:last + 1]
    # drop a leading whitespace-only run (the box's blank-line padding), then
    # strip any leading spaces off the first remaining run
    for ln in out:
        while ln.runs and not ln.runs[0].text.strip():
            ln.runs.pop(0)
        if ln.runs:
            ln.runs[0].text = ln.runs[0].text.lstrip(" \t")
            if not ln.runs[0].text:
                ln.runs.pop(0)
    return out
