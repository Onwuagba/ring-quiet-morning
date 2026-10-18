"""Ring event simulator (demo tool). Enabled only with RING_SIMULATOR=1.

It plays the part of the Ring cloud: pressing a device button sends a webhook in Ring's documented envelope, signed
with `X-Signature: sha256=<HMAC>` exactly as Ring would, to this app's real /webhooks/ring/ endpoint over HTTP. A
simulated clock lets you step through a morning (the rules and escalation run on that clock), and a phone panel shows
the Telegram messages the caregivers would receive, including the tappable All OK / Need help buttons."""
from __future__ import annotations

import io
import json
import urllib.request
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.http import Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from . import ringapi, rules, telegram as tg
from .models import CheckIn, Event, Household, Notification

DEMO_ACCOUNT = "ava1.ring.account.QUIETMORNING-DEMO"
DEVICES = {"kitchen-motion": "Kitchen motion sensor", "doorbell": "Front doorbell"}
STATE = {"clock": None, "phone": [], "log": [], "mid": 100, "hid": None}


def _enabled(view):
    def wrap(request, *a, **kw):
        if not settings.RING_SIMULATOR:
            raise Http404()
        return view(request, *a, **kw)
    wrap.__name__ = view.__name__
    return wrap


def _cmd():
    from watch.management.commands.simulate_demo import Command
    c = Command(stdout=io.StringIO())
    c.tz = timezone.get_current_timezone()
    c.today = timezone.localdate()
    c.h = Household.objects.filter(ring_location_id=DEMO_ACCOUNT).first()
    return c


def _transport(method, payload):
    from watch.management.commands.simulate_demo import DEMO_CHATS
    who = {v: k for k, v in DEMO_CHATS.items()}.get(payload.get("chat_id"), "?")
    if method == "sendMessage":
        STATE["mid"] += 1
        rows = (payload.get("reply_markup") or {}).get("inline_keyboard", [])
        STATE["phone"].append({"id": STATE["mid"], "who": who, "text": payload["text"], "buttons": [b["text"] for r in rows for b in r],
                               "at": STATE["clock"].strftime("%H:%M") if STATE["clock"] else ""})
        return {"message_id": STATE["mid"]}
    if method == "editMessageText":
        for m in STATE["phone"]:
            if m["id"] == payload.get("message_id"):
                m["text"], m["buttons"] = payload["text"], []
    return True


def _at(hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":")[:2])
    return datetime.combine(timezone.localdate(), time(h, m), timezone.get_current_timezone())


def _state():
    c = _cmd()
    h = c.h
    out = {"clock": STATE["clock"].strftime("%H:%M") if STATE["clock"] else None, "phone": STATE["phone"], "log": STATE["log"][-12:],
           "household_id": h.pk if h else None, "devices": DEVICES}
    if h and STATE["clock"]:
        a = rules.assess(h, STATE["clock"])
        out["assessment"] = {"headline": a.headline, "explanation": a.explanation, "confidence": a.confidence,
                             "label": a.confidence_label, "needs_check": a.needs_check, "needs_sensor": a.needs_sensor}
        out["checkin"] = (CheckIn.objects.filter(household=h, date=STATE["clock"].date()).order_by("-id").values_list("state", flat=True).first())
        out["audit"] = [{"at": timezone.localtime(n.created_at).strftime("%H:%M"), "who": n.recipient, "channel": n.channel, "status": n.status}
                        for n in Notification.objects.filter(checkin__household=h).order_by("created_at", "id")]
        out["events"] = [{"at": timezone.localtime(e.at).strftime("%H:%M"), "kind": e.kind, "device": e.device}
                         for e in Event.objects.filter(household=h, at__gte=_at("05:00")).order_by("at")][-8:]
    return out


