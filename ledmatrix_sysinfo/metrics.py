"""Live CPU, memory, disk, battery, network, and GPU samples on Windows."""

from __future__ import annotations

import ctypes
import math
import re
import sys
from ctypes import wintypes
from dataclasses import dataclass

import psutil

# Full-scale points for logarithmic bars.
NET_FULL_SCALE_BPS = 100 * 1024 * 1024  # 100 MB/s
DISK_FULL_SCALE_BPS = 2 * 1024 * 1024 * 1024  # 2 GB/s


def log_percent(bps: float, full_scale: float, floor: float = 1024.0) -> float:
    """Map a byte rate onto 0..100. 1 KB/s is just visible; full_scale is a full bar."""
    if bps <= 0:
        return 0.0
    if bps < floor:
        return 3.0
    pct = math.log(bps / floor) / math.log(full_scale / floor) * 100.0
    return max(0.0, min(100.0, pct))


def _ema(prev: float | None, new: float, alpha: float) -> float:
    if prev is None:
        return new
    return prev * (1.0 - alpha) + new * alpha


@dataclass
class Snapshot:
    cpu: float = 0.0
    mem: float = 0.0
    ssd: float = 0.0
    ssd_is_percent: bool = True
    ssd_bps: float = 0.0
    battery_percent: float | None = None
    battery_plugged: bool | None = None
    net_up_bps: float = 0.0
    net_down_bps: float = 0.0
    net_bar: float = 0.0
    rx_vis: float = 0.0
    tx_vis: float = 0.0
    gpu: float = 0.0
    gpu_ok: bool = False
    battery_secsleft: int | None = None

    @property
    def net_bps(self) -> float:
        return self.net_up_bps + self.net_down_bps

    def battery_mode(self) -> str:
        """full, charging, discharging, or missing."""
        if self.battery_percent is None:
            return "missing"
        if self.battery_plugged:
            holding = self.battery_secsleft == psutil.POWER_TIME_UNLIMITED
            if self.battery_percent >= 99.5 or holding:
                return "full"
            return "charging"
        if self.battery_plugged is False:
            return "discharging"
        return "missing"


class GpuMonitor:
    """GPU 3D-engine utilization via Windows PDH (the same source Task Manager uses)."""

    _LUID = re.compile(r"luid_(0x[0-9a-f]+_0x[0-9a-f]+)", re.IGNORECASE)
    _COUNTER = r"\GPU Engine(*)\Utilization Percentage"
    _REFRESH_SEC = 15.0

    def __init__(self) -> None:
        self.available = sys.platform == "win32"
        self.ok = False
        self.value = 0.0
        self._pdh = None
        self._query = ctypes.c_void_p() if sys.platform == "win32" else None
        self._counters: list[tuple[ctypes.c_void_p, str]] = []
        self._last_build = 0.0
        self._value_type = None
        if self.available:
            self._setup_ctypes()

    def _setup_ctypes(self) -> None:
        self._pdh = ctypes.WinDLL("pdh")
        self._pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        self._pdh.PdhOpenQueryW.restype = wintypes.DWORD
        self._pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        self._pdh.PdhCloseQuery.restype = wintypes.DWORD
        self._pdh.PdhExpandWildCardPathW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
        ]
        self._pdh.PdhExpandWildCardPathW.restype = wintypes.DWORD
        self._pdh.PdhAddEnglishCounterW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
        self._pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        self._pdh.PdhCollectQueryData.restype = wintypes.DWORD

        class PDH_FMT_COUNTERVALUE(ctypes.Structure):
            _fields_ = [
                ("CStatus", wintypes.DWORD),
                ("doubleValue", ctypes.c_double),
            ]

        self._value_type = PDH_FMT_COUNTERVALUE
        self._pdh.PdhGetFormattedCounterValue.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(PDH_FMT_COUNTERVALUE),
        ]
        self._pdh.PdhGetFormattedCounterValue.restype = wintypes.DWORD

    def close(self) -> None:
        if self._pdh is not None and self._query:
            self._pdh.PdhCloseQuery(self._query)
        self._query = ctypes.c_void_p()
        self._counters.clear()

    def _expand(self) -> list[str]:
        needed = wintypes.DWORD(0)
        rc = self._pdh.PdhExpandWildCardPathW(None, self._COUNTER, None, ctypes.byref(needed), 0)
        # PDH_MORE_DATA (0x800007D2) is the expected result of the size query.
        if needed.value == 0:
            return []
        buf = ctypes.create_unicode_buffer(needed.value)
        rc = self._pdh.PdhExpandWildCardPathW(None, self._COUNTER, buf, ctypes.byref(needed), 0)
        if rc != 0:
            return []
        # Multi-string: paths separated by nulls. buf.value stops at the first null.
        text = "".join(buf[i] for i in range(needed.value))
        return [part for part in text.split("\x00") if part]

    def _rebuild(self, now: float) -> None:
        self.close()
        self._last_build = now
        if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            self.ok = False
            return
        added = 0
        for path in self._expand():
            if "engtype_3d" not in path.lower():
                continue
            handle = ctypes.c_void_p()
            if self._pdh.PdhAddEnglishCounterW(self._query, path, None, ctypes.byref(handle)) == 0:
                self._counters.append((handle, path))
                added += 1
        self.ok = added > 0
        if self.ok:
            # First sample only establishes a baseline.
            self._pdh.PdhCollectQueryData(self._query)

    def read(self, now: float) -> float:
        if not self.available or self._pdh is None:
            self.ok = False
            return self.value
        if not self._counters or (now - self._last_build) > self._REFRESH_SEC:
            previous = self.value
            self._rebuild(now)
            self.value = previous
            return self.value
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return self.value

        per_luid: dict[str, float] = {}
        pdh_fmt_double = 0x00000200
        for handle, path in self._counters:
            raw = self._value_type()
            counter_type = wintypes.DWORD()
            rc = self._pdh.PdhGetFormattedCounterValue(
                handle, pdh_fmt_double, ctypes.byref(counter_type), ctypes.byref(raw)
            )
            if rc != 0 or raw.CStatus not in (0, 1):
                continue
            match = self._LUID.search(path)
            key = match.group(1).lower() if match else path
            per_luid[key] = per_luid.get(key, 0.0) + float(raw.doubleValue)
        if per_luid:
            self.value = max(0.0, min(100.0, max(per_luid.values())))
            self.ok = True
        return self.value


