"""USB serial connection to Framework LED Matrix modules."""

from __future__ import annotations

import logging
import time

import serial
from serial.tools import list_ports

FWK_VID = 0x32AC
LED_MATRIX_PID = 0x0020
BAUD = 115200

CMD_BRIGHTNESS = 0x00
CMD_SLEEP = 0x03
CMD_ANIMATE = 0x04
CMD_STAGE_COL = 0x07
CMD_FLUSH = 0x08

WIDTH = 9
HEIGHT = 34

log = logging.getLogger("ledmatrix")


def list_matrix_ports() -> list[str]:
    """COM ports for Framework LED Matrix modules (VID 32AC, PID 0020)."""
    found = []
    for port in list_ports.comports():
        if port.vid == FWK_VID and port.pid == LED_MATRIX_PID:
            found.append(port.device)
    return found


def describe_ports() -> list[str]:
    lines = []
    for port in list_ports.comports():
        vid = f"0x{port.vid:04X}" if port.vid is not None else "?"
        pid = f"0x{port.pid:04X}" if port.pid is not None else "?"
        mark = ""
        if port.vid == FWK_VID and port.pid == LED_MATRIX_PID:
            mark = "  <-- LED Matrix"
        lines.append(f"{port.device:8}  VID {vid}  PID {pid}  {port.description}{mark}")
    return lines or ["No serial ports found."]


class MatrixDevice:
    """One LED matrix. Framebuffer is fb[x][y], x=0..8 left to right, y=0..33 top to bottom."""

    def __init__(self, port: str):
        self.port = port
        self.version: str | None = None
        self._ser: serial.Serial | None = None

    @property
    def connected(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def open(self) -> None:
        self.close()
        # DTR/RTS stay low so opening the port does not reset the RP2040.
        self._ser = serial.Serial()
        self._ser.port = self.port
        self._ser.baudrate = BAUD
        self._ser.timeout = 0.3
        self._ser.write_timeout = 1.0
        self._ser.dtr = False
        self._ser.rts = False
        self._ser.open()
        # The firmware drops USB traffic until the port is configured.
        time.sleep(0.25)
        self._ser.reset_input_buffer()
        # Wake. If the module was asleep it fades the LEDs back on and does not
        # read USB during that fade, so wait before sending anything else.
        self.command(CMD_SLEEP, [0x00])
        time.sleep(1.5)
        self.command(CMD_ANIMATE, [0x00])
        self.version = self.firmware_version()

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    def command(self, command_id: int, params: list[int] | bytes = ()) -> None:
        """Send one command as its own USB packet.

        The firmware parses each serial read as a single command and only looks
        at the first 64 bytes. A full frame is about 350 bytes, so commands have
        to be written and flushed one at a time or the module ignores them.
        """
        if not self.connected or self._ser is None:
            raise serial.SerialException("not open")
        payload = bytes([0x32, 0xAC, command_id]) + bytes(params)
        if len(payload) > 64:
            raise ValueError(f"command {command_id:#x} is {len(payload)} bytes; the module reads 64")
        self._ser.write(payload)
        self._ser.flush()
        # Long enough for the module to read this packet before the next one arrives.
        time.sleep(0.005)

    def set_brightness(self, level: int) -> None:
        level = max(0, min(255, int(level)))
        self.command(CMD_BRIGHTNESS, [level])

    def firmware_version(self) -> str | None:
        if not self.connected or self._ser is None:
            return None
        self._ser.reset_input_buffer()
        self.command(0x20, [])
        data = self._ser.read(32)
        if len(data) < 2:
            return None
        major = data[0]
        minor = (data[1] & 0xF0) >> 4
        patch = data[1] & 0x0F
        return f"{major}.{minor}.{patch}"

    def draw(self, fb: list[list[int]], flip_x: bool = False, flip_y: bool = False) -> None:
        for x in range(WIDTH):
            src = (WIDTH - 1 - x) if flip_x else x
            column = fb[src]
            if flip_y:
                column = column[::-1]
            if len(column) != HEIGHT:
                raise ValueError(f"column {src} has {len(column)} rows, expected {HEIGHT}")
            self.command(CMD_STAGE_COL, [x, *(max(0, min(255, int(v))) for v in column)])
        self.command(CMD_FLUSH, [0x00])

    def clear(self) -> None:
        blank = [[0] * HEIGHT for _ in range(WIDTH)]
        self.draw(blank)

    def sleep(self) -> None:
        try:
            self.clear()
        except Exception:
            pass
        try:
            self.command(CMD_SLEEP, [0x01])
        except Exception:
            pass


class DeviceManager:
    """Keeps every attached matrix showing the same frame. Reconnects on failure."""

    def __init__(self, preferred_port: str | None = None):
        self.preferred_port = preferred_port
        self.devices: list[MatrixDevice] = []
        self._last_attempt = 0.0
        self.just_connected = False
        self.status = "Looking for LED matrix..."

    def set_preferred_port(self, port: str | None) -> None:
        if port != self.preferred_port:
            self.preferred_port = port
            self.close()

    def close(self) -> None:
        for dev in self.devices:
            dev.close()
        self.devices.clear()

    def shutdown(self) -> None:
        for dev in self.devices:
            try:
                dev.sleep()
            except Exception:
                pass
            dev.close()
        self.devices.clear()

    def ensure(self) -> bool:
        now = time.monotonic()
        have = {d.port for d in self.devices if d.connected}
        if self.preferred_port:
            if have == {self.preferred_port}:
                return True
        else:
            wanted = set(list_matrix_ports())
            if wanted and wanted == have:
                return True
            if not wanted and have:
                return True
        if now - self._last_attempt < 2.0:
            return bool(have)
        self._last_attempt = now
        if not self.preferred_port and not list_matrix_ports() and not have:
            self.status = "Matrix not found"
            return False
        self.close()
        if self.preferred_port:
            ports = [self.preferred_port]
        else:
            ports = list_matrix_ports()
        if not ports:
            self.status = "Matrix not found"
            return False
        opened: list[MatrixDevice] = []
        errors: list[str] = []
        for port in ports:
            dev = MatrixDevice(port)
            try:
                dev.open()
            except Exception as exc:
                errors.append(f"{port}: {exc}")
                dev.close()
                continue
            opened.append(dev)
        self.devices = opened
        self.just_connected = bool(opened)
        if not opened:
            self.status = "Could not open LED matrix (" + "; ".join(errors) + ")"
            return False
        names = ", ".join(
            f"{d.port}" + (f" firmware {d.version}" if d.version else "") for d in opened
        )
        self.status = f"Connected to {names}"
        return True

    def set_brightness(self, level: int) -> None:
        dead: list[MatrixDevice] = []
        for dev in self.devices:
            try:
                dev.set_brightness(level)
            except Exception:
                log.exception("brightness failed on %s", dev.port)
                dev.close()
                dead.append(dev)
        for dev in dead:
            if dev in self.devices:
                self.devices.remove(dev)

    def draw(self, fb: list[list[int]], flip_x: bool, flip_y: bool) -> None:
        dead: list[MatrixDevice] = []
        for dev in self.devices:
            try:
                dev.draw(fb, flip_x=flip_x, flip_y=flip_y)
            except Exception:
                log.exception("draw failed on %s", dev.port)
                dead.append(dev)
        for dev in dead:
            dev.close()
            if dev in self.devices:
                self.devices.remove(dev)
        if dead:
            self.status = "LED matrix disconnected. Reconnecting..."

    def sleep_all(self) -> None:
        for dev in list(self.devices):
            try:
                dev.sleep()
            except Exception:
                dev.close()
