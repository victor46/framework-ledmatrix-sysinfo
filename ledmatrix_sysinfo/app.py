"""Tray app: sample Windows sensors and draw them on the LED matrix."""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import sys
import threading
import time
from ctypes import wintypes

from ledmatrix_sysinfo import __version__
from ledmatrix_sysinfo.ambient import AmbientLight
from ledmatrix_sysinfo.device import DeviceManager, describe_ports
from ledmatrix_sysinfo.metrics import Sampler, Snapshot
from ledmatrix_sysinfo.power import PowerMonitor
from ledmatrix_sysinfo.render import (
    BAT_Y,
    DEFAULT_STATS,
    STAT_CHOICES,
    STAT_LABEL,
    STAT_LETTER,
    STAT_SLOTS,
    ascii_frame,
    normalize_stats,
    render,
    self_test,
)

log = logging.getLogger("ledmatrix")

FRAME_SEC = 1.0
BRIGHTNESS_PRESETS = (
    (20, "Dim"),
    (40, "Low"),
    (70, "Medium"),
    (120, "High"),
    (200, "Max"),
)
STARTUP_NAME = "Framework LED Matrix Sysinfo.vbs"
MUTEX_NAME = "Local\\FrameworkLedMatrixSysinfo"


def config_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "framework-ledmatrix-sysinfo")


def config_path() -> str:
    return os.path.join(config_dir(), "config.json")


def startup_path() -> str:
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup", STARTUP_NAME)


