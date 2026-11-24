"""Tiny local web dashboard: current status, outage counts and history.

Served by the daemon itself on 127.0.0.1 (stdlib only). Events are appended to a
JSON-lines file by `record()`.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def record(path: str, event: str, **fields) -> None:
    """Append one event ({'t': epoch, 'event': ..., ...}) to the history file."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "event": event, **fields}) + "\n")
    except OSError:
        pass


class Pause:
    """Pause/resume switch for the sensor-triggered halt, kept in a file so it survives restarts.
    The file holds {"until": epoch} (timed) or {"until": null} (until switched back on)."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def until(self):
        """None if not paused; float('inf') if paused indefinitely; else the end time."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        until = data.get("until")
        if until is None:
            return float("inf")
        if time.time() >= until:
            self.clear()
            return None
        return float(until)

    def paused(self) -> bool:
        return self.until() is not None

    def set(self, hours) -> dict:
        try:
            hours = float(hours or 0)
            if not 0 <= hours <= 24 * 365:
                raise ValueError
        except (TypeError, ValueError):
            return {"error": "Invalid duration."}
        until = time.time() + hours * 3600 if hours else None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"until": until}))
        except OSError as exc:
            return {"error": f"Cannot save pause state: {exc}"}
        return {"ok": True}

    def clear(self) -> dict:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return {"error": str(exc)}
        return {"ok": True}


def read_events(path: str) -> list:
    events = []
    try:
        for line in Path(path).read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
    except OSError:
        pass
    return events


def summary(events: list, now: float) -> dict:
    """Outage counts and total downtime. `restored` events carry the outage duration."""
    midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    lost = [e for e in events if e["event"] == "lost"]
    done = [e for e in events if e["event"] == "restored"]
    return {
        "outages_total": len(lost),
        "outages_today": sum(e["t"] >= midnight for e in lost),
        "downtime_total_s": sum(e.get("duration_s", 0) for e in done),
        "longest_s": max((e.get("duration_s", 0) for e in done), default=0),
        "recent": events[-30:][::-1],
    }


PAGE = """<!doctype html><meta charset=utf-8><title>Mains watch</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{font:16px system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
.card{border-radius:12px;padding:1.2rem;margin-bottom:1rem;background:#1c1c1c}
#state{font-size:2rem;font-weight:700}.ok{color:#4ade80}.bad{color:#f87171}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1rem}
.n{font-size:1.8rem;font-weight:700}.l{color:#9ca3af;font-size:.85rem}
.hide{display:none}#pausebar{background:#78350f}button{font:inherit;padding:.3rem .7rem;margin:.2rem .3rem 0 0;border:0;border-radius:6px;background:#374151;color:#eee;cursor:pointer}
table{width:100%;border-collapse:collapse}td{padding:.3rem .2rem;border-top:1px solid #333}
</style>
<div class="card hide" id=pausebar><b>Protection is paused</b> <span id=pausetxt></span> - a power cut will NOT halt the GPU. <button onclick="pauseCall('/api/unpause')">Turn protection back on</button></div>
<div class=card><div id=state>...</div><div class=l id=sub></div><div class=l><a href=/setup style=color:#9ca3af>Settings / change sensor</a></div>
<div class=l id=pausectl>Sensor broken, old or a nuisance? Pause protection:
<button onclick="pauseCall('/api/pause',1)">1 hour</button><button onclick="pauseCall('/api/pause',8)">8 hours</button><button onclick="pauseCall('/api/pause',0)">until I turn it on</button></div></div>
<div class="card grid">
<div><div class=n id=tot>-</div><div class=l>power cuts, all time</div></div>
<div><div class=n id=today>-</div><div class=l>today</div></div>
<div><div class=n id=down>-</div><div class=l>total time without mains</div></div>
<div><div class=n id=gpu>-</div><div class=l>GPU power now (cap <span id=cap></span> W)</div></div>
<div><div class=n id=frozen>-</div><div class=l>GPU jobs frozen</div></div>
</div>
<div class=card><b>Will it work after a restart?</b>
<button onclick=runCheck()>Run restart check</button><div id=verdict style="margin:.6rem 0;font-weight:700"></div><table id=checks></table></div>
<div class=card><b>History</b><table id=hist></table></div>
<script>
const dur=s=>s<60?Math.round(s)+' s':s<3600?Math.round(s/60)+' min':(s/3600).toFixed(1)+' h';
const $=i=>document.getElementById(i);
const pauseCall=async(u,h)=>{await fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-Setup':'1'},body:JSON.stringify({hours:h})});tick()};
async function tick(){try{
 const d=await (await fetch('/api/status')).json();
 if(d.setup_needed){location='/setup';return}
 $('pausebar').classList.toggle('hide',!d.paused);$('pausectl').classList.toggle('hide',!!d.paused);
 $('pausetxt').textContent=d.paused_until?'until '+new Date(d.paused_until*1000).toLocaleString():'until you turn it back on';
 $('state').textContent=d.on_battery?'POWER CUT - GPU halted':'Mains OK';
 $('state').className=d.on_battery?'bad':'ok';
 $('sub').textContent=d.on_battery?'since '+new Date(d.since*1000).toLocaleTimeString()+' ('+dur(Date.now()/1000-d.since)+')':'sensor '+d.sensor+' answering';
 $('tot').textContent=d.outages_total;$('today').textContent=d.outages_today;
 $('down').textContent=dur(d.downtime_total_s);
 $('gpu').textContent=d.gpu_watts==null?'n/a':Math.round(d.gpu_watts)+' W';
 $('cap').textContent=d.cap_watts;$('frozen').textContent=d.frozen;
 $('hist').innerHTML=d.recent.map(e=>'<tr><td>'+new Date(e.t*1000).toLocaleString()+'</td><td>'+
  (e.event=='paused'?'protection paused':e.event=='unpaused'?'protection turned back on':e.event=='lost'?'power lost':e.event=='restored'?'power back (cut lasted '+dur(e.duration_s)+')':e.event)+'</td></tr>').join('');
}catch(e){$('state').textContent='service not reachable';$('state').className='bad'}}
async function runCheck(){$('verdict').textContent='Checking...';
 try{const v=await (await fetch('/api/check')).json();
  $('verdict').textContent=v.restart_proof?'Yes - it will start by itself after a restart.':'NOT restart-proof: '+v.failed.join(', ');
  $('verdict').className=v.restart_proof?'ok':'bad';
  $('checks').innerHTML=v.checks.map(c=>'<tr><td>'+({ok:'OK',warn:'warn',fail:'FAIL'}[c.status])+'</td><td>'+c.name+'</td><td class=l>'+c.detail+'</td></tr>').join('')
 }catch(e){$('verdict').textContent='check failed'}}
tick();setInterval(tick,2000);
</script>"""

