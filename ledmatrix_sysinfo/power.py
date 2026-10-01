"""Detect Windows sleep and wake so the matrix can be blanked and restored.

Framework 16 uses Modern Standby. The LED module resets to its built-in
animation when USB power-cycles during sleep, so the monitor re-sends a
clear-and-sleep command while the session is suspended and redraws on wake.
"""

from __future__ import annotations

import ctypes
import logging
import threading

log = logging.getLogger("ledmatrix")

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013
DEVICE_NOTIFY_CALLBACK = 2

# GUID_CONSOLE_DISPLAY_STATE {6FE69556-704A-47A0-8F24-C28D936FDA47}
# GUID_LIDSWITCH_STATE_CHANGE {BA3E0F4D-B817-4094-A2D1-D56379E6A0F3}

_POWER_CB = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p)


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class _SUBSCRIBE(ctypes.Structure):
    _fields_ = [
        ("Callback", _POWER_CB),
        ("Context", ctypes.c_void_p),
    ]


def _guid(data1: int, data2: int, data3: int, data4: bytes) -> _GUID:
    guid = _GUID(data1, data2, data3, (ctypes.c_ubyte * 8)(*data4))
    return guid


_DISPLAY = _guid(0x6FE69556, 0x704A, 0x47A0, bytes.fromhex("8F24C28D936FDA47"))
_LID = _guid(0xBA3E0F4D, 0xB817, 0x4094, bytes.fromhex("A2D1D56379E6A0F3"))


class PowerMonitor:
    def __init__(self) -> None:
        self._asleep = False
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._handles: list[tuple[str, ctypes.c_void_p]] = []
        self._keep: list[object] = []
        self.registrations: dict[str, bool] = {}
        self._register()

    @property
    def asleep(self) -> bool:
        return self._asleep

    def _register(self) -> None:
        try:
            powrprof = ctypes.windll.powrprof
        except Exception:
            log.warning("powrprof is unavailable; sleep blanking is disabled")
            return
        self._register_suspend(powrprof)
        self._register_setting(powrprof, "display", _DISPLAY)
        self._register_setting(powrprof, "lid", _LID)

    def _register_suspend(self, powrprof) -> None:
        callback = _POWER_CB(self._on_suspend)
        params = _SUBSCRIBE(callback, None)
        handle = ctypes.c_void_p()
        self._keep.extend([callback, params])
        try:
            rc = powrprof.PowerRegisterSuspendResumeNotification(
                DEVICE_NOTIFY_CALLBACK, ctypes.byref(params), ctypes.byref(handle)
            )
        except Exception as exc:
            log.warning("suspend notification failed: %s", exc)
            self.registrations["suspend"] = False
            return
        self.registrations["suspend"] = rc == 0
        if rc == 0:
            self._handles.append(("suspend", handle))
        else:
            log.warning("PowerRegisterSuspendResumeNotification rc=%s", rc)

    def _register_setting(self, powrprof, name: str, guid: _GUID) -> None:
        callback = _POWER_CB(self._on_setting)
        params = _SUBSCRIBE(callback, None)
        handle = ctypes.c_void_p()
        self._keep.extend([callback, params])
        try:
            rc = powrprof.PowerSettingRegisterNotification(
                ctypes.byref(guid),
                DEVICE_NOTIFY_CALLBACK,
                ctypes.byref(params),
                ctypes.byref(handle),
            )
        except Exception as exc:
            log.warning("power setting %s failed: %s", name, exc)
            self.registrations[name] = False
            return
        self.registrations[name] = rc == 0
        if rc == 0:
            self._handles.append(("setting", handle))
        else:
            log.warning("PowerSettingRegisterNotification %s rc=%s", name, rc)

    def close(self) -> None:
        powrprof = ctypes.windll.powrprof
        for kind, handle in self._handles:
            try:
                if kind == "suspend":
                    powrprof.PowerUnregisterSuspendResumeNotification(handle)
                else:
                    powrprof.PowerSettingUnregisterNotification(handle)
            except Exception:
                pass
        self._handles.clear()

    def check_and_clear_wake(self) -> bool:
        if self._wake.is_set():
            self._wake.clear()
            return True
        return False

    def note_sleep(self, source: str) -> None:
        with self._lock:
            if self._asleep:
                return
            self._asleep = True
        log.info("sleep (%s)", source)

    def note_wake(self, source: str) -> None:
        with self._lock:
            if not self._asleep:
                return
            self._asleep = False
        log.info("wake (%s)", source)
        self._wake.set()

    def _on_suspend(self, _context, event_type, _setting) -> int:
        if event_type == PBT_APMSUSPEND:
            self.note_sleep("suspend")
        elif event_type in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            self.note_wake("resume")
        return 0

    def _on_setting(self, _context, event_type, setting) -> int:
        if event_type != PBT_POWERSETTINGCHANGE or not setting:
            return 0
        try:
            data_len = ctypes.c_ulong.from_address(setting + 16).value
            if data_len < 4:
                return 0
            value = ctypes.c_ulong.from_address(setting + 20).value
            guid_data1 = ctypes.c_ulong.from_address(setting).value
        except Exception:
            return 0
        if guid_data1 == _DISPLAY.Data1:
            if value == 0:
                self.note_sleep("display off")
            elif value == 1:
                self.note_wake("display on")
        elif guid_data1 == _LID.Data1:
            if value == 0:
                self.note_sleep("lid closed")
            elif value == 1:
                self.note_wake("lid open")
        return 0