def load_config() -> dict:
    cfg = {
        "brightness": 40,
        "auto_brightness": True,
        "flip_x": False,
        "flip_y": False,
        "port": None,
        "stats": list(DEFAULT_STATS),
    }
    try:
        with open(config_path(), encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update({k: stored[k] for k in cfg if k in stored})
    except FileNotFoundError:
        pass
    except Exception:
        log.warning("could not read %s, using defaults", config_path())
    cfg["stats"] = list(normalize_stats(cfg.get("stats")))
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(config_dir(), exist_ok=True)
    with open(config_path(), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)


def _pythonw() -> str:
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        candidate = exe[: -len("python.exe")] + "pythonw.exe"
        if os.path.isfile(candidate):
            return candidate
    return exe


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def install_startup() -> None:
    script = os.path.join(repo_root(), "sysinfo.pyw")
    pyw = _pythonw()
    target = startup_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # VBS doubles quotes inside a string. The Run line launches pythonw with no console.
    body = (
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.CurrentDirectory = "{repo_root()}"\r\n'
        f'sh.Run """{pyw}"" ""{script}""", 0, False\r\n'
    )
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    log.info("installed startup launcher %s", target)


def remove_startup() -> None:
    try:
        os.remove(startup_path())
        log.info("removed startup launcher")
    except FileNotFoundError:
        pass


def startup_installed() -> bool:
    return os.path.isfile(startup_path())


def human_rate(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / (1024 * 1024):.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


def _stat_phrase(snap: Snapshot, key: str) -> str:
    if key == "cpu":
        return f"CPU {snap.cpu:.0f}%"
    if key == "mem":
        return f"MEM {snap.mem:.0f}%"
    if key == "gpu":
        return f"GPU {snap.gpu:.0f}%" if snap.gpu_ok else "GPU n/a"
    if key == "ssd":
        if snap.ssd_is_percent:
            return f"SSD {snap.ssd:.0f}%"
        return f"SSD {human_rate(snap.ssd_bps)}"
    if key == "net":
        return f"NET {human_rate(snap.net_bps)}"
    return STAT_LABEL.get(key, key)


def setup_logging() -> None:
    os.makedirs(config_dir(), exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.FileHandler(os.path.join(config_dir(), "sysinfo.log"), encoding="utf-8")
    ]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=handlers)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Framework 16 LED Matrix system monitor")
    parser.add_argument("--port", help="COM port of the LED matrix. Default: auto-detect")
    parser.add_argument("--brightness", type=int, help="LED brightness 0-255 for this session")
    parser.add_argument("--flip-x", action="store_true", default=None, help="Mirror left to right")
    parser.add_argument("--flip-y", action="store_true", default=None, help="Flip top to bottom")
    parser.add_argument("--preview", action="store_true", help="Open a window that mirrors the matrix")
    parser.add_argument("--no-tray", action="store_true", help="Do not create a system tray icon")
    parser.add_argument("--no-device", action="store_true", help="Do not open the LED matrix")
    parser.add_argument("--demo", action="store_true", help="Show animated sample data")
    parser.add_argument("--dump", action="store_true", help="Print one ASCII frame and exit")
    parser.add_argument("--list-ports", action="store_true", help="List serial ports and exit")
    parser.add_argument("--self-test", action="store_true", help="Check the renderer and sensors, then exit")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    # store_true sets False when the flag is absent. None means "use the saved config".
    if "--flip-x" not in sys.argv and (argv is None or "--flip-x" not in argv):
        args.flip_x = None
    if "--flip-y" not in sys.argv and (argv is None or "--flip-y" not in argv):
        args.flip_y = None
    return args


class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = load_config()
        if args.brightness is not None:
            self.cfg["brightness"] = max(0, min(255, args.brightness))
            self.cfg["auto_brightness"] = False
        if args.flip_x is not None:
            self.cfg["flip_x"] = True
        if args.flip_y is not None:
            self.cfg["flip_y"] = True
        if args.port:
            self.cfg["port"] = args.port
        self.stop = threading.Event()
        self.sampler = Sampler()
        self.ambient = AmbientLight()
        self.devices = DeviceManager(self.cfg.get("port"))
        self.power = PowerMonitor()
        self.preview = None
        self.icon = None
        self.snap = Snapshot()
        self._snap_lock = threading.Lock()
        self._cfg_lock = threading.Lock()
        self._brightness_applied = None
        self._force_init = False
        self._mutex = None
        self._shown_status = None

    def status_text(self) -> str:
        with self._cfg_lock:
            stats = normalize_stats(self.cfg.get("stats"))
        with self._snap_lock:
            snap = self.snap
        if snap.battery_percent is None:
            bat = "n/a"
        else:
            bat = f"{snap.battery_percent:.0f}%"
            mode = snap.battery_mode()
            if mode == "charging":
                bat += " charging"
            elif mode == "full":
                bat += " full"
            elif mode == "discharging":
                bat += " in use"
        parts = [_stat_phrase(snap, key) for key in stats]
        parts.append(f"BAT {bat}")
        if self.args.no_device or self.args.demo:
            prefix = "Demo" if self.args.demo else "Sensors only"
        else:
            prefix = self.devices.status
        return f"{prefix} | {'  '.join(parts)}"

    def _acquire_mutex(self) -> bool:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self._mutex = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if kernel32.GetLastError() == 183:
            log.info("another copy is already running")
            return False
        return True

    def _set_stat(self, row: int, key: str) -> None:
        with self._cfg_lock:
            stats = list(normalize_stats(self.cfg.get("stats")))
            if key in stats:
                other = stats.index(key)
                stats[row], stats[other] = stats[other], stats[row]
            else:
                stats[row] = key
            self.cfg["stats"] = stats
            save_config(self.cfg)
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _set_brightness(self, level: int) -> None:
        with self._cfg_lock:
            self.cfg["brightness"] = level
            self.cfg["auto_brightness"] = False
            save_config(self.cfg)
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _toggle_flip(self, axis: str) -> None:
        with self._cfg_lock:
            self.cfg[axis] = not self.cfg.get(axis)
            save_config(self.cfg)
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def _toggle_startup(self) -> None:
        if startup_installed():
            remove_startup()
        else:
            install_startup()
        if self.icon is not None:
            try:
                self.icon.update_menu()
            except Exception:
                pass

    def open_preview(self) -> None:
        if self.preview is not None and getattr(self.preview, "alive", False):
            return
        try:
            from ledmatrix_sysinfo.preview import Preview
        except Exception:
            log.exception("preview window is unavailable")
            return
        self.preview = Preview()

    def _frame_snapshot(self, now: float) -> Snapshot:
        if self.args.demo:
            return _demo_snapshot(now)
        return self.sampler.sample(now)

    def loop(self) -> None:
        log.info("monitor loop started")
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    self._frame(started)
                except Exception:
                    log.exception("frame failed")
                    self.stop.wait(1.0)
                elapsed = time.monotonic() - started
                if elapsed > 8:
                    log.info("time jump of %.0fs, reinitializing the matrix", elapsed)
                    self._force_init = True
                    self.power.note_wake("time jump")
                remaining = FRAME_SEC - (time.monotonic() - started)
                if remaining > 0:
                    self.stop.wait(remaining)
        finally:
            if not self.args.no_device:
                self.devices.shutdown()
            self.sampler.close()
            self.ambient.close()
            self.power.close()
            log.info("monitor loop stopped")

    def _frame(self, now: float) -> None:
        if self.power.asleep and not self.args.demo:
            if not self.args.no_device and self.devices.ensure():
                self.devices.sleep_all()
            self.stop.wait(1.0)
            return

        snap = self._frame_snapshot(now)
        with self._cfg_lock:
            brightness = int(self.cfg.get("brightness") or 40)
            auto = bool(self.cfg.get("auto_brightness", True))
            flip_x = bool(self.cfg.get("flip_x"))
            flip_y = bool(self.cfg.get("flip_y"))
            stats = normalize_stats(self.cfg.get("stats"))
        if auto:
            auto_level = self.ambient.matrix_level()
            if auto_level is not None:
                brightness = auto_level
        fb = render(snap, now, "", stats)
        with self._snap_lock:
            self.snap = snap

        if self.power.check_and_clear_wake():
            self._force_init = True

        if not self.args.no_device:
            self.devices.ensure()
            if self.devices.status != self._shown_status:
                self._shown_status = self.devices.status
                if self.devices.status == "Matrix not found":
                    log.info("LED matrix not found. Seat the module, then run: py -3 sysinfo.pyw --list-ports")
                else:
                    log.info(self.devices.status)
            if self.devices.devices:
                if self.devices.just_connected or self._force_init or self._brightness_applied != brightness:
                    self.devices.set_brightness(brightness)
                    self.devices.just_connected = False
                    self._brightness_applied = brightness
                    self._force_init = False
                    detail = f"brightness {brightness}"
                    if auto and self.ambient.lux is not None:
                        detail += f" from {self.ambient.lux:.0f} lux"
                    log.info("%s, %s", self.devices.status, detail)
                self.devices.draw(fb, flip_x=flip_x, flip_y=flip_y)

        if self.preview is not None and getattr(self.preview, "alive", False):
            labels = [
                (STAT_LETTER[key], text_y, bar_y)
                for key, (text_y, bar_y) in zip(stats, STAT_SLOTS)
            ]
            labels.append(("BAT", BAT_Y, BAT_Y + 2))
            self.preview.update(fb, self.status_text(), labels)

        if self.icon is not None:
            try:
                self.icon.title = self.status_text()[:127]
            except Exception:
                pass

    def run_tray(self) -> None:
        import pystray
        from PIL import Image, ImageDraw

        image = Image.new("RGB", (64, 64), "#141414")
        draw = ImageDraw.Draw(image)
        for index, height in enumerate((40, 28, 16, 34)):
            x = 8 + index * 14
            draw.rectangle((x, 52 - height, x + 8, 52), fill="white")

        def brightness_action(level):
            def inner(_icon, _item):
                self._set_brightness(level)

            return inner

        def enable_auto(_icon, _item):
            with self._cfg_lock:
                self.cfg["auto_brightness"] = True
                save_config(self.cfg)
            self.icon.update_menu()

        def stat_action(row, key):
            def inner(_icon, _item):
                self._set_stat(row, key)

            return inner

        def stat_checked(row, key):
            def inner(_item):
                with self._cfg_lock:
                    stats = normalize_stats(self.cfg.get("stats"))
                return stats[row] == key

            return inner

        def stat_menu(row):
            return pystray.Menu(
                *(
                    pystray.MenuItem(
                        label,
                        stat_action(row, key),
                        checked=stat_checked(row, key),
                        radio=True,
                    )
                    for key, label, _letter in STAT_CHOICES
                )
            )

        row_names = ("Top", "Middle", "Bottom")
        menu = pystray.Menu(
            pystray.MenuItem(lambda _item: self.status_text()[:120], None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Stats",
                pystray.Menu(
                    *(
                        pystray.MenuItem(row_names[row], stat_menu(row))
                        for row in range(3)
                    )
                ),
            ),
            pystray.MenuItem(
                "Brightness",
                pystray.Menu(
                    pystray.MenuItem(
                        "Auto (room light)",
                        enable_auto,
                        checked=lambda _item: bool(self.cfg.get("auto_brightness", True)),
                        radio=True,
                    ),
                    *(
                        pystray.MenuItem(
                            label,
                            brightness_action(level),
                            checked=lambda _item, level=level: (
                                not self.cfg.get("auto_brightness", True)
                                and int(self.cfg.get("brightness") or 0) == level
                            ),
                            radio=True,
                        )
                        for level, label in BRIGHTNESS_PRESETS
                    ),
                ),
            ),
            pystray.MenuItem(
                "Flip vertical",
                lambda _icon, _item: self._toggle_flip("flip_y"),
                checked=lambda _item: bool(self.cfg.get("flip_y")),
            ),
            pystray.MenuItem(
                "Flip horizontal",
                lambda _icon, _item: self._toggle_flip("flip_x"),
                checked=lambda _item: bool(self.cfg.get("flip_x")),
            ),
            pystray.MenuItem(
                "Start with Windows",
                lambda _icon, _item: self._toggle_startup(),
                checked=lambda _item: startup_installed(),
            ),
            pystray.MenuItem(
                "Show preview",
                self._toggle_preview,
                checked=lambda _item: self.preview is not None and getattr(self.preview, "alive", False),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self.quit),
        )
        self.icon = pystray.Icon("framework-ledmatrix", image, "Framework LED Matrix", menu)
        log.info("tray icon ready")
        self.icon.run()

    def _toggle_preview(self, _icon, _item) -> None:
        if self.preview is not None and getattr(self.preview, "alive", False):
            self.preview.close()
        else:
            self.open_preview()
        if self.icon is not None:
            self.icon.update_menu()

    def quit(self, _icon=None, _item=None) -> None:
        log.info("quit")
        self.stop.set()
        if self.preview is not None:
            self.preview.close()
        worker = getattr(self, "worker", None)
        if worker is not None and worker.is_alive() and threading.current_thread() is not worker:
            worker.join(timeout=3)
        if self.icon is not None:
            self.icon.stop()

    def run(self) -> int:
        if not self._acquire_mutex():
            ctypes.windll.user32.MessageBoxW(
                None,
                "Framework LED Matrix sysinfo is already running.\nLook for its icon in the system tray.",
                "LED Matrix",
                0x40,
            )
            return 0
        regs = ", ".join(f"{name}={'ok' if ok else 'fail'}" for name, ok in self.power.registrations.items())
        log.info("Framework LED Matrix sysinfo %s", __version__)
        log.info("power notifications: %s", regs or "none")
        log.info("rows: three chosen stats, battery, then the clock")
        self.worker = threading.Thread(target=self.loop, name="matrix", daemon=False)
        self.worker.start()
        if self.args.preview:
            self.open_preview()
        if self.args.no_tray:
            try:
                while not self.stop.is_set():
                    self.stop.wait(0.5)
            except KeyboardInterrupt:
                self.quit()
            self.worker.join(timeout=3)
            return 0
        try:
            self.run_tray()
        except ImportError:
            log.exception("system tray is unavailable; running in the console instead")
            try:
                while not self.stop.is_set():
                    self.stop.wait(0.5)
            except KeyboardInterrupt:
                self.quit()
        except KeyboardInterrupt:
            self.quit()
        finally:
            self.stop.set()
            if self.worker.is_alive():
                self.worker.join(timeout=3)
        return 0


def _demo_snapshot(now: float) -> Snapshot:
    import math

    cpu = 50 + 45 * math.sin(now * 0.7)
    ssd = max(0.0, 50 * math.sin(now * 2.0))
    down = 1_800_000 * max(0.0, math.sin(now * 1.4))
    return Snapshot(
        cpu=cpu,
        mem=62.0,
        ssd=ssd,
        ssd_is_percent=True,
        ssd_bps=20 * 1024 * 1024,
        battery_percent=76.0,
        battery_plugged=True,
        battery_charging=True,
        net_up_bps=90_000,
        net_down_bps=down,
        net_bar=40 + 30 * math.sin(now),
        rx_vis=80 if math.sin(now * 3) > 0 else 8,
        tx_vis=25,
        gpu=15 + 25 * math.sin(now * 0.5),
        gpu_ok=True,
    )


def _print_dump(args: argparse.Namespace) -> int:
    if args.demo:
        snap = _demo_snapshot(time.monotonic())
    else:
        sampler = Sampler()
        try:
            sampler.sample(time.monotonic())
            time.sleep(0.4)
            snap = sampler.sample(time.monotonic())
        finally:
            sampler.close()
    stats = normalize_stats(load_config().get("stats"))
    fb = render(snap, time.monotonic(), "", stats)
    print(ascii_frame(fb))
    print()
    bat = "n/a" if snap.battery_percent is None else f"{snap.battery_percent:.0f}%"
    parts = [_stat_phrase(snap, key) for key in stats]
    parts.append(f"BAT {bat}")
    print("  ".join(parts))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.version:
        print(__version__)
        return 0
    if args.list_ports:
        print("\n".join(describe_ports()))
        return 0
    if args.self_test:
        self_test()
        print("renderer ok")
        if sys.platform == "win32":
            sampler = Sampler()
            try:
                sampler.sample(time.monotonic())
                time.sleep(0.6)
                snap = sampler.sample(time.monotonic())
            finally:
                sampler.close()
            print(
                f"sensors  CPU {snap.cpu:.1f}%  MEM {snap.mem:.1f}%  "
                f"SSD {snap.ssd:.1f} ({'percent' if snap.ssd_is_percent else 'rate'})  "
                f"BAT {snap.battery_percent} plugged={snap.battery_plugged}  "
                f"GPU {snap.gpu:.1f}% ok={snap.gpu_ok}  "
                f"down {human_rate(snap.net_down_bps)} up {human_rate(snap.net_up_bps)}"
            )
            print("ports:")
            print("\n".join(describe_ports()))
        return 0
    if sys.platform != "win32":
        print("This monitor runs on Windows.")
        return 1
    setup_logging()
    if args.dump:
        return _print_dump(args)
    try:
        return App(args).run()
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