SETUP_PAGE = """<!doctype html><meta charset=utf-8><title>Mains watch setup</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
body{font:16px system-ui,sans-serif;max-width:620px;margin:2rem auto;padding:0 1rem;background:#111;color:#eee}
.card{border-radius:12px;padding:1.2rem;margin-bottom:1rem;background:#1c1c1c}
h1{font-size:1.4rem}h2{margin:0 0 .6rem;font-size:1.1rem}.l{color:#9ca3af;font-size:.9rem}
button{font:inherit;padding:.6rem 1.1rem;border:0;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer;margin:.3rem .3rem 0 0}
button.alt{background:#374151}button:disabled{opacity:.5}
input{font:inherit;padding:.5rem;border-radius:8px;border:1px solid #444;background:#111;color:#eee;width:8rem}
label{display:block;margin:.5rem 0}.err{color:#f87171}.ok{color:#4ade80}.hide{display:none}
.opt{display:block;margin:.3rem 0}
</style>
<h1>Mains watch setup</h1>
<div class=card id=s1><h2>1. Find your sensor</h2>
<p class=l>The sensor is the Wi-Fi device on a wall socket that is <b>not</b> on the UPS. Plug it in, then scan.</p>
<button id=scan>Scan network</button><div id=m1 class=l></div></div>
<div class="card hide" id=s2><h2>2. Unplug the sensor</h2>
<p class=l>Unplug it from the wall now, then press the button. I will see which device disappears.</p>
<button id=unp>I have unplugged it</button><div id=m2 class=l></div><div id=pick></div></div>
<div class="card hide" id=s3><h2>3. Plug it back in</h2>
<p>Sensor: <b id=ip></b></p><button id=back>I have plugged it back in</button><div id=m3 class=l></div></div>
<div class="card hide" id=s4><h2 id=h4>4. Settings</h2>
<label>GPU power cap while on battery (W) <input id=cap type=number value=150></label>
<label>Seconds between checks <input id=every type=number step=0.1 value=1></label>
<label>Missed checks before a power cut is declared <input id=confirm type=number value=3></label>
<label>Good checks before power is considered back <input id=restore type=number value=2></label>
<label>Seconds between GPU power readings <input id=poll type=number step=1 value=10></label>
<button id=save>Save and start</button><div id=m4 class=l></div></div>
<script>
const $=i=>document.getElementById(i);let sensor='';
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-Setup':'1'},body:JSON.stringify(b||{})}).then(r=>r.json());
const busy=(b,m,t)=>{b.disabled=true;m.className='l';m.textContent=t};
const fail=(b,m,t)=>{b.disabled=false;m.className='err';m.textContent=t};
fetch('/api/setup/config').then(r=>r.json()).then(c=>{if(c.GPU_CAP_WATTS)$('cap').value=c.GPU_CAP_WATTS;
 if(c.CHECK_EVERY)$('every').value=c.CHECK_EVERY;if(c.OUTAGE_CONFIRM)$('confirm').value=c.OUTAGE_CONFIRM;
 if(c.RESTORE_CONFIRM)$('restore').value=c.RESTORE_CONFIRM;if(c.GPU_POLL_EVERY)$('poll').value=c.GPU_POLL_EVERY;
 if(c.SENSOR_IP){sensor=c.SENSOR_IP;$('h4').textContent='Settings (sensor '+sensor+')';$('s4').classList.remove('hide');$('save').textContent='Save settings'}});
$('scan').onclick=async()=>{busy($('scan'),$('m1'),'Scanning, about 10 seconds...');
 const r=await post('/api/setup/scan');if(r.error)return fail($('scan'),$('m1'),r.error);
 $('m1').className='l ok';$('m1').textContent='Found '+r.found.length+' devices. Router: '+r.router;
 $('s2').classList.remove('hide');$('scan').disabled=false;$('scan').textContent='Scan again'};
$('unp').onclick=async()=>{busy($('unp'),$('m2'),'Checking, about 15 seconds...');
 const r=await post('/api/setup/unplugged');$('unp').disabled=false;
 if(r.error)return fail($('unp'),$('m2'),r.error);
 const g=r.gone;$('pick').innerHTML='';
 if(!g.length){$('m2').className='err';$('m2').textContent='No device disappeared. It may not answer ping, or it was still plugged in. Try again, or type its IP:';
  $('pick').innerHTML='<input id=manual placeholder=192.168.1.x> <button class=alt id=usem>Use this IP</button>';
  $('usem').onclick=()=>choose($('manual').value.trim());return}
 $('m2').className='l ok';$('m2').textContent=g.length==1?'This device went offline:':'Several devices went offline, pick the sensor:';
 g.forEach(ip=>{const b=document.createElement('button');b.textContent=ip;b.onclick=()=>choose(ip);$('pick').appendChild(b)})};
const choose=ip=>{sensor=ip;$('ip').textContent=ip;$('s3').classList.remove('hide')};
$('back').onclick=async()=>{busy($('back'),$('m3'),'Checking...');
 for(let i=0;i<8;i++){const r=await post('/api/setup/check',{ip:sensor});
  if(r.online){$('m3').className='l ok';$('m3').textContent=sensor+' is back online.';$('back').disabled=false;
   $('s4').classList.remove('hide');return}await new Promise(r=>setTimeout(r,2000))}
 fail($('back'),$('m3'),'Not back yet. Check it is plugged in and try again.')};
$('save').onclick=async()=>{busy($('save'),$('m4'),'Saving...');
 const r=await post('/api/setup/save',{sensor,cap:$('cap').value,every:$('every').value,confirm:$('confirm').value,restore:$('restore').value,poll:$('poll').value});
 if(r.error)return fail($('save'),$('m4'),r.error);
 $('m4').className='l ok';$('m4').textContent='Saved. Restarting the service...';
 for(let i=0;i<30;i++){await new Promise(r=>setTimeout(r,1000));
  try{const d=await (await fetch('/api/status')).json();if(!d.setup_needed&&d.sensor==sensor){location='/';return}}catch(e){}}
 fail($('save'),$('m4'),'Service did not come back; check: journalctl -u mains-watch')};
</script>"""


