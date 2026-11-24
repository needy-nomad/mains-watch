# mains-watch

A small Python script that tells a desktop when mains power has gone, using a Wi-Fi device as the sensor. It switches to power saver during the cut, hibernates if the cut lasts too long, and restores normal performance when power returns. It works on Windows and Linux and needs only the standard library.

## The use case

A desktop on a UPS or inverter can ride out short cuts, but the UPS usually can't tell the PC anything. Many home UPS units and inverters have no data port, so the PC has no idea mains has gone.

This script works around that with an inexpensive sensor:

- Plug a Wi-Fi smart plug or smart device into a wall socket that is **not** on the UPS.
- Keep the router and Wi-Fi on backup power, so the router keeps answering during a cut.
- Keep the PC on the UPS.

When mains fails, the sensor drops off the network but the router is still up. The PC sees that pattern and reacts. A Wi-Fi hiccup where both the sensor and router are silent is ignored, so it won't trigger a false power-saver switch.

Any device that answers ping and is on a non-backed-up socket works. A smart clock, a smart plug, or a smart bulb all fit.

## How it works

| Sensor | Router | Meaning | Action |
|---|---|---|---|
| answers | — | mains on | restore normal after 2 consecutive hits |
| silent | answers | mains off | power saver after 3 consecutive misses (~9 s) |
| silent | silent | Wi-Fi or router problem | no change |
| silent for `HIBERNATE_AFTER` | — | long outage | hibernate once |

Actions by platform:

- **Linux:** `powerprofilesctl set power-saver` / `balanced`, and `systemctl hibernate`.
- **Windows:** `powercfg /setactive SCHEME_MAX` / `SCHEME_BALANCED`, and `shutdown /h`.

## Setup

1. Give the sensor a fixed IP with a DHCP reservation in your router.
2. Plug the sensor into a socket that is not on the UPS or inverter.
3. Edit the settings at the top of `mains_watch.py`:
   - `SENSOR_IP`: the sensor's IP.
   - `ROUTER_IP`: your router's IP.
   - `HIBERNATE_AFTER`: seconds on battery before hibernating, or `None` to disable.
4. Run it:

   ```bash
   python3 mains_watch.py
   ```

   On Windows, run `python mains_watch.py`. Use `pythonw` to run without a console window.

Events are logged to the console and to `mains_watch.log`.

## Testing

Run the state-machine tests with simulated outages:

```bash
python3 -m unittest discover -s tests
```

To test against real hardware, unplug the sensor and watch the log for `Mains lost`, then plug it back in and watch for `Mains back`.

## Limitations

- The sensor must be on a non-backed-up socket. If it's on the UPS, the script never sees a cut.
- The sensor must answer ping. Some devices ignore ICMP, so test it first.
- Hibernation needs the OS to allow it. On Linux, the desktop user needs permission to run `systemctl hibernate`.
- If the router also loses power, the script can't tell a mains cut from a network problem, so it does nothing. Keep the router on backup power.
- This is a convenience tool, not a safety system. Don't rely on it for data safety. Save your work and use proper UPS shutdown software where it matters.

## License

Released under the MIT License. See [LICENSE](LICENSE).