@csrf_exempt
@require_POST
@_enabled
def reset(request):
    """Seed Mama Ngozi's home with eight weeks of believable mornings and start the clock at 05:00."""
    from watch.management.commands.simulate_demo import Command  # noqa: F401
    User = get_user_model()
    c = _cmd()
    Household.objects.filter(ring_location_id=DEMO_ACCOUNT).delete()
    c.h = c.setup_household(User)
    c.seed()
    STATE.update(clock=_at("05:00"), phone=[], log=[], mid=100, hid=c.h.pk)
    tg.set_transport(_transport)
    return JsonResponse(_state())


@require_GET
@_enabled
def state(request):
    return JsonResponse(_state())


@csrf_exempt
@require_POST
@_enabled
def send_event(request):
    """Send one signed Ring-format webhook to our own /webhooks/ring/ and report what happened."""
    body = json.loads(request.body or "{}")
    etype, device, hhmm = body.get("type", "motion_detected"), body.get("device", "kitchen-motion"), body.get("at") or "05:00"
    if etype not in ringapi.KIND_BY_TYPE or device not in DEVICES:
        return JsonResponse({"error": "unknown event or device"}, status=400)
    payload = ringapi.build_event(DEMO_ACCOUNT, etype, device, at=_at(hhmm), sub_type="human" if etype == "motion_detected" else None)
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = ringapi.sign(settings.RING_WEBHOOK_SECRET, raw)
    url = request.build_absolute_uri("/webhooks/ring/")
    req = urllib.request.Request(url, data=raw, headers={"Content-Type": "application/json", "X-Signature": sig}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    entry = {"at": hhmm, "type": etype, "device": device, "signature": sig[:23] + "...", "status": code,
             "body": json.dumps(payload, indent=1)[:900]}
    STATE["log"].append(entry)
    return JsonResponse({"sent": entry, **_state()})


@csrf_exempt
@require_POST
@_enabled
def set_clock(request):
    body = json.loads(request.body or "{}")
    cur = STATE["clock"] or _at("05:00")
    STATE["clock"] = _at(body["time"]) if body.get("time") else cur + timedelta(minutes=int(body.get("add", 0)))
    return JsonResponse(_state())


@csrf_exempt
@require_POST
@_enabled
def run_check(request):
    """Run the same job Celery beat runs every minute (assess, open check-ins, advance escalation) at the simulated time."""
    from watch.management.commands.simulate_demo import clock
    from watch.tasks import tick
    with clock(STATE["clock"]):
        result = tick()
    return JsonResponse({"tick": result, **_state()})


@csrf_exempt
@require_POST
@_enabled
def press(request):
    """A caregiver taps an inline button in Telegram."""
    from watch.management.commands.simulate_demo import clock
    body = json.loads(request.body or "{}")
    c = _cmd()
    with clock(STATE["clock"] + timedelta(minutes=1)):
        c.press(body.get("who", "tunde"), body.get("button", "ok"))
    STATE["clock"] += timedelta(minutes=1)
    return JsonResponse(_state())


@_enabled
def page(request):
    return HttpResponse(PAGE)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ring event simulator - Quiet Morning</title>
<style>
:root{--bg:#0b1218;--panel:#111e29;--line:#1f3446;--ink:#e8f1f7;--dim:#8ba5b7;--ring:#1da1ff;--ok:#5fdca0;--warn:#f2b84b}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(900px 500px at 10% -10%,#12324a,transparent),var(--bg);color:var(--ink);font:15px/1.4 -apple-system,Segoe UI,Helvetica,Arial,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;border-bottom:1px solid var(--line)}header b{font-size:19px}.pill{background:#0e2f46;border:1px solid #1b4d70;color:#9fd3f5;border-radius:999px;padding:3px 12px;font-size:12px;letter-spacing:.06em;text-transform:uppercase}
.grid{display:grid;grid-template-columns:1.05fr 1fr 330px;gap:18px;padding:18px 22px;align-items:start}
@media(max-width:1100px){.grid{grid-template-columns:1fr}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:16px}h2{font-size:13px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);margin:0 0 12px}
.dev{border:1px solid var(--line);border-radius:12px;padding:12px;margin-bottom:12px;background:#0d1822}.dev h3{margin:0 0 8px;font-size:16px;display:flex;justify-content:space-between}.dev h3 span{font-size:12px;color:var(--dim);font-weight:400}
button{font:inherit;cursor:pointer;border:1px solid #24516f;background:#133149;color:var(--ink);border-radius:10px;padding:8px 12px;margin:0 6px 6px 0}button:hover{background:#18405e}button.pri{background:var(--ring);color:#03202f;border-color:var(--ring);font-weight:600}
input{font:inherit;background:#08121a;color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:7px 9px;width:84px}
.clock{font-size:54px;font-weight:700;letter-spacing:-1px;margin:2px 0 8px}.clock small{font-size:14px;color:var(--dim);font-weight:400;letter-spacing:0}
.assess{border-radius:12px;padding:12px;margin:10px 0;border:1px solid var(--line);background:#0d1822}.assess.good{border-color:#2c7a58;background:#0f2a21}.assess.bad{border-color:#9a6a1b;background:#2b2210}
.assess b{display:block;font-size:17px;margin-bottom:3px}.assess span{color:var(--dim)}
pre{margin:6px 0 0;font:11.5px/1.35 ui-monospace,Menlo,monospace;color:#a8c7dc;white-space:pre-wrap;max-height:150px;overflow:auto;background:#08121a;border-radius:8px;padding:8px}
.log{border-left:3px solid var(--ring);padding:6px 10px;margin:8px 0;background:#0d1822;border-radius:0 8px 8px 0;font-size:13px}.log em{color:var(--ok);font-style:normal}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:4px 6px;border-top:1px solid var(--line)}
.phone{width:300px;margin:0 auto;background:#000;border:6px solid #1a2a37;border-radius:34px;padding:12px 10px 16px;min-height:560px}
.phone .bar{text-align:center;color:#7d93a3;font-size:12px;margin-bottom:8px}.tg{background:#17212b;border-radius:16px;padding:10px;min-height:470px}
.msg{background:#2b5278;border-radius:12px 12px 12px 4px;padding:8px 10px;margin:8px 0;font-size:13.5px;animation:in .3s}.msg small{display:block;color:#9bb7d0;margin-bottom:3px;font-size:11px}
.msg .btns{display:flex;gap:6px;margin-top:8px}.msg .btns button{flex:1;margin:0;background:#34506d;border-color:#456b8d;font-size:13px}@keyframes in{from{opacity:0;transform:translateY(6px)}}
.empty{color:#53697a;text-align:center;margin-top:70px;font-size:13px}
</style></head><body>
<header><b>Ring event simulator</b><span class="pill">signed webhooks</span><span style="color:var(--dim)">plays the Ring cloud: events go to this app's real <code>/webhooks/ring/</code></span>
<span style="flex:1"></span><button class="pri" onclick="reset()">Reset demo home</button><a id="dash" style="color:#9fd3f5" href="#" target="_blank">Caregiver dashboard</a></header>
<div class="grid">
<div class="card"><h2>Ring devices · Mama Ngozi's home</h2>
 <div id="devices"></div>
 <h2 style="margin-top:16px">Webhook log</h2><div id="log"><div class="empty" style="margin-top:12px">No events yet. Press a device button.</div></div></div>
<div class="card"><h2>Quiet Morning</h2><div class="clock" id="clock">--:-- <small>simulated time</small></div>
 <div><button onclick="clk('+10')">+10 min</button><button onclick="goto('07:00')">07:00</button><button onclick="goto('08:04')">08:04</button><button onclick="goto('08:10')">08:10</button><button class="pri" onclick="check()">Run check</button></div>
 <div id="assess" class="assess"><b>Press "Reset demo home" to begin</b><span>Eight weeks of mornings are loaded, so the routine is already learned.</span></div>
 <h2>Today's events</h2><table id="events"></table><h2 style="margin-top:14px">Who was told</h2><table id="audit"></table></div>
<div><div class="phone"><div class="bar" id="phonebar">Telegram</div><div class="tg" id="tg"><div class="empty">Caregivers' messages appear here</div></div></div></div>
</div>
<script>
const $=s=>document.querySelector(s);const post=(u,b={})=>fetch(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());
let S={};
function ev(type,device){const at=($('#at-'+device).value||'07:34');return post('event',{type,device,at}).then(d=>{render(d);return d})}
const TYPES=[['motion_detected','Motion'],['button_press','Doorbell press'],['device_online','Online'],['device_offline','Offline']];
function renderDevices(devs){if($('#devices').dataset.ok)return;$('#devices').dataset.ok=1;$('#devices').innerHTML=Object.entries(devs).map(([id,name])=>`<div class="dev"><h3>${name}<span>${id}</span></h3>
 <div>${TYPES.filter(t=>id==='doorbell'?t[0]!=='motion_detected':t[0]!=='button_press').map(t=>`<button data-t="${t[0]}" data-d="${id}" onclick="ev('${t[0]}','${id}')">${t[1]}</button>`).join('')}</div>
 <div style="color:var(--dim);font-size:13px">event time <input id="at-${id}" value="${id==='doorbell'?'05:00':'07:34'}"> (today)</div></div>`).join('')}
function render(d){S=d;if(d.devices)renderDevices(d.devices);
 $('#clock').innerHTML=(d.clock||'--:--')+' <small>simulated time</small>';
 if(d.household_id)$('#dash').href='/h/'+d.household_id+'/';
 if(d.assessment){const a=d.assessment;$('#assess').className='assess '+((a.needs_check||a.needs_sensor)?'bad':(/Normal|Confirmed/.test(a.headline)?'good':''));
  $('#assess').innerHTML=`<b>${a.headline}</b><span>${a.explanation}</span>`+(a.confidence?`<div style="margin-top:6px;color:var(--warn)">confidence ${a.confidence}% (${a.label})</div>`:'')+(d.checkin?`<div style="margin-top:6px">Check-in: <b>${d.checkin}</b></div>`:'')}
 $('#events').innerHTML=(d.events||[]).map(e=>`<tr><td>${e.at}</td><td>${e.kind}</td><td>${e.device}</td></tr>`).join('')||'<tr><td style="color:var(--dim)">none yet</td></tr>';
 $('#audit').innerHTML=(d.audit||[]).map(a=>`<tr><td>${a.at}</td><td>${a.who}</td><td>${a.channel}</td><td>${a.status}</td></tr>`).join('')||'<tr><td style="color:var(--dim)">nobody yet</td></tr>';
 const lg=d.log||[];$('#log').innerHTML=lg.length?lg.slice().reverse().map(l=>`<div class="log"><b>${l.type}</b> · ${l.device} · ${l.at}<br>X-Signature: ${l.signature} → <em>${l.status}</em><pre>${l.body.replace(/</g,'&lt;')}</pre></div>`).join(''):'<div class="empty" style="margin-top:12px">No events yet. Press a device button.</div>';
 const ph=d.phone||[];$('#tg').innerHTML=ph.length?ph.map(m=>`<div class="msg"><small>to ${m.who} · ${m.at}</small>${m.text.replace(/</g,'&lt;')}${m.buttons.length?`<div class="btns">${m.buttons.map(b=>`<button onclick="tap('${m.who}','${/Need/.test(b)?'help':'ok'}')">${b}</button>`).join('')}</div>`:''}</div>`).join(''):'<div class="empty">Caregivers\' messages appear here</div>'}
function reset(){post('reset').then(render)}
function clk(add){post('clock',{add:parseInt(add)}).then(render)}
function goto(t){post('clock',{time:t}).then(render)}
function check(){post('check').then(render)}
function tap(who,button){post('press',{who,button}).then(render)}
fetch('state').then(r=>r.json()).then(render);
</script></body></html>"""
