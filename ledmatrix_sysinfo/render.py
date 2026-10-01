"""Framebuffer layout for the 9 wide by 34 tall LED matrix.

Three chosen stats each get a 3x3 letter and a one-pixel bar. The letter is
the first letter of the stat: C, M, G, N, or S. Battery stays three lines:
the top and bottom show the charge, and the middle line moves right while
charging and left while discharging. Hours and minutes are large digits on
separate lines, with a blank column between the two digits. The bottom row
is the seconds hand.

    rows  0-2    first stat
    row   3      first bar
    rows  5-7    second stat
    row   8      second bar
    rows 10-12   third stat
    row  13      third bar
    rows 15-17   battery
    rows 21-25   hours
    rows 27-31   minutes
    row  33      seconds
"""

from __future__ import annotations

import time

from ledmatrix_sysinfo.font import FONT3, FONT5
from ledmatrix_sysinfo.metrics import Snapshot

WIDTH = 9
HEIGHT = 34

CPU_TEXT = 0
CPU_BAR = 3
MEM_TEXT = 5
MEM_BAR = 8
GPU_TEXT = 10
GPU_BAR = 13
BAT_Y = 15
HH_Y = 21
MM_Y = 27
TICK_Y = 33

# Three fixed rows. The letter in each row comes from the user's choice.
STAT_SLOTS = (
    (CPU_TEXT, CPU_BAR),
    (MEM_TEXT, MEM_BAR),
    (GPU_TEXT, GPU_BAR),
)

# key, menu name, letter drawn on the matrix
STAT_CHOICES = (
    ("cpu", "CPU", "C"),
    ("mem", "Memory", "M"),
    ("gpu", "GPU", "G"),
    ("net", "Network", "N"),
    ("ssd", "SSD", "S"),
)
STAT_KEYS = tuple(key for key, _label, _letter in STAT_CHOICES)
STAT_LABEL = {key: label for key, label, _letter in STAT_CHOICES}
STAT_LETTER = {key: letter for key, _label, letter in STAT_CHOICES}
DEFAULT_STATS = ("cpu", "mem", "gpu")

# (key, first row, last row) kept for the default layout and older callers.
HITS: list[tuple[str, int, int]] = [
    ("cpu", CPU_TEXT, CPU_BAR),
    ("mem", MEM_TEXT, MEM_BAR),
    ("gpu", GPU_TEXT, GPU_BAR),
    ("bat", BAT_Y, BAT_Y + 2),
]

TRACK = 14
TRACK_ACTIVE = 42
FILL = 255

METRIC_KEYS = [key for key, _y0, _y1 in HITS]

METRIC_LABELS = {
    "cpu": "CPU",
    "mem": "Memory",
    "gpu": "GPU",
    "net": "Network",
    "ssd": "SSD",
    "bat": "Battery",
}

def normalize_stats(value) -> tuple[str, str, str]:
    """Three distinct stats. Missing or repeated choices fall back to the default."""
    picked: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if item in STAT_KEYS and item not in picked:
                picked.append(item)
    for key in (*DEFAULT_STATS, *STAT_KEYS):
        if len(picked) >= 3:
            break
        if key not in picked:
            picked.append(key)
    return (picked[0], picked[1], picked[2])


def stat_percent(snap: Snapshot, key: str) -> float:
    if key == "cpu":
        return snap.cpu
    if key == "mem":
        return snap.mem
    if key == "gpu":
        return snap.gpu
    if key == "net":
        return snap.net_bar
    if key == "ssd":
        return snap.ssd
    return 0.0


def blank() -> list[list[int]]:
    return [[0] * HEIGHT for _ in range(WIDTH)]


def fmt_pct(pct: float) -> str:
    value = max(0, min(100, int(round(pct))))
    return f"{value:>3}"


def fmt_rate(bps: float) -> str:
    """Three-character rate. K is KB/s. A decimal or M is MB/s."""
    bps = max(0.0, float(bps))
    kb = bps / 1024.0
    mb = kb / 1024.0
    if mb >= 9.95:
        return f"{min(99, int(round(mb))):>2}M"
    if kb >= 100.0:
        return f"{mb:.1f}"[:3]
    return f"{int(kb):>2}K"