def _net_totals() -> tuple[int, int]:
    sent = 0
    recv = 0
    counters = psutil.net_io_counters(pernic=True)
    for name, nic in counters.items():
        lower = name.lower()
        if "loopback" in lower or lower == "lo":
            continue
        sent += nic.bytes_sent
        recv += nic.bytes_recv
    return sent, recv


class DiskActivity:
    """Disk busy percent from \\PhysicalDisk(_Total)\\% Idle Time."""

    _PATH = r"\PhysicalDisk(_Total)\% Idle Time"

    def __init__(self) -> None:
        self.ok = False
        self.value = 0.0
        self._pdh = None
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self._value_type = None
        self._primed = False
        if sys.platform == "win32":
            self._open()

    def _open(self) -> None:
        self._pdh = ctypes.WinDLL("pdh")
        self._pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
        self._pdh.PdhOpenQueryW.restype = wintypes.DWORD
        self._pdh.PdhAddEnglishCounterW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._pdh.PdhAddEnglishCounterW.restype = wintypes.DWORD
        self._pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        self._pdh.PdhCollectQueryData.restype = wintypes.DWORD
        self._pdh.PdhCloseQuery.argtypes = [ctypes.c_void_p]
        self._pdh.PdhCloseQuery.restype = wintypes.DWORD

        class PDH_FMT_COUNTERVALUE(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        self._value_type = PDH_FMT_COUNTERVALUE
        self._pdh.PdhGetFormattedCounterValue.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(PDH_FMT_COUNTERVALUE),
        ]
        self._pdh.PdhGetFormattedCounterValue.restype = wintypes.DWORD
        if self._pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._query)) != 0:
            return
        if self._pdh.PdhAddEnglishCounterW(self._query, self._PATH, None, ctypes.byref(self._counter)) != 0:
            return
        self._pdh.PdhCollectQueryData(self._query)
        self._primed = True
        self.ok = True

    def close(self) -> None:
        if self._pdh is not None and self._query:
            self._pdh.PdhCloseQuery(self._query)
        self._query = ctypes.c_void_p()

    def read(self) -> float:
        if not self.ok or self._pdh is None:
            return self.value
        if self._pdh.PdhCollectQueryData(self._query) != 0:
            return self.value
        if not self._primed:
            self._primed = True
            return self.value
        raw = self._value_type()
        counter_type = wintypes.DWORD()
        rc = self._pdh.PdhGetFormattedCounterValue(
            self._counter, 0x00000200, ctypes.byref(counter_type), ctypes.byref(raw)
        )
        if rc != 0 or raw.CStatus not in (0, 1):
            return self.value
        idle = max(0.0, min(100.0, float(raw.doubleValue)))
        self.value = 100.0 - idle
        return self.value


