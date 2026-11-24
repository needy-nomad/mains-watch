# mains-watch

Halts your NVIDIA GPU the moment mains power fails, and brings it back when power returns. It keeps the GPU under a power cap (150 W by default) while you run on a UPS or inverter. It starts by itself at boot on Linux and Windows, has a local web dashboard and is set up in the browser.

It also switches the CPU to power saver during a cut, and can optionally hibernate if the cut lasts too long.

## Why

Most home UPS units and inverters have no data port, so the PC cannot know that mains has gone. A GPU running a training job or render can pull 400 W or more and drain the battery within minutes. This tool detects the cut with an inexpensive sensor and freezes the GPU work immediately.

- Plug a Wi-Fi smart plug or any ping-able Wi-Fi device into a wall socket that is **not** on the UPS. This is the sensor.
- Keep the router on backup power, and the PC on the UPS.
- When mains fails, the sensor drops off the network while the router keeps answering. That pattern means "power cut".

## What it does

| Sensor | Router | Meaning | Action |
|---|---|---|---|
| answers | | mains on | after 2 good checks: unlock GPU clock, continue frozen jobs, restore normal CPU profile |
| silent for 3 checks | answers | mains off | freeze every GPU job, lock the GPU clock to its minimum, CPU power saver |
| silent | silent | Wi-Fi or router problem | nothing (ambiguous) |
| silent for `HIBERNATE_AFTER` | | long outage | hibernate once (off by default) |

- **Freezing**: processes holding a CUDA context are suspended (`SIGSTOP` on Linux, `NtSuspendProcess` on Windows). Nothing is killed; memory stays allocated and work continues after resume. Jobs started during an outage are frozen too.
- **150 W cap**: consumer cards have a hardware minimum power limit far above 150 W (for example 400 W on an RTX 5090), so `nvidia-smi -pl` cannot enforce it. A GPU with no running kernels idles at a few tens of watts, so freezing is what keeps it under the cap. The cap is checked, and a warning is logged if the card exceeds it while halted.
- **Safe by default**: stopping the service always releases frozen jobs. If the watcher crashes or restarts mid-outage, the GPU stays frozen until the sensor answers again. A reused process id is never signalled.

## Requirements

- Linux with systemd, or Windows 10/11
- Python 3.8+ (standard library only)
- An NVIDIA GPU with `nvidia-smi` (without it, only the CPU power-saver part works)
- A sensor on a non-UPS socket that answers ping (or a TCP port or HTTP page, see `SENSOR_CHECK`)

## Install

### Linux

```bash
git clone https://github.com/needy-nomad/mains-watch
cd mains-watch
./install.sh
```

The script asks for your sudo password (it needs root to run at boot and to control the GPU), installs to `/opt/mains-watch`, enables the systemd service, runs the restart check and opens the setup page in your browser.

### Windows

Open PowerShell in the project folder:

```powershell
.\install.ps1
```

Windows asks for permission (UAC). The script registers a Task Scheduler task that runs at boot as SYSTEM, even before anyone logs in, and is allowed to run on battery. Install Python for **all users** (python.org installer, "Install for all users"), because a per-user Python cannot be used by SYSTEM.

> Windows support is written but has **not** been tested on a real Windows machine. Run `.\install.ps1 -Check` after installing and report problems. `nvidia-smi` on Windows may not list GPU processes in the default WDDM driver mode; if it lists none, nothing can be frozen there.

## First-run setup (in the browser)

The installer opens `http://127.0.0.1:8099/setup`:

1. **Scan**: finds devices on your network.
2. **Unplug the sensor** and press the button. The page shows which device disappeared; click it. If none disappears (the device ignores ping), type its IP.
3. **Plug it back in**; the page confirms it answers again.
4. **Settings**: GPU power cap, check interval, and how many missed checks declare a power cut.
5. **Save and start**.

Prefer no browser? `./install.sh 192.168.1.50` or `.\install.ps1 -SensorIp 192.168.1.50` sets the sensor directly. Give the sensor a fixed IP (DHCP reservation in your router).

## Dashboard