def draw_text(
    fb: list[list[int]],
    text: str,
    y: int,
    brightness: int = FILL,
    font: dict[str, list[list[int]]] | None = None,
    gap: int = 0,
) -> None:
    glyphs = font or FONT3
    width = gap * max(0, len(text) - 1)
    for ch in text:
        glyph = glyphs.get(ch)
        width += len(glyph[0]) if glyph else 3
    x = max(0, (WIDTH - width) // 2)
    for index, ch in enumerate(text):
        glyph = glyphs.get(ch)
        if glyph is None:
            x += 3
        else:
            for gy, row in enumerate(glyph):
                for gx, on in enumerate(row):
                    px = x + gx
                    py = y + gy
                    if on and 0 <= px < WIDTH and 0 <= py < HEIGHT:
                        fb[px][py] = brightness
            x += len(glyph[0])
        if index != len(text) - 1:
            x += gap


def _levels(pct: float, active: bool) -> list[int]:
    pct = max(0.0, min(100.0, pct))
    exact = pct / 100.0 * WIDTH
    full = int(exact)
    frac = exact - full
    track = TRACK_ACTIVE if active else TRACK
    levels = []
    for i in range(WIDTH):
        if i < full:
            levels.append(FILL)
        elif i == full and frac > 0.001 and full < WIDTH:
            levels.append(int(track + frac * (FILL - track)))
        else:
            levels.append(track)
    return levels


def _paint_rows(fb: list[list[int]], y: int, rows: int, levels: list[int]) -> None:
    for dy in range(rows):
        for x, level in enumerate(levels):
            fb[x][y + dy] = level


def _animate_row(fb: list[list[int]], y: int, now: float, direction: int, active: bool) -> None:
    """Move one light through the inner seven. The end lights stay on."""
    track = TRACK_ACTIVE if active else TRACK
    span = WIDTH - 2
    step = int(now) % span
    head = (1 + step) if direction > 0 else (WIDTH - 2 - step)
    for x in range(WIDTH):
        if x in (0, WIDTH - 1) or x == head:
            fb[x][y] = FILL
        else:
            fb[x][y] = track


def metric_value_text(snap: Snapshot, key: str) -> str:
    if key == "cpu":
        return fmt_pct(snap.cpu)
    if key == "mem":
        return fmt_pct(snap.mem)
    if key == "bat":
        if snap.battery_percent is None:
            return "---"
        return fmt_pct(snap.battery_percent)
    if key == "gpu":
        if not snap.gpu_ok and snap.gpu <= 0:
            return "---"
        return fmt_pct(snap.gpu)
    return "---"


def _seconds_ticker(fb: list[list[int]], second: int) -> None:
    """One bright pixel across the 9 lights. Each step is 60/9 seconds."""
    second = max(0, min(59, int(second)))
    index = min(WIDTH - 1, second * WIDTH // 60)
    for x in range(WIDTH):
        fb[x][TICK_Y] = FILL if x == index else TRACK


def _paint_clock(fb: list[list[int]], hour: int, minute: int, second: int) -> None:
    for y in range(HH_Y, MM_Y + 5):
        for x in range(WIDTH):
            fb[x][y] = 0
    # Nine lights cannot hold a readable HH:MM on one line. Each half is a
    # 3x5 pair with a blank column between the digits.
    draw_text(fb, f"{hour:02d}", HH_Y, font=FONT5, gap=1)
    draw_text(fb, f"{minute:02d}", MM_Y, font=FONT5, gap=1)
    _seconds_ticker(fb, second)


def render(
    snap: Snapshot,
    now: float,
    selected: str,
    stats: tuple[str, ...] | list[str] | None = None,
) -> list[list[int]]:
    fb = blank()
    chosen = normalize_stats(DEFAULT_STATS if stats is None else stats)
    for key, (text_y, bar_y) in zip(chosen, STAT_SLOTS):
        draw_text(fb, STAT_LETTER[key], text_y)
        _paint_rows(fb, bar_y, 1, _levels(stat_percent(snap, key), key == selected))

    mode = snap.battery_mode()
    active = selected == "bat"
    level = _levels(0.0 if snap.battery_percent is None else snap.battery_percent, active)
    _paint_rows(fb, BAT_Y, 1, level)
    _paint_rows(fb, BAT_Y + 2, 1, level)
    if mode == "charging":
        _animate_row(fb, BAT_Y + 1, now, 1, active)
    elif mode == "discharging":
        _animate_row(fb, BAT_Y + 1, now, -1, active)
    else:
        for x in range(1, WIDTH - 1):
            fb[x][BAT_Y + 1] = level[x]
        fb[0][BAT_Y + 1] = FILL
        fb[WIDTH - 1][BAT_Y + 1] = FILL

    local = time.localtime()
    _paint_clock(fb, local.tm_hour, local.tm_min, local.tm_sec)
    return fb


def render_at(
    snap: Snapshot,
    now: float,
    selected: str,
    clock: tuple[int, int, int],
    stats: tuple[str, ...] | list[str] | None = None,
) -> list[list[int]]:
    """Render with an explicit clock, so tests don't depend on the wall clock."""
    fb = render(snap, now, selected, stats)
    _paint_clock(fb, clock[0], clock[1], clock[2])
    return fb


def ascii_frame(fb: list[list[int]]) -> str:
    ramp = " .:-=+*#%@"
    lines = []
    for y in range(HEIGHT):
        chars = []
        for x in range(WIDTH):
            level = fb[x][y]
            chars.append(ramp[min(len(ramp) - 1, level * len(ramp) // 256)])
        lines.append("".join(chars))
    return "\n".join(lines)


def selected_key(now: float, dwell: float, pinned: str | None) -> str:
    if pinned in METRIC_KEYS:
        return pinned
    if dwell <= 0:
        dwell = 1.2
    index = int(now / dwell) % len(METRIC_KEYS)
    return METRIC_KEYS[index]


def self_test() -> None:
    """Check the layout math, fonts, and a few frames. Raises AssertionError on failure."""
    rates = [0, 500, 5 * 1024, 85 * 1024, 500 * 1024, 2.4 * 1024 * 1024, 40 * 1024 * 1024, 200 * 1024 * 1024]
    for bps in rates:
        text = fmt_rate(bps)
        assert len(text) == 3, (bps, text)
    assert fmt_pct(47) == " 47"
    assert fmt_pct(100) == "100"
    assert [second * WIDTH // 60 for second in (0, 6, 7, 59)] == [0, 0, 1, 8]

    full = Snapshot(cpu=100, mem=0, gpu=0, gpu_ok=True)
    fb = render_at(full, 0.0, "mem", (12, 34, 56))
    assert all(fb[x][CPU_BAR] == FILL for x in range(WIDTH))
    assert all(fb[x][MEM_BAR] == TRACK_ACTIVE for x in range(WIDTH))
    assert fb[3][CPU_TEXT] == FILL
    assert fb[0][CPU_TEXT] == 0
    assert fb[1][HH_Y] == 0
    assert fb[2][HH_Y] == FILL
    assert fb[WIDTH - 1][HH_Y + 4] == 0
    assert fb[WIDTH - 1][MM_Y] == 0
    assert fb[8][TICK_Y] == FILL
    assert fb[0][TICK_Y] == TRACK

    charging = Snapshot(battery_percent=40, battery_plugged=True, gpu_ok=True)
    frame_a = render_at(charging, 0.0, "bat", (1, 2, 0))
    frame_b = render_at(charging, 1.0, "bat", (1, 2, 0))
    assert [frame_a[x][BAT_Y + 1] for x in range(WIDTH)] != [frame_b[x][BAT_Y + 1] for x in range(WIDTH)]
    assert frame_a[0][BAT_Y + 1] == FILL
    assert frame_a[WIDTH - 1][BAT_Y + 1] == FILL
    assert frame_a[1][BAT_Y + 1] == FILL
    assert frame_b[2][BAT_Y + 1] == FILL

    draining = Snapshot(battery_percent=40, battery_plugged=False, gpu_ok=True)
    left = render_at(draining, 0.0, "cpu", (1, 2, 0))
    assert left[0][BAT_Y + 1] == FILL
    assert left[WIDTH - 1][BAT_Y + 1] == FILL
    assert left[WIDTH - 2][BAT_Y + 1] == FILL
    assert left[0][TICK_Y] == FILL

    empty = Snapshot(gpu_ok=False)
    assert metric_value_text(empty, "gpu") == "---"
    assert selected_key(0.0, 1.2, None) == "cpu"
    assert selected_key(1.2, 1.2, None) == "mem"
    assert selected_key(50.0, 1.2, "gpu") == "gpu"
    assert normalize_stats(["ssd", "ssd", "net"]) == ("ssd", "net", "cpu")
    assert normalize_stats(None) == DEFAULT_STATS

    custom = Snapshot(cpu=0, mem=100, ssd=100, net_bar=0, gpu=0, gpu_ok=True)
    picked = render_at(custom, 0.0, "", (0, 0, 0), ("ssd", "net", "mem"))
    assert picked[3][CPU_TEXT] == FILL
    assert picked[4][CPU_TEXT] == FILL
    assert all(picked[x][CPU_BAR] == FILL for x in range(WIDTH))
    assert picked[3][MEM_TEXT] == FILL
    assert picked[4][MEM_TEXT] == 0
    assert picked[5][MEM_TEXT] == FILL
    assert all(picked[x][MEM_BAR] == TRACK for x in range(WIDTH))
    assert all(picked[x][GPU_BAR] == FILL for x in range(WIDTH))

    for y in range(HEIGHT):
        for x in range(WIDTH):
            assert 0 <= fb[x][y] <= 255
