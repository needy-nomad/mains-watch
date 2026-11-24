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
table{width:100%;border-collapse:collapse}td{padding:.3rem .2rem;border-top:1px solid #333}
</style>
<div class=card><div id=state>...</div><div class=l id=sub></div></div>
<div class="card grid">
<div><div class=n id=tot>-</div><div class=l>power cuts, all time</div></div>
<div><div class=n id=today>-</div><div class=l>today</div></div>
<div><div class=n id=down>-</div><div class=l>total time without mains</div></div>
<div><div class=n id=gpu>-</div><div class=l>GPU power now (cap <span id=cap></span> W)</div></div>
<div><div class=n id=frozen>-</div><div class=l>GPU jobs frozen</div></div>
</div>
<div class=card><b>History</b><table id=hist></table></div>
<script>
const dur=s=>s<60?Math.round(s)+' s':s<3600?Math.round(s/60)+' min':(s/3600).toFixed(1)+' h';
const $=i=>document.getElementById(i);
async function tick(){try{
 const d=await (await fetch('/api/status')).json();
 $('state').textContent=d.on_battery?'POWER CUT - GPU halted':'Mains OK';
 $('state').className=d.on_battery?'bad':'ok';
 $('sub').textContent=d.on_battery?'since '+new Date(d.since*1000).toLocaleTimeString()+' ('+dur(Date.now()/1000-d.since)+')':'sensor '+d.sensor+' answering';
 $('tot').textContent=d.outages_total;$('today').textContent=d.outages_today;
 $('down').textContent=dur(d.downtime_total_s);
 $('gpu').textContent=d.gpu_watts==null?'n/a':Math.round(d.gpu_watts)+' W';
 $('cap').textContent=d.cap_watts;$('frozen').textContent=d.frozen;
 $('hist').innerHTML=d.recent.map(e=>'<tr><td>'+new Date(e.t*1000).toLocaleString()+'</td><td>'+
  (e.event=='lost'?'power lost':e.event=='restored'?'power back (cut lasted '+dur(e.duration_s)+')':e.event)+'</td></tr>').join('');
}catch(e){$('state').textContent='service not reachable';$('state').className='bad'}}
tick();setInterval(tick,2000);
</script>"""


def serve(port: int, events_path: str, live: dict) -> None:
    """Run the dashboard in a background thread. `live` is updated by the main loop."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/status":
                body = json.dumps({**live, **summary(read_events(events_path), time.time())})
                ctype = "application/json"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE, "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
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
