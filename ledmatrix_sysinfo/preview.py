"""Scaled-up preview of the 9x34 framebuffer. Labels sit beside each bar."""

from __future__ import annotations

import threading
import tkinter as tk

from ledmatrix_sysinfo.render import BAT_Y, HEIGHT, STAT_SLOTS, WIDTH

CELL = 16
GAP = 3
LABEL_W = 42
MARGIN_X = 8
MARGIN_TOP = 12
MARGIN_BOTTOM = 36


class Preview:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.fb = None
        self.caption = "Starting..."
        self.labels = [("C", 0, 3), ("M", 5, 8), ("G", 10, 13), ("BAT", BAT_Y, BAT_Y + 2)]
        self.alive = True
        self._thread = threading.Thread(target=self._run, name="preview", daemon=True)
        self._root: tk.Tk | None = None
        self._thread.start()

    def update(self, fb, caption: str, labels=None) -> None:
        with self._lock:
            self.fb = fb
            self.caption = caption
            if labels is not None:
                self.labels = labels
        with self._lock:
            self.fb = fb
            self.caption = caption

    def close(self) -> None:
        root = self._root
        if root is not None:
            try:
                root.after(0, root.destroy)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            self._main()
        finally:
            self.alive = False
            self._root = None

    def _main(self) -> None:
        root = tk.Tk()
        self._root = root
        root.title("Framework LED Matrix")
        root.configure(bg="#101010")
        origin_x = MARGIN_X + LABEL_W
        width = origin_x + MARGIN_X + WIDTH * CELL + (WIDTH - 1) * GAP
        height = MARGIN_TOP + MARGIN_BOTTOM + HEIGHT * CELL + (HEIGHT - 1) * GAP
        root.geometry(f"{width}x{height}")
        root.resizable(False, False)
        canvas = tk.Canvas(root, width=width, height=height, bg="#101010", highlightthickness=0)
        canvas.pack()
        caption = tk.StringVar(value=self.caption)
        tk.Label(root, textvariable=caption, bg="#101010", fg="#dddddd", font=("Segoe UI", 10)).place(
            x=8, y=height - 28
        )

        pitch = CELL + GAP
        slots = list(STAT_SLOTS) + [(BAT_Y, BAT_Y + 2)]
        label_ids = []
        for y0, y1 in slots:
            cy = MARGIN_TOP + ((y0 + y1) / 2) * pitch + CELL / 2
            label_ids.append(
                canvas.create_text(MARGIN_X, cy, text="", anchor="w", fill="#bbbbbb", font=("Segoe UI", 8))
            )

        rects = []
        for y in range(HEIGHT):
            row = []
            for x in range(WIDTH):
                x0 = origin_x + x * pitch
                y0 = MARGIN_TOP + y * pitch
                row.append(canvas.create_rectangle(x0, y0, x0 + CELL, y0 + CELL, width=0, fill="#181818"))
            rects.append(row)

        def tick() -> None:
            if not root.winfo_exists():
                return
            with self._lock:
                fb = self.fb
                text = self.caption
                labels = list(self.labels)
            caption.set(text)
            for item, (name, _y0, _y1) in zip(label_ids, labels):
                canvas.itemconfigure(item, text=name)
            if fb is not None:
                for y in range(HEIGHT):
                    for x in range(WIDTH):
                        canvas.itemconfigure(rects[y][x], fill=_color(fb[x][y]))
            root.after(50, tick)

        root.protocol("WM_DELETE_WINDOW", root.destroy)
        root.after(50, tick)
        root.mainloop()


def _color(value: int) -> str:
    if value <= 0:
        return "#181818"
    level = 36 + int(max(0, min(255, value)) / 255 * 219)
    return f"#{level:02x}{level:02x}{level:02x}"
