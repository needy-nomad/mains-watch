#!/usr/bin/env python3
"""Interactive setup: finds your sensor by watching which device disappears when you
unplug it, then writes /etc/mains-watch.conf. Run by install.sh (needs root).

    sudo ./install.sh --setup
"""

import ipaddress
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def ping(ip: str) -> bool:
    return subprocess.run(["ping", "-c", "1", "-W", "1", ip], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def local_network():
    """(gateway, own_ip, /24 network) from the default route, or None."""
    route = subprocess.run(["ip", "-4", "route", "show", "default"],
                           capture_output=True, text=True).stdout.split()
    if "via" not in route or "dev" not in route:
        return None
    gateway, dev = route[route.index("via") + 1], route[route.index("dev") + 1]
    addr = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", dev],
                          capture_output=True, text=True).stdout
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", addr)
    if not m:
        return None
    own = m.group(1)
    return gateway, own, ipaddress.ip_network(f"{own}/24", strict=False)


def scan(network, ping_fn=ping) -> set:
    """Addresses that answer ping, probed twice so one lost packet doesn't hide a device."""
    hosts = [str(h) for h in network.hosts()]
    alive = set()
    with ThreadPoolExecutor(max_workers=64) as pool:
        for _ in range(2):
            alive |= {h for h, ok in zip(hosts, pool.map(ping_fn, hosts)) if ok}
    return alive


def vanished(before: set, after: set, ignore: set) -> list:
    """Devices seen before the unplug and gone after, sorted by address."""
    return sorted(before - after - ignore, key=ipaddress.ip_address)


def set_key(path: str, key: str, value) -> None:
    """Set KEY=value in a KEY=VALUE config file, keeping everything else."""
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    new = f"{key}={value}"
    for i, line in enumerate(lines):
        if re.match(rf"\s*{key}\s*=", line):
            lines[i] = new
            break
    else:
        lines.append(new)
    p.write_text("\n".join(lines) + "\n")


def ask(prompt: str, default: str = "", input_fn=input) -> str:
    answer = input_fn(f"{prompt}" + (f" [{default}]" if default else "") + ": ").strip()
    return answer or default


def pick_sensor(net, gateway, own, input_fn, ping_fn, say) -> str:
    ignore = {gateway, own}
    say(f"\nScanning {net} for devices (about 10 s)...")
    before = scan(net, ping_fn) - ignore
    say(f"Found {len(before)} devices answering ping.")
    if not before:
        return ask("No devices found. Type the sensor's IP", input_fn=input_fn)

    say("\nNow UNPLUG the sensor (the Wi-Fi device on the wall socket that is NOT on the UPS).")
    input_fn("Press Enter once it is unplugged... ")
    time.sleep(1 if ping_fn is not ping else 4)       # let it drop off the network
    after = scan(net, ping_fn)
    gone = vanished(before, after, ignore)

    if len(gone) == 1:
        say(f"\nThat device went offline: {gone[0]}")
        sensor = gone[0] if ask("Use it as the sensor? (y/n)", "y", input_fn).lower().startswith("y") \
            else ask("Type the sensor's IP", input_fn=input_fn)
    elif gone:
        say("\nSeveral devices went offline:")
        for i, ip in enumerate(gone, 1):
            say(f"  {i}. {ip}")
        choice = ask("Number of the sensor (or type an IP)", "1", input_fn)
        sensor = gone[int(choice) - 1] if choice.isdigit() and 0 < int(choice) <= len(gone) else choice
    else:
        say("\nNo device disappeared. It may not answer ping, or it was not unplugged.")
        sensor = ask("Type the sensor's IP, or press Enter to quit", input_fn=input_fn)

    if sensor:
        input_fn("Now plug the sensor back in and press Enter... ")
        for _ in range(10):
            if ping_fn(sensor):
                say(f"{sensor} is back online.")
                break
            time.sleep(0 if ping_fn is not ping else 2)
        else:
            say(f"Warning: {sensor} has not come back yet; check it is plugged in.")
    return sensor


def main(conf: str, input_fn=input, ping_fn=ping, say=print, network=None) -> int:
    say("mains-watch setup\n=================")
    net_info = network or local_network()
    if not net_info:
        say("No default network route found. Connect to your Wi-Fi/LAN first.")
        return 1
    gateway, own, net = net_info
    say(f"This PC: {own}   Router: {gateway}")

    if shutil.which("nvidia-smi"):
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=name,power.draw", "--format=csv,noheader"],
                             capture_output=True, text=True).stdout.strip()
        say(f"GPU: {gpu or 'nvidia-smi found but returned nothing'}")
    else:
        say("Warning: nvidia-smi not found; GPU halting will be disabled (power-saver still works).")

    sensor = pick_sensor(net, gateway, own, input_fn, ping_fn, say)
    if not sensor:
        say("No sensor chosen; nothing written.")
        return 1

    say("\nSettings (press Enter to keep the default)")
    cap = ask("GPU power cap in watts while on battery", "150", input_fn)
    every = ask("Seconds between sensor checks", "1", input_fn)
    confirm = ask("Missed checks in a row before a power cut is declared", "3", input_fn)

    for key, value in (("SENSOR_IP", sensor), ("GPU_CAP_WATTS", cap),
                       ("CHECK_EVERY", every), ("OUTAGE_CONFIRM", confirm)):
        set_key(conf, key, value)
    say(f"\nSaved to {conf}. Edit it any time, then: sudo systemctl restart mains-watch")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/etc/mains-watch.conf"))