def serve(port: int, events_path: str, live: dict, setup=None, pause=None, check=None) -> None:
    """Run the dashboard in a background thread. `live` is updated by the main loop;
    `setup` is an onboard.Setup serving the /setup page."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                body = json.dumps({**live, **summary(read_events(events_path), time.time())})
                ctype = "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE, "text/html; charset=utf-8"
            elif self.path == "/setup" and setup:
                body, ctype = SETUP_PAGE, "text/html; charset=utf-8"
            elif self.path == "/api/check" and check:
                body, ctype = json.dumps(check()), "application/json"
            elif self.path == "/api/setup/config" and setup:
                body, ctype = json.dumps(setup.config()), "application/json"
            else:
                self.send_error(404)
                return
            self._send(body, ctype)

        def do_POST(self):
            # Writes change root-owned config: only accept same-origin fetches from our own
            # page (custom header can't be sent cross-site; Host check blocks DNS rebinding).
            host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")
            routes = {} if not setup else {
                "/api/setup/scan": lambda b: setup.scan(),
                "/api/setup/unplugged": lambda b: setup.unplugged(),
                "/api/setup/check": lambda b: setup.check(str(b.get("ip", ""))),
                "/api/setup/save": setup.save}
            if pause:
                routes["/api/pause"] = lambda b: pause.set(b.get("hours"))
                routes["/api/unpause"] = lambda b: pause.clear()
            if not (host_ok and self.headers.get("X-Setup") == "1" and self.path in routes):
                self.send_error(403 if not host_ok or self.path in routes else 404)
                return
            try:
                length = min(int(self.headers.get("Content-Length", 0)), 4096)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self.send_error(400)
                return
            self._send(json.dumps(routes[self.path](payload)), "application/json")

        def _send(self, body, ctype):
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
