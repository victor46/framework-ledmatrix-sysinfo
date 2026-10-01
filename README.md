# Framework 16 LED Matrix system monitor

A Windows tray app for the [Framework Laptop 16 LED Matrix](https://github.com/FrameworkComputer/inputmodule-rs/blob/main/ledmatrix/README.md). It draws live system stats on the module's 9×34 white LED grid and updates once a second.

The grid is only nine pixels wide. You choose three of CPU, memory, GPU, network, and SSD. Each chosen stat is its first letter and a bar. Battery and the clock stay on the module.

```
C
####          one-line bar, then a blank row
M
######
G
##
###           battery level
 >            ends stay lit; the middle moves
###
21            hours
05            minutes
    *         second hand
```

The letters are C for CPU, M for memory, G for GPU, N for network, and S for SSD. The picture above is the default: C, M, then G. Each letter has a single-line bar underneath and a blank row below the bar.

| Letter | What the bar shows |
| --- | --- |
| C | Processor utilization |
| M | Memory in use |
| G | Busiest GPU's 3D engine, the same counters Task Manager uses |
| N | Combined upload and download. The bar fills on a curve, and it is full at about 100 MB/s |
| S | How busy the SSD is |
| Battery | Three-line bar. Top and bottom show the charge. On the middle line the first and last lights stay on. The lights between them move right while charging and left while the battery is in use, and match the charge when it is holding steady |

Hours and minutes are large digits on their own lines, with a blank column between the two digits. One row of nine lights cannot hold a readable hours-and-minutes. The bottom row walks one light across the minute.

## Install on Windows

You need Python 3.10 or newer, with the `py` launcher. From this folder:

```bat
py -3 -m pip install -r requirements.txt
```

That installs the serial, sensor, tray, and image libraries listed in `requirements.txt`.

## Run it

```bat
py -3 sysinfo.pyw
```

`run.bat` does the same launch. The process sits in the system tray. A second copy will not start; Windows shows a message pointing you at the icon that is already running. Quit from the tray menu. The matrix is cleared when the app exits.

Useful checks:

```bat
py -3 sysinfo.pyw --list-ports
py -3 sysinfo.pyw --self-test
py -3 sysinfo.pyw --dump
py -3 sysinfo.pyw --preview
```

`--list-ports` should show a COM port marked `LED Matrix` (USB VID `32AC`, PID `0020`). `--self-test` checks the drawing code and prints one sensor sample. `--dump` prints one text picture of the current frame. `--preview` opens a window that mirrors the grid.

If another program already has the COM port open, this app cannot draw until that program quits.

## Tray menu

Right-click the tray icon. The top line is the three stats you picked, plus the battery. Hovering the icon shows the same line.

- **Stats** chooses the three letters. **Top**, **Middle**, and **Bottom** each list CPU, Memory, GPU, Network, and SSD. Picking a stat that is already on another row swaps the two rows.
- **Brightness** follows the room by default, using the same ambient-light steps as the power button. Pick Dim, Low, Medium, High, or Max to hold one level instead.
- **Flip vertical** and **Flip horizontal** turn the picture if it is upside down or mirrored on your module.
- **Start with Windows** adds a launcher to your user Startup folder. It starts with no console window.
- **Show preview** opens or closes the mirror window.
- **Quit** stops the app and clears the matrix.

While the laptop sleeps or the lid is closed, the app clears the matrix and tells it to sleep. That keeps the module from falling back to its built-in animation when USB power-cycles during Modern Standby. It redraws after wake.

Settings are saved in `%APPDATA%\framework-ledmatrix-sysinfo\config.json`. A log is written beside it as `sysinfo.log`.

## If the clock is at the top

The firmware treats the first byte of each column as the top of a 9-wide, 34-tall image. If your module shows the clock at the top, turn on **Flip vertical** in the tray menu.

To target one module when more than one is plugged in:

```bat
py -3 sysinfo.pyw --port COM5
```

Both modules get the same picture when the port is left on auto.
