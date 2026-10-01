"""Match the matrix brightness to the room, using the same steps as the power button.

The Framework embedded controller maps the ambient light sensor onto the power
button LED. Those lux bands are used here, and each step is the same percentage
of full brightness the power button uses. The lux value is the one the
controller already publishes for that LED.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes

log = logging.getLogger("ledmatrix")

# Lux threshold, then power-button duty cycle in percent.
# The EC uses strict greater-than, top band first. A change of 15 lux or less
# is ignored so the level does not flicker on the threshold.
_BANDS = (
    (130, 55),
    (100, 40),
    (70, 28),
    (40, 15),
)
_DARKEST_PERCENT = 8
_HYSTERESIS_LUX = 15

# Chrome EC memory map. Offset 0x80 is the uint16 lux the power button reads.
_EC_PATH = r"\\.\GLOBALROOT\Device\CrosEC"
_EC_MEMMAP_SIZE = 0xFF
_EC_ALS_OFFSET = 0x80
_IOCTL_CROSEC_RDMEM = (0x80EC << 16) | (1 << 14) | (0x802 << 2)
_FILE_ACCESS = 0x0012019F


class _CrosEcReadMem(ctypes.Structure):
    _fields_ = [
        ("offset", ctypes.c_uint32),
        ("bytes", ctypes.c_uint32),
        ("buffer", ctypes.c_ubyte * _EC_MEMMAP_SIZE),
    ]


def brightness_for_lux(lux: float) -> int:
    """Matrix brightness 0-255 for a lux reading, using the power-button curve."""
    percent = _DARKEST_PERCENT
    for threshold, band in _BANDS:
        if lux > threshold:
            percent = band
            break
    return max(1, min(255, round(percent * 255 / 100)))


class AmbientLight:
    """Background reader for the Framework ambient light sensor."""

    def __init__(self) -> None:
        self.lux: float | None = None
        self._level: int | None = None
        self._prev_lux: float | None = None
        self._handle: int | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ambient-light", daemon=True)
        self._thread.start()

    def matrix_level(self) -> int | None:
        lux = self.lux
        if lux is None:
            return None
        if self._prev_lux is not None and abs(lux - self._prev_lux) <= _HYSTERESIS_LUX:
            return self._level
        self._prev_lux = lux
        self._level = brightness_for_lux(lux)
        return self._level

    def close(self) -> None:
        self._stop.set()
        self._close_handle()

    def _run(self) -> None:
        missing_logged = False
        while not self._stop.is_set():
            if self._handle is None:
                handle = _open_ec()
                with self._lock:
                    if self._stop.is_set():
                        if handle is not None:
                            ctypes.windll.kernel32.CloseHandle(handle)
                        return
                    self._handle = handle
                if self._handle is None:
                    if not missing_logged:
                        log.info("no ambient light sensor; matrix brightness stays manual")
                        missing_logged = True
                    self._stop.wait(2.0)
                    continue
            with self._lock:
                handle = self._handle
            lux = _read_lux(handle) if handle is not None else None
            if lux is None:
                self._close_handle()
                self._stop.wait(2.0)
                continue
            self.lux = lux
            self._stop.wait(0.4)
        self._close_handle()

    def _close_handle(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle is not None:
            ctypes.windll.kernel32.CloseHandle(handle)


def _open_ec() -> int | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(_EC_PATH, _FILE_ACCESS, 3, None, 3, 0, None)
    if not handle or handle == wintypes.HANDLE(-1).value:
        return None
    return int(handle)


def _read_lux(handle: int) -> float | None:
    kernel32 = ctypes.windll.kernel32
    kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.DeviceIoControl.restype = wintypes.BOOL
    mem = _CrosEcReadMem()
    mem.offset = _EC_ALS_OFFSET
    mem.bytes = 2
    returned = wintypes.DWORD()
    ok = kernel32.DeviceIoControl(
        handle,
        _IOCTL_CROSEC_RDMEM,
        ctypes.byref(mem),
        ctypes.sizeof(mem),
        ctypes.byref(mem),
        ctypes.sizeof(mem),
        ctypes.byref(returned),
        None,
    )
    if not ok:
        return None
    return float(int.from_bytes(bytes(mem.buffer[:2]), "little"))