`http://127.0.0.1:8099` (also the "Mains Watch" entry in the app menu / Start menu). It is reachable only from this computer.

- Current state, power cuts all time and today, total time without mains, GPU power, frozen jobs and history (kept across reboots).
- **Pause protection** for 1 hour, 8 hours or until you turn it back on. Use this for a broken, old or noisy sensor. While paused, a cut does not halt the GPU, a banner says so, and anything already frozen is released.
- **Run restart check**: see below.
- **Settings / change sensor** reopens the setup page.

Changing settings or pausing is only accepted from the dashboard page itself; other websites open in your browser cannot do it.

## Will it work after a restart?

```bash
./install.sh --check            # Linux
.\install.ps1 -Check            # Windows
python3 mains_watch.py --check  # either (exit code 0 = yes)
```

The same check is on the dashboard. It verifies the things that decide whether protection survives a reboot:

- the service or task is enabled to start at boot, and restarts itself if it crashes
- the installed program files exist; it runs with administrator rights (and, on Windows, as SYSTEM, allowed on battery, with a Python SYSTEM can use)
- the network connection connects automatically before login for all users (NetworkManager on Linux). Otherwise the sensor cannot be reached after a reboot until you log in
- settings are saved and the data folders are writable

It also reports, as warnings, whether the sensor and router answer now, the GPU is readable and protection is not paused. The installer runs the check at the end.

## Configuration

Linux: `/etc/mains-watch.conf`. Windows: `C:\ProgramData\mains-watch\mains-watch.conf`. One `KEY=value` per line; `#` starts a comment. Restart the service after editing (`sudo systemctl restart mains-watch`). Environment variables override the file.

| Key | Default | Meaning |
|---|---|---|
| `SENSOR_IP` | (set by setup) | Address of the sensor |
| `SENSOR_CHECK` | `ping` | How to probe the sensor: `ping`, `tcp:<port>`, or an `http://host/path` URL (any reply counts) |
| `ROUTER_IP` | default gateway | Router that stays up during a cut |
| `CHECK_EVERY` | `1.0` | Seconds between checks |
| `OUTAGE_CONFIRM` | `3` | Missed checks in a row before a cut is declared |
| `RESTORE_CONFIRM` | `2` | Good checks in a row before power is considered back |
| `GPU_CAP_WATTS` | `150` | Cap checked while halted |
| `GPU_LOCK_MHZ` | lowest supported | Graphics clock while halted |
| `GPU_POLL_EVERY` | `10.0` | Seconds between GPU power readings (dashboard and cap check) |
| `HIBERNATE_AFTER` | none | Seconds on battery before hibernating |
| `DASHBOARD_PORT` | `8099` | Local dashboard port, `0` disables it |
| `LOG_FILE` | none | Extra log file (Windows installer sets one) |
| `STATE_FILE`, `PAUSE_FILE`, `EVENTS_FILE` | system data folders | Where state, pause switch and history live |

## Logs and uninstall

Linux: `journalctl -u mains-watch -f`. Windows: `C:\ProgramData\mains-watch\mains-watch.log`.

```bash
./install.sh --uninstall        # Linux
.\install.ps1 -Uninstall        # Windows
```

Uninstalling releases any frozen jobs first; your config and history are kept.

## Tests

```bash
python3 -m unittest discover -s tests
```

The suite uses fake `ping`, `nvidia-smi` and `powerprofilesctl` and real stand-in processes, and covers the state machine, freezing and resuming, late starters, crashes and restarts, pausing, the dashboard and its request guards, the setup flow and the restart check. It runs on Linux only.

## Limitations

- The sensor must be on a non-backed-up socket, and the router on backup power. If the router also loses power the tool cannot tell a cut from a network problem and does nothing.
- Only NVIDIA GPUs are controlled. Jobs that talk to a remote service may time out while frozen.
- Locking clocks and freezing other users' processes needs root or Administrator.
- This is a convenience tool, not a safety system. Do not rely on it for data safety; use proper UPS shutdown software where it matters.

## License

MIT. See [LICENSE](LICENSE).
