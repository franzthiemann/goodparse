"""Command-line entry point: ``goodnotes2xournal INPUT [-o OUTPUT]``."""

from __future__ import annotations

import argparse
import sys

from . import __version__, convert_file
from .goodnotes import parse_goodnotes
from .xournal import DEFAULT_WIDTH_SCALE


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="goodnotes2xournal",
        description="Convert GoodNotes (.goodnotes) files to Xournal++ (.xopp).",
    )
    parser.add_argument("input", help="path to a .goodnotes file or extracted folder")
    parser.add_argument("-o", "--output", help="output .xopp path (default: alongside input)")
    parser.add_argument(
        "-w", "--width-scale", type=float, default=DEFAULT_WIDTH_SCALE, dest="width_scale",
        help=f"multiplier on GoodNotes' stroke widths (default: {DEFAULT_WIDTH_SCALE})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="print a per-page stroke summary",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    try:
        if args.verbose:
            doc = parse_goodnotes(args.input)
            for i, page in enumerate(doc.pages):
                print(f"page {i + 1}: {len(page.strokes)} stroke(s)", file=sys.stderr)
        out = convert_file(args.input, args.output, width_scale=args.width_scale)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
