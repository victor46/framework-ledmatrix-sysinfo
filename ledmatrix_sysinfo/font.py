"""3x5 glyphs for the 9-pixel-wide LED matrix."""

from __future__ import annotations


def _glyph(art: str, height: int = 5, width: int = 3) -> list[list[int]]:
    rows = [line for line in art.strip("\n").split("\n")]
    parsed = []
    for line in rows:
        if len(line) != width:
            raise ValueError(f"glyph row must be {width} wide, got {line!r}")
        parsed.append([1 if ch == "#" else 0 for ch in line])
    if len(parsed) != height:
        raise ValueError(f"glyph must be {height} rows")
    return parsed


# Each glyph is 3 wide and 5 tall. '#' is on.
_ART = {
    "0": """
###
#.#
#.#
#.#
###
""",
    "1": """
.#.
##.
.#.
.#.
.#.
""",
    "2": """
###
..#
###
#..
###
""",
    "3": """
###
..#
###
..#
###
""",
    "4": """
#.#
#.#
###
..#
..#
""",
    "5": """
###
#..
###
..#
###
""",
    "6": """
###
#..
###
#.#
###
""",
    "7": """
###
..#
..#
..#
..#
""",
    "8": """
###
#.#
###
#.#
###
""",
    "9": """
###
#.#
###
..#
###
""",
    "K": """
#.#
#.#
##.
#.#
#..
""",
    "M": """
#.#
###
#.#
#.#
#.#
""",
    "-": """
...
...
###
...
...
""",
    " ": """
...
...
...
...
...
""",
    "C": """
###
#..
#..
#..
###
""",
    "E": """
###
#..
###
#..
###
""",
    "G": """
###
#..
#.#
#.#
.##
""",
    "P": """
###
#.#
###
#..
#..
""",
    "U": """
#.#
#.#
#.#
#.#
###
""",
}

FONT5: dict[str, list[list[int]]] = {ch: _glyph(art) for ch, art in _ART.items()}

# 3x3 letters for the stat names. Three letters fill the 9-pixel width.
_ART3 = {
    "C": """
###
#..
###
""",
    "E": """
###
##.
###
""",
    "G": """
##.
#.#
###
""",
    "M": """
#.#
###
#.#
""",
    "N": """
#.#
##.
#.#
""",
    "P": """
###
##.
#..
""",
    "S": """
###
##.
.##
""",
    "U": """
#.#
#.#
###
""",
}

FONT3: dict[str, list[list[int]]] = {ch: _glyph(art, 3) for ch, art in _ART3.items()}

def char_width(ch: str) -> int:
    if ch == ".":
        return 1
    return 3