def _disk_bytes() -> int:
    io = psutil.disk_io_counters(perdisk=False)
    if io is None:
        return 0
    return int(io.read_bytes + io.write_bytes)


class Sampler:
    def __init__(self) -> None:
        psutil.cpu_percent(interval=None)
        self._gpu = GpuMonitor()
        self._disk = DiskActivity()
        self._cpu: float | None = None
        self._ssd: float | None = None
        self._gpu_smooth: float | None = None
        self._net_bar: float | None = None
        self._last_t: float | None = None
        self._last_net = _net_totals()
        self._last_disk_bytes = _disk_bytes()
        self.rx_vis = 0.0
        self.tx_vis = 0.0

    def close(self) -> None:
        self._gpu.close()
        self._disk.close()

    def sample(self, now: float) -> Snapshot:
        dt = 0.1 if self._last_t is None else max(0.001, now - self._last_t)
        self._last_t = now

        cpu_raw = float(psutil.cpu_percent(interval=None))
        self._cpu = _ema(self._cpu, cpu_raw, 0.45)
        mem = float(psutil.virtual_memory().percent)

        sent, recv = _net_totals()
        prev_sent, prev_recv = self._last_net
        up_bps = max(0.0, (sent - prev_sent) / dt)
        down_bps = max(0.0, (recv - prev_recv) / dt)
        self._last_net = (sent, recv)

        rx_target = log_percent(down_bps, NET_FULL_SCALE_BPS)
        tx_target = log_percent(up_bps, NET_FULL_SCALE_BPS)
        if recv > prev_recv:
            rx_target = max(rx_target, 40.0)
        if sent > prev_sent:
            tx_target = max(tx_target, 40.0)
        decay = math.exp(-dt / 0.22)
        self.rx_vis = max(rx_target, self.rx_vis * decay)
        self.tx_vis = max(tx_target, self.tx_vis * decay)
        net_raw = log_percent(up_bps + down_bps, NET_FULL_SCALE_BPS)
        self._net_bar = _ema(self._net_bar, net_raw, 0.5)

        disk_bytes = _disk_bytes()
        disk_bps = max(0, disk_bytes - self._last_disk_bytes) / dt
        self._last_disk_bytes = disk_bytes
        if self._disk.ok:
            ssd_raw = self._disk.read()
            ssd_is_percent = True
        else:
            ssd_raw = log_percent(disk_bps, DISK_FULL_SCALE_BPS, floor=64 * 1024)
            ssd_is_percent = False
        self._ssd = _ema(self._ssd, ssd_raw, 0.45)

        battery_percent = None
        battery_plugged = None
        battery_secsleft = None
        try:
            batt = psutil.sensors_battery()
        except Exception:
            batt = None
        if batt is not None:
            if batt.percent is not None:
                battery_percent = float(batt.percent)
            battery_plugged = bool(batt.power_plugged)
            try:
                battery_secsleft = int(batt.secsleft)
            except (TypeError, ValueError):
                battery_secsleft = None

        gpu_raw = self._gpu.read(now)
        self._gpu_smooth = _ema(self._gpu_smooth, gpu_raw, 0.45)

        return Snapshot(
            cpu=self._cpu or 0.0,
            mem=mem,
            ssd=self._ssd or 0.0,
            ssd_is_percent=ssd_is_percent,
            ssd_bps=disk_bps,
            battery_percent=battery_percent,
            battery_plugged=battery_plugged,
            battery_secsleft=battery_secsleft,
            net_up_bps=up_bps,
            net_down_bps=down_bps,
            net_bar=self._net_bar or 0.0,
            rx_vis=self.rx_vis,
            tx_vis=self.tx_vis,
            gpu=self._gpu_smooth or 0.0,
            gpu_ok=self._gpu.ok,
        )
