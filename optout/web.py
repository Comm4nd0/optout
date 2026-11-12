"""LAN dashboard: progress, the 'needs you' queue, every broker, per-request history and actions."""
import secrets
from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import DictLoader, Environment, select_autoescape
from markupsafe import Markup, escape

from . import config, db, inbox, render, scheduler
from .classify import URL_RE

cfg = config.load()
security = HTTPBasic(auto_error=False)
app = FastAPI(title="optout", docs_url=None, redoc_url=None, openapi_url=None)


def auth(creds: HTTPBasicCredentials | None = Depends(security)):
    if not cfg.web_password:
        return
    if not creds or not (secrets.compare_digest(creds.username, cfg.web_user)
                         and secrets.compare_digest(creds.password, cfg.web_password)):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


def ctx():
    con = db.connect(cfg.db)
    people, sender = config.load_people(cfg)
    return con, people, sender


LABELS = {
    "queued": "Queued", "sent": "Sent", "acknowledged": "Acknowledged", "chased": "Reminder sent",
    "needs_verification": "Click link", "needs_id": "ID requested", "needs_review": "Review reply",
    "overdue": "Overdue", "manual_todo": "Do by hand", "form_queued": "Form queued",
    "refused": "Refused", "confirmed": "Removed", "no_data": "No data held", "skipped": "Skipped",
}
TONE = {"confirmed": "good", "no_data": "good", "skipped": "muted", "queued": "muted",
        "form_queued": "muted", "sent": "info", "acknowledged": "info", "chased": "warn",
        "overdue": "bad", "refused": "bad"}
for s in db.NEEDS_YOU:
    TONE.setdefault(s, "warn")

# the states rolled up into what the progress bar shows: key -> (name, tone, states)
GROUPS = {
    "done": ("Done", "good", ("confirmed", "no_data")),
    "waiting": ("Waiting on broker", "info", ("sent", "acknowledged", "chased")),
    "needs": ("Needs you", "warn", db.NEEDS_YOU),
    "queued": ("Queued", "muted", ("queued", "form_queued")),
    "skipped": ("Skipped", "skip", ("skipped",)),
}
GROUP_OF = {state: key for key, (_, _, states) in GROUPS.items() for state in states}
# what each 'needs you' state is asking for, quickest first: state -> (heading, what to do)
TODO = {
    "needs_verification": ("Confirmation links to click",
                           "The broker sent a link that confirms the request, and it could not be opened "
                           "automatically. Open it yourself."),
    "needs_id": ("ID requested",
                 "The broker asked for proof of identity. Nothing is ever sent for you: reply to their email "
                 "yourself if you are happy to, or skip the broker."),
    "needs_review": ("Replies to read",
                     "The reply could not be read automatically. Read it and record what it means."),
    "refused": ("Refused", "The broker declined. UK and EU brokers come with a complaint draft for the ICO."),
    "overdue": ("Overdue",
                "The deadline passed with no answer. UK and EU brokers come with a complaint draft for the ICO."),
    "manual_todo": ("Forms to fill in by hand",
                    "No recipe can submit these. Open the form, paste in the details, then press "
                    "“I submitted it”."),
}
GROUP_ROWS = 60  # rows shown per 'needs you' group before linking to the full list
EVENTS = {"sent": ("Request sent", "info"), "chase_sent": ("Reminder sent", "warn"),
          "reply": ("Reply received", "info"), "verify_link": ("Confirmation link tried", "muted")}
JOBS = {"inbox": "Replies checked", "cycle": "Last send run", "forms": "Last form run"}
METHODS = {"email": "Email", "form": "Web form", "manual": "By hand"}


def _dt(ts):
    return datetime.fromisoformat(ts).astimezone() if ts else None


def _day(ts) -> str:
    d = _dt(ts)
    return f"{d.day} {d:%b %Y}" if d else "–"


def _when(ts) -> str:
    d = _dt(ts)
    return f"{d.day} {d:%b %Y}, {d:%H:%M}" if d else "–"


def _ago(ts) -> str:
    """'3 days ago', or 'in 2 weeks' for a time still to come."""
    if not ts:
        return ""
    secs = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
    day = 86400
    for at_least, size, unit in ((365 * day, 365 * day, "year"), (60 * day, 30 * day, "month"),
                                 (14 * day, 7 * day, "week"), (day, day, "day"), (3600, 3600, "hour"),
                                 (60, 60, "minute")):
        if abs(secs) >= at_least:
            n = int(abs(secs) // size)
            text = f"{n} {unit}{'' if n == 1 else 's'}"
            return f"in {text}" if secs < 0 else f"{text} ago"
    return "just now"


def _linkify(text) -> Markup:
    """Escaped text with its http(s) URLs made clickable (confirmation links, mostly)."""
    out, last = [], 0
    for m in URL_RE.finditer(text or ""):
        url = m.group(0).rstrip(".,;")
        out += [escape(text[last:m.start()]),
                Markup('<a href="{0}" target="_blank" rel="noopener noreferrer">{0}</a>').format(url)]
        last = m.start() + len(url)
    return Markup("").join(out + [escape((text or "")[last:])])


def _tally(rows) -> dict:
    """Requests per group, how many count ('n': all but skipped) and the share of those done."""
    t = dict.fromkeys(GROUPS, 0)
    for r in rows:
        t[GROUP_OF.get(r["state"], "queued")] += 1
    n = sum(t.values()) - t["skipped"]
    return {**t, "n": n, "pct": int(100 * t["done"] / n) if n else 0}


def _jobs(con) -> list[dict]:
    beats = {h["name"]: h for h in con.execute("SELECT * FROM heartbeat")}
    return [{"label": label, "ago": _ago(beats[k]["ts"]) if k in beats else "never",
             "ok": k not in beats or bool(beats[k]["ok"]), "detail": beats[k]["detail"] if k in beats else ""}
            for k, label in JOBS.items() if k in beats or k != "forms"]


CSS = """
:root{color-scheme:light dark;--bg:#f3f4f7;--card:#fff;--fg:#161b26;--mute:#5f6b7c;--line:#e2e5eb;--line2:#eef0f4;
--hover:#f7f8fa;--acc:#3451d1;--acc-fg:#fff;--shadow:0 1px 2px rgba(16,24,40,.05),0 1px 3px rgba(16,24,40,.05);
--good:#17703f;--good-bg:#e2f4e9;--good-s:#2f9e63;--info:#2a4fc4;--info-bg:#e5ebfd;--info-s:#5b7df0;
--warn:#8a5a00;--warn-bg:#fcefd0;--warn-s:#e2a31d;--bad:#b02a1e;--bad-bg:#fde6e3;--bad-s:#e0564a;
--muted-bg:#edeff3;--muted-s:#c3c9d3;--skip-s:#9aa3b0}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--card:#161a21;--fg:#e7e9ee;--mute:#97a1b0;--line:#272d37;
--line2:#1f242c;--hover:#1b2029;--acc:#8ea6ff;--acc-fg:#0e1116;--shadow:none;
--good:#6fd6a0;--good-bg:#12301f;--good-s:#3fae74;--info:#9ab3ff;--info-bg:#1a2644;--info-s:#5f7fe8;
--warn:#f2c466;--warn-bg:#382b10;--warn-s:#d99a1c;--bad:#ff9288;--bad-bg:#3b1a17;--bad-s:#d9584c;
--muted-bg:#222832;--muted-s:#3b4350;--skip-s:#566070}}
.good{--t:var(--good);--tb:var(--good-bg);--ts:var(--good-s)}.info{--t:var(--info);--tb:var(--info-bg);--ts:var(--info-s)}
.warn{--t:var(--warn);--tb:var(--warn-bg);--ts:var(--warn-s)}.bad{--t:var(--bad);--tb:var(--bad-bg);--ts:var(--bad-s)}
.muted{--t:var(--mute);--tb:var(--muted-bg);--ts:var(--muted-s)}.skip{--t:var(--mute);--tb:var(--muted-bg);--ts:var(--skip-s)}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px;border-radius:4px}
h1{font-size:24px;line-height:1.25;margin:0;letter-spacing:-.01em}h2{font-size:16px;margin:0 0 10px}
code{font:13px/1.4 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--muted-bg);border-radius:5px;padding:1px 5px}
.in{max-width:1120px;margin:0 auto;padding-left:20px;padding-right:20px}
.top{background:var(--card);border-bottom:1px solid var(--line)}
.top .in{display:flex;align-items:center;gap:6px 18px;min-height:56px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;font-weight:650;font-size:17px;color:var(--fg)}
.brand:hover{text-decoration:none}.brand svg{width:26px;height:26px}
nav{display:flex;gap:2px;margin-right:auto}
nav a{padding:6px 11px;border-radius:8px;color:var(--mute);font-weight:550}
nav a:hover{text-decoration:none;color:var(--fg)}nav a.on{color:var(--fg);background:var(--muted-bg)}
main{padding-top:24px;padding-bottom:64px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);padding:18px 20px}
.small{font-size:13px;color:var(--mute)}.sub{color:var(--mute);margin:4px 0 18px}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 10px 2px 8px;border-radius:99px;font-size:12.5px;
font-weight:550;white-space:nowrap;color:var(--t,var(--mute));background:var(--tb,var(--muted-bg))}
.pill::before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor;opacity:.8}
a.pill:hover{text-decoration:none;filter:brightness(.96)}
.banner{border-radius:10px;padding:10px 14px;margin:0 0 16px;color:var(--t);background:var(--tb)}
.hero{padding:22px 24px}
.hero-top{display:flex;flex-wrap:wrap;gap:16px 36px;align-items:flex-end;justify-content:space-between}
.pct{font-size:42px;font-weight:700;letter-spacing:-.02em;line-height:1}
.pct small{font-size:15px;font-weight:450;color:var(--mute);letter-spacing:0;margin-left:10px}
.facts{display:flex;flex-wrap:wrap;gap:10px 30px;margin:0}
.facts dt{font-size:12.5px;color:var(--mute)}.facts dd{margin:0;font-weight:550;font-variant-numeric:tabular-nums}
.facts dd.fail{color:var(--bad)}
.meter{display:block;height:4px;width:100%;min-width:120px;border-radius:9px;background:var(--muted-bg);margin-top:5px;overflow:hidden}
.meter i{display:block;height:100%;background:var(--info-s);border-radius:9px}
.bar{display:flex;gap:2px;height:12px;border-radius:99px;overflow:hidden;background:var(--muted-bg);margin:20px 0 12px}
.seg{flex:1 0 0;min-width:4px;background:var(--ts)}
.legend{display:flex;flex-wrap:wrap;gap:2px 6px;list-style:none;margin:0 -8px;padding:0}
.legend a{display:flex;align-items:center;gap:8px;padding:5px 10px 5px 8px;border-radius:8px;color:var(--fg)}
.legend a:hover{background:var(--muted-bg);text-decoration:none}
.legend b{font-variant-numeric:tabular-nums}.legend span{color:var(--mute)}
.dot{width:10px;height:10px;border-radius:3px;background:var(--ts);flex:none}
.people{display:grid;grid-template-columns:max-content minmax(60px,1fr) max-content;gap:10px 16px;align-items:center;
margin-top:16px;padding-top:16px;border-top:1px solid var(--line)}
.person{display:contents}.people .bar{height:8px;margin:0}.people .n{font-size:13px;color:var(--mute);font-variant-numeric:tabular-nums;text-align:right}
.tabs{display:flex;gap:4px;margin:26px 0 16px;border-bottom:1px solid var(--line)}
.tabs a{display:flex;gap:8px;align-items:center;padding:9px 14px;color:var(--mute);font-weight:550;
border-bottom:2px solid transparent;margin-bottom:-1px}
.tabs a:hover{color:var(--fg);text-decoration:none}.tabs a.on{color:var(--fg);border-color:var(--acc)}
.count{font-size:12px;padding:1px 8px;border-radius:99px;color:var(--t,var(--mute));background:var(--tb,var(--muted-bg));
font-variant-numeric:tabular-nums}
.group{padding:0;margin-bottom:14px;overflow:hidden}
.group summary{list-style:none;cursor:pointer;display:flex;gap:14px;align-items:center;padding:14px 18px}
.group summary::-webkit-details-marker{display:none}.group summary:hover{background:var(--hover)}
.badge{flex:none;min-width:38px;height:38px;padding:0 8px;border-radius:10px;display:grid;place-items:center;
font-weight:700;font-variant-numeric:tabular-nums;color:var(--t);background:var(--tb)}
.gt{flex:1;min-width:0}.gt b{display:block;font-size:16px}.gt .hint{display:block;font-size:13.5px;color:var(--mute)}
.chev{flex:none;width:20px;height:20px;color:var(--mute);transition:transform .15s}details[open]>summary .chev{transform:rotate(180deg)}
.rows{list-style:none;margin:0;padding:0;border-top:1px solid var(--line);display:grid;
grid-template-columns:repeat(auto-fill,minmax(min(420px,100%),1fr));margin-bottom:-1px}
.rows a{display:flex;gap:12px;align-items:baseline;padding:9px 18px;color:var(--fg);border-bottom:1px solid var(--line2)}
.rows a:hover{background:var(--hover);text-decoration:none}
.rows .name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500}
.rows .who,.rows time{color:var(--mute);font-size:13px;white-space:nowrap}.rows time{min-width:96px;text-align:right}
.more{margin:0;padding:10px 18px;font-weight:550}
.empty{text-align:center;padding:40px 20px;color:var(--mute)}.empty b{display:block;color:var(--fg);font-size:16px;margin-bottom:2px}
.foot{margin:18px 2px 0}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 12px}
.filters input{flex:1;min-width:200px}
input,select,button,textarea,.btn{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:7px 12px}
input::placeholder{color:var(--mute)}
button,.btn{cursor:pointer;font-weight:550;display:inline-block;text-align:center}
button:hover,.btn:hover{border-color:var(--mute);text-decoration:none}
.btn-primary,.btn-primary:hover{background:var(--acc);border-color:var(--acc);color:var(--acc-fg)}
.btn-primary:hover{filter:brightness(1.08)}.btn-quiet{color:var(--mute)}
.tablewrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow)}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:9px 16px;border-bottom:1px solid var(--line2);vertical-align:middle}
th{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--mute);font-weight:600;
border-bottom-color:var(--line);white-space:nowrap}
tbody tr:hover{background:var(--hover)}tbody tr:last-child td{border-bottom:0}
.bname{display:block;font-weight:500}.only-s{display:none}.opt{white-space:nowrap}
.tag{font-size:11px;font-weight:600;letter-spacing:.04em;border:1px solid var(--line);border-radius:5px;padding:1px 6px;color:var(--mute)}
.crumb{margin:0 0 14px}.dhead{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center}
.stack>*{margin-bottom:16px}
.call{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:center;justify-content:space-between;border-left:4px solid var(--ts)}
.call p{margin:2px 0 0;color:var(--mute);max-width:70ch}
.cardhead{display:flex;gap:12px;align-items:center;justify-content:space-between;margin-bottom:10px}.cardhead h2{margin:0}
.copy{list-style:none;margin:12px 0;padding:0;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.copy li{display:flex;gap:12px;align-items:center;padding:8px 8px 8px 14px;border-bottom:1px solid var(--line2)}
.copy li:last-child{border-bottom:0}.copy .k{flex:none;width:150px;color:var(--mute);font-size:13px}
.copy .v{flex:1;min-width:0;word-break:break-word;user-select:all}
.copy button,.cardhead button{flex:none;padding:3px 10px;font-size:13px}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:10px;
padding:12px 14px;margin:8px 0 0;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.cols{display:grid;grid-template-columns:minmax(0,1fr) 330px;grid-template-rows:auto 1fr;gap:16px 20px;align-items:start;
grid-template-areas:"main act" "main info"}.c-main{grid-area:main}.c-act{grid-area:act}.c-info{grid-area:info}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}.actions input,.actions .btn-primary{grid-column:1/-1}
dl.kv{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:7px 16px;margin:0}
.kv dt{color:var(--mute)}.kv dd{margin:0;word-break:break-word}
.tl{list-style:none;margin:6px 0 0;padding:0}
.tl li{position:relative;padding:0 0 18px 28px}.tl li:last-child{padding-bottom:0}
.tl li::before{content:"";position:absolute;left:4px;top:6px;width:10px;height:10px;border-radius:50%;
background:var(--ts,var(--muted-s));box-shadow:0 0 0 4px var(--card);z-index:1}
.tl li::after{content:"";position:absolute;left:8.5px;top:10px;bottom:-8px;width:1px;background:var(--line)}
.tl li:last-child::after{display:none}
.tl .t{display:flex;flex-wrap:wrap;gap:2px 10px;align-items:baseline}.tl time{color:var(--mute);font-size:13px}
.tl .note{color:var(--mute);font-size:14px;white-space:pre-wrap;word-break:break-word}
.tl .hd{font-size:13px;color:var(--mute);word-break:break-word}.tl .hd b{font-weight:550}
.tl details{margin-top:4px}.tl details summary{cursor:pointer;color:var(--acc);font-size:13px;font-weight:550;width:max-content}
.steps{margin:10px 0 0;padding-left:22px}.steps li{margin:6px 0}
@media (max-width:860px){.cols{grid-template-columns:minmax(0,1fr);grid-template-rows:none;grid-template-areas:"act" "main" "info"}}
@media (max-width:640px){.in{padding-left:14px;padding-right:14px}.hero{padding:18px 16px}.pct{font-size:36px}
.pct small{display:block;margin:8px 0 0}.opt{display:none}.only-s{display:inline}thead{display:none}
tbody tr{display:flex;flex-wrap:wrap;gap:6px 14px;padding:11px 14px;border-bottom:1px solid var(--line2)}
tbody tr:last-child{border-bottom:0}tbody td{display:block;padding:0;border:0}tbody td:first-child{flex:0 0 100%}
tbody td[data-p]::before{content:attr(data-p);color:var(--mute);font-size:13px;margin-right:6px}
.rows a{padding:9px 14px}.rows time{min-width:0}.group summary{padding:12px 14px}.copy li{flex-wrap:wrap;gap:2px 12px}
.copy .k{width:100%}.people{display:block}.person{display:grid;grid-template-columns:minmax(0,1fr) max-content;gap:6px 12px}
.person+.person{margin-top:14px}.person .bar{grid-column:1/-1;grid-row:2}}
"""

LOGO = ('<svg viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="#3451d1"/>'
        '<path d="M16 6.5l8 3v6.2c0 4.6-3.2 8.4-8 9.8-4.8-1.4-8-5.2-8-9.8V9.5z" fill="#fff"/>'
        '<path d="M12.3 16.2l2.7 2.7 4.9-5.2" fill="none" stroke="#3451d1" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>')

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{% block title %}Optout{% endblock %}</title>
<link rel="icon" href="data:image/svg+xml,{{ logo|urlencode }}"><style>{{ css|safe }}</style></head><body>
<header class="top"><div class="in"><a class="brand" href="/">{{ logo|safe }}Optout</a>
<nav><a href="/" {{ 'class=on' if page != 'mail' }}>Requests</a><a href="/mail" {{ 'class=on' if page == 'mail' }}>Mail</a></nav>
{% if dry and page != 'home' %}<span class="pill warn" title="Nothing is being sent. Set OPTOUT_DRY_RUN=false to go live.">Dry run</span>{% endif %}
</div></header><main class="in">{% block body %}{% endblock %}</main>
<script>
document.addEventListener("click", function (e) {
  var b = e.target.closest("[data-copy]"); if (!b) return;
  var el = document.getElementById(b.dataset.copy), was = b.textContent;
  function done() { b.textContent = "Copied"; setTimeout(function () { b.textContent = was; }, 1200); }
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(el.innerText).then(done); return; }
  var r = document.createRange(), s = getSelection();  // plain http on a LAN has no clipboard API
  r.selectNodeContents(el); s.removeAllRanges(); s.addRange(r);
  try { document.execCommand("copy"); done(); } catch (err) {}
  s.removeAllRanges();
});
</script></body></html>"""

INDEX = """{% extends "base" %}
{% macro bar(t) %}<div class="bar" role="img" aria-label="{% for k, g in groups.items() if t[k] %}{{ t[k] }} {{ g[0]|lower }}{{ ", " if not loop.last }}{% endfor %}">
{% for k, g in groups.items() if t[k] %}<span class="seg {{ g[1] }}" style="flex-grow:{{ t[k] }}" title="{{ g[0] }}: {{ t[k]|num }}"></span>{% endfor %}</div>{% endmacro %}
{% block body %}
{% if dry %}<div class="banner warn"><b>Dry-run mode.</b> Nothing is being sent. Set <code>OPTOUT_DRY_RUN=false</code> to go live.</div>{% endif %}
<section class="card hero">
<div class="hero-top"><div class="pct">{{ total.pct }}%<small>{{ total.done|num }} of {{ total.n|num }} requests done</small></div>
<dl class="facts"><div><dt>Emails in the last 24 hours</dt><dd>{{ sent24 }} of {{ limit }}<span class="meter"><i style="width:{{ [100, (100 * sent24 / limit)|int if limit else 0]|min }}%"></i></span></dd></div>
{% for jb in jobs %}<div><dt>{{ jb.label }}</dt><dd {% if not jb.ok %}class="fail" title="{{ jb.detail }}"{% endif %}>{{ jb.ago }}{{ " (failed)" if not jb.ok }}</dd></div>{% endfor %}</dl></div>
{{ bar(total) }}
<ul class="legend">{% for k, g in groups.items() if total[k] or k != "skipped" %}
<li><a class="{{ g[1] }}" href="{{ '/?v=todo' if k == 'needs' else '/?v=all&s=' ~ k }}"><i class="dot"></i><b>{{ total[k]|num }}</b><span>{{ g[0] }}</span></a></li>{% endfor %}</ul>
{% if per_person|length > 1 %}<div class="people">{% for name, t in per_person %}
<div class="person"><span>{{ name }}</span>{{ bar(t) }}<span class="n">{{ t.done|num }} of {{ t.n|num }} done</span></div>{% endfor %}</div>{% endif %}
</section>
<div class="tabs"><a href="/?v=todo" {{ 'class=on' if view == 'todo' }}>Needs you <span class="count {{ 'warn' if total.needs }}">{{ total.needs|num }}</span></a>
<a href="/?v=all" {{ 'class=on' if view == 'all' }}>All brokers <span class="count">{{ n_all|num }}</span></a></div>
{% if view == "todo" %}
{% for g in todo %}<details class="card group {{ tone[g.state] }}" open>
<summary><span class="badge">{{ g.n }}</span><span class="gt"><b>{{ g.title }}</b><span class="hint">{{ g.hint }}</span></span>
<svg class="chev" viewBox="0 0 20 20" aria-hidden="true"><path d="M5 7.5l5 5 5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></summary>
<ul class="rows">{% for r in g.rows %}<li><a href="/r/{{ r.id }}"><span class="name">{{ r.name }}</span><span class="who">{{ r.person|title }}</span><time title="{{ r.updated_at|day }}">{{ r.updated_at|ago }}</time></a></li>{% endfor %}</ul>
{% if g.n > g.rows|length %}<p class="more"><a href="/?v=all&s={{ g.state }}">See all {{ g.n }} →</a></p>{% endif %}
</details>{% else %}<div class="card empty"><b>Nothing needs you right now.</b>Requests that need a decision or a form filled in will be listed here.</div>{% endfor %}
<p class="small foot">Replies that could not be tied to a request are not listed here. In Gmail they are under the label
<a href="https://mail.google.com/mail/#label/{{ needs_label|urlencode }}" target="_blank" rel="noopener noreferrer">{{ needs_label }}</a> when they ask for something.</p>
{% else %}
<form class="filters" method="get"><input type="hidden" name="v" value="all">
<input type="search" name="q" value="{{ q }}" placeholder="Search by name or domain" aria-label="Search brokers">
<select name="j" aria-label="Region"><option value="">All regions</option>{% for j in ["uk","eu","us"] %}<option value="{{ j }}" {{ "selected" if j==jur }}>{{ j|upper }}</option>{% endfor %}</select>
<select name="s" aria-label="Status"><option value="">Any status</option>
<optgroup label="Group">{% for k, g in groups.items() %}<option value="{{ k }}" {{ "selected" if k==st }}>{{ g[0] }}</option>{% endfor %}</optgroup>
<optgroup label="Exact status">{% for k,v in labels.items() %}<option value="{{ k }}" {{ "selected" if k==st }}>{{ v }}</option>{% endfor %}</optgroup></select>
<button class="btn-primary">Filter</button>{% if q or jur or st %}<a class="btn" href="/?v=all">Clear</a>{% endif %}</form>
{% if grid %}<div class="tablewrap"><table><thead><tr><th>Broker</th><th class="opt">Region</th><th class="opt">Method</th>{% for p in people %}<th>{{ p|title }}</th>{% endfor %}</tr></thead><tbody>
{% for b in grid %}<tr><td><span class="bname">{{ b.name }}</span><span class="small">{{ b.domain }}<span class="only-s">{{ " · " if b.domain }}{{ b.jurisdiction|upper }} · {{ methods.get(b.method, b.method) }}</span></span></td>
<td class="opt"><span class="tag">{{ b.jurisdiction|upper }}</span></td><td class="opt small">{{ methods.get(b.method, b.method) }}</td>
{% for p in people %}{% set r = b.reqs.get(p) %}<td data-p="{{ p|title }}">{% if r %}<a class="pill {{ tone[r.state] }}" href="/r/{{ r.id }}">{{ labels[r.state] }}</a>{% else %}<span class="small">–</span>{% endif %}</td>{% endfor %}</tr>{% endfor %}
</tbody></table></div>
<p class="small foot">{% if matched > grid|length %}Showing the first {{ grid|length }} of {{ matched|num }} brokers; filter to narrow it down.{% else %}{{ matched|num }} broker{{ "" if matched == 1 else "s" }}.{% endif %}</p>
{% else %}<div class="card empty"><b>No brokers match.</b><a href="/?v=all">Clear the filters</a></div>{% endif %}
{% endif %}
{% endblock %}"""

DETAIL = """{% extends "base" %}{% block title %}{{ b.name }} · Optout{% endblock %}{% block body %}
{% macro contact(c) %}{% if c.startswith("http") %}<a href="{{ c }}" target="_blank" rel="noopener noreferrer">{{ c }}</a>{% elif "@" in c %}<a href="mailto:{{ c }}">{{ c }}</a>{% else %}{{ c or "–" }}{% endif %}{% endmacro %}
<p class="crumb"><a href="/">← Requests</a></p>
<div class="dhead"><h1>{{ b.name }}</h1><span class="pill {{ tone[r.state] }}">{{ labels[r.state] }}</span></div>
<p class="sub">{{ p.full_name }} · round {{ r.round }} · reference optout:{{ r.id }}</p>
<div class="stack">
{% if guide and r.state != "manual_todo" %}<section class="card call {{ tone[r.state] }}"><div><b>What to do</b><p>{{ guide[1] }}</p></div>
{% if link %}<a class="btn btn-primary" href="{{ link }}" target="_blank" rel="noopener noreferrer">Open the confirmation link</a>{% endif %}</section>{% endif %}
{% if r.state in ("manual_todo","form_queued") %}<section class="card"><div class="cardhead"><h2>Do it by hand</h2>
{% if b.contact.startswith("http") %}<a class="btn" href="{{ b.contact }}" target="_blank" rel="noopener noreferrer">Open the form</a>{% endif %}</div>
{% if r.state == "form_queued" %}<p class="small">A recipe will submit this form on the next daily run. To do it yourself instead:</p>{% endif %}
Open the form, search for {{ p.full_name }} and submit the removal. Details to paste:
<ul class="copy">{% for label, value in details %}<li><span class="k">{{ label }}</span><span class="v" id="d{{ loop.index }}">{{ value }}</span><button type="button" data-copy="d{{ loop.index }}">Copy</button></li>{% endfor %}</ul>
<span class="small">Then press “I submitted it”. The request goes to Acknowledged with a {{ reply_days }}-day deadline.</span></section>{% endif %}
{% if ico %}<section class="card"><div class="cardhead"><h2>ICO complaint draft</h2><button type="button" data-copy="ico">Copy</button></div><pre id="ico">{{ ico }}</pre></section>{% endif %}
</div>
<div class="cols">
<section class="card c-main"><h2>History</h2><ol class="tl">{% for e in events %}<li class="{{ e.tone }}">
<div class="t">{% if e.state %}<span class="pill {{ e.tone }}">{{ e.title }}</span>{% else %}<b>{{ e.title }}</b>{% endif %}<time>{{ e.when }} · {{ e.ago }}</time></div>
{% if e.note %}<div class="note">{{ e.note|linkify }}</div>{% endif %}
{% for k, v in e.head %}<div class="hd"><b>{{ k }}:</b> {{ v }}</div>{% endfor %}
{% if e.body %}<details {{ "open" if e.open }}><summary>{{ "Their reply" if e.open else "The email" }}</summary><pre>{{ e.body|linkify }}</pre></details>{% endif %}
</li>{% else %}<li class="small">No events yet.</li>{% endfor %}</ol></section>
<section class="card c-act"><h2>Record what happened</h2><form method="post" action="/r/{{ r.id }}" class="actions">
<input name="note" placeholder="Note (optional)" aria-label="Note">
{% for val, text in actions %}<button name="action" value="{{ val }}" class="{{ 'btn-primary' if loop.first else 'btn-quiet' if val == 'skipped' }}">{{ text }}</button>{% endfor %}</form></section>
<section class="card c-info"><h2>Broker</h2><dl class="kv">
<dt>Domain</dt><dd>{{ b.domain or "–" }}</dd><dt>Region</dt><dd>{{ b.jurisdiction|upper }}</dd><dt>Method</dt><dd>{{ methods.get(b.method, b.method) }}</dd>
<dt>Contact</dt><dd>{{ contact(b.contact) }}</dd>
{% if b.alt_contact %}<dt>Also</dt><dd>{{ contact(b.alt_contact) }}</dd>{% endif %}
{% if b.needs_id %}<dt>ID</dt><dd>Usually asks for ID</dd>{% endif %}
{% if b.notes %}<dt>Notes</dt><dd>{{ b.notes }}</dd>{% endif %}
<dt>Sent</dt><dd>{% if r.sent_at %}{{ r.sent_at|day }}<div class="small">{{ r.sent_at|ago }}</div>{% else %}–{% endif %}</dd>
<dt>Reply due</dt><dd>{% if r.due_at %}{{ r.due_at|day }}<div class="small">{{ r.due_at|ago }}</div>{% else %}–{% endif %}</dd>
{% if r.recheck_at %}<dt>Ask again</dt><dd>{{ r.recheck_at|day }}<div class="small">{{ r.recheck_at|ago }}</div></dd>{% endif %}
</dl></section></div>
{% endblock %}"""

MAIL = """{% extends "base" %}{% block title %}Mail · Optout{% endblock %}{% block body %}
<h1>Mail</h1><p class="sub">Where the answers go, and how to keep them out of your inbox.</p>
<div class="stack">
<section class="card"><h2>What happens to replies</h2>
{% if user %}<dl class="kv"><dt>Requests are sent from</dt><dd>{{ user }}</dd>
<dt>Brokers are asked to reply to</dt><dd>{{ reply }}{% if reply != user %}<div class="small">The same mailbox. The <code>+{{ tag }}</code> part is what lets Gmail tell these answers apart from your other mail.</div>{% endif %}</dd>
{% for jb in jobs %}<dt>{{ jb.label }}</dt><dd {% if not jb.ok %}class="fail" title="{{ jb.detail }}"{% endif %}>{{ jb.ago }}{{ " (failed)" if not jb.ok }}</dd>{% endfor %}</dl>
{% else %}<p class="small">No Gmail account is set up yet. On a server, run <code>optout-secrets</code>.</p>{% endif %}
<p>Every 15 minutes the inbox job reads new mail, works out which request each answer belongs to, files it under the Gmail label
<a href="https://mail.google.com/mail/#label/{{ label|urlencode }}" target="_blank" rel="noopener noreferrer">{{ label }}</a> and takes it out of the inbox.
Anything that needs a decision appears under <a href="/?v=todo">Needs you</a>.</p></section>
<section class="card"><div class="cardhead"><h2>Stop replies reaching the inbox at all</h2>
<a class="btn btn-primary" href="/mail/optout-gmail-filters.xml" download>Download the Gmail filters</a></div>
<p>Until the inbox job runs, an answer sits in the inbox like any other email and your phone announces it. Two Gmail filters prevent that:
mail sent to {% if reply != user %}<code>{{ reply }}</code>, or mail {% endif %}with <code>optout</code> in the subject, skips the inbox, goes straight under the label <code>{{ label }}</code> and is never treated as spam.
The inbox job still reads it there.</p>
<ol class="steps"><li>Download the filter file with the button above.</li>
<li>In Gmail on a computer, open <b>Settings</b> → <b>See all settings</b> → <b>Filters and blocked addresses</b>.</li>
<li>Choose <b>Import filters</b>, pick the file, press <b>Open file</b>, then <b>Create filters</b>.</li>
<li>To clear out answers already in the inbox, tick <b>Apply new filters to existing email</b> before creating them, or run <code>optout refile</code>.</li></ol>
<p class="small">Bounce messages and the odd answer on a new subject to your plain address will still land in the inbox first; the inbox job clears them within 15 minutes.</p></section>
</div>
{% endblock %}"""

jenv = Environment(loader=DictLoader({"base": BASE, "index": INDEX, "detail": DETAIL, "mail": MAIL}),
                   autoescape=select_autoescape(default=True))
jenv.filters.update(day=_day, ago=_ago, linkify=_linkify, num=lambda n: f"{n:,}")


def _page(name, **ctx) -> str:
    return jenv.get_template(name).render(css=CSS, logo=LOGO, dry=cfg.dry_run, labels=LABELS, tone=TONE,
                                          groups=GROUPS, methods=METHODS, **ctx)


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def index(v: str = "", q: str = "", j: str = "", s: str = ""):
    con, people, _ = ctx()
    latest = con.execute(
        """SELECT r.*, b.name FROM requests r JOIN brokers b ON b.slug=r.broker
           WHERE r.id IN (SELECT max(id) FROM requests GROUP BY person, broker)""").fetchall()
    total = _tally(latest)
    view = v if v in ("todo", "all") else "all" if q or j or s or not total["needs"] else "todo"
    todo, grid, matched = [], [], 0
    if view == "todo":
        for state, (title, hint) in TODO.items():
            rows = sorted((r for r in latest if r["state"] == state), key=lambda r: r["updated_at"])
            if rows:
                todo.append({"state": state, "title": title, "hint": hint, "n": len(rows), "rows": rows[:GROUP_ROWS]})
    else:
        sql, args = "SELECT * FROM brokers WHERE enabled=1", []
        if q:
            sql, args = sql + " AND (name LIKE ? OR domain LIKE ?)", args + [f"%{q}%", f"%{q}%"]
        if j:
            sql, args = sql + " AND jurisdiction=?", args + [j]
        by_broker = {}
        for r in latest:
            by_broker.setdefault(r["broker"], {})[r["person"]] = r
        wanted = GROUPS[s][2] if s in GROUPS else (s,) if s else ()
        for b in con.execute(sql + " ORDER BY priority, name", args).fetchall():
            reqs = by_broker.get(b["slug"], {})
            if wanted and not any(r["state"] in wanted for r in reqs.values()):
                continue
            grid.append({**dict(b), "reqs": reqs})
        matched, grid = len(grid), grid[:400]
    return _page(
        "index", page="home", view=view, total=total, todo=todo, grid=grid, matched=matched, people=list(people),
        per_person=[(p["full_name"], _tally(r for r in latest if r["person"] == key)) for key, p in people.items()],
        n_all=con.execute("SELECT count(*) FROM brokers WHERE enabled=1").fetchone()[0],
        sent24=scheduler.sent_last_24h(con), limit=cfg.daily_limit, jobs=_jobs(con),
        needs_label=inbox.NEEDS_LABEL, q=q, jur=j, st=s)


def _details(p) -> list[tuple[str, str]]:
    rows = [("Name", p["full_name"])] + [("Also known as", n) for n in p.get("other_names") or []]
    rows += [("Address" if a.get("current") else "Previous address", a["text"]) for a in p.get("addresses") or []]
    rows += [("Email", e) for e in p.get("emails") or []] + [("Phone", t) for t in p.get("phones") or []]
    if p.get("dob"):
        rows.append(("Date of birth (only if the form insists)", p["dob"]))
    return rows


def _event(e) -> dict:
    """One history row; an email's headers are split from its body so the body can fold away."""
    kind, detail = e["type"], e["detail"] or ""
    out = {"when": _when(e["ts"]), "ago": _ago(e["ts"]), "state": False, "note": "", "head": [], "body": "",
           "open": kind == "reply"}
    if kind.startswith("state:"):
        state = kind[6:]
        return {**out, "title": LABELS.get(state, state), "tone": TONE.get(state, "muted"), "state": True,
                "note": detail}
    out["title"], out["tone"] = EVENTS.get(kind, (kind.replace("_", " ").capitalize(), "muted"))
    head, blank, body = detail.partition("\n\n")
    if kind in ("sent", "chase_sent", "reply") and blank:
        for line in head.splitlines():
            k, _, val = line.partition(": ")
            out["head"].append(("Read as", {**LABELS, "autoreply": "Automatic reply"}.get(val, val))
                               if k == "Class" else (k, val))
        out["body"] = body
    else:
        out["note"] = detail
    return out


ACTIONS = {
    "submitted": "I submitted it", "confirmed": "Mark removed", "no_data": "No data held",
    "needs_review": "Needs review", "requeue": "Send again", "skipped": "Skip this broker",
}


@app.get("/r/{rid}", response_class=HTMLResponse, dependencies=[Depends(auth)])
def detail(rid: int):
    con, people, sender = ctx()
    r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404)
    b = con.execute("SELECT * FROM brokers WHERE slug=?", (r["broker"],)).fetchone()
    p = people.get(r["person"], {"full_name": r["person"], "key": r["person"]})
    events = con.execute("SELECT * FROM events WHERE request_id=? ORDER BY id DESC", (rid,)).fetchall()
    ico = None
    if r["state"] in ("overdue", "refused") and b["jurisdiction"] in ("uk", "eu"):
        chase = con.execute("SELECT ts FROM events WHERE request_id=? AND type='chase_sent' ORDER BY id DESC",
                            (rid,)).fetchone()
        _, ico = render.render(render.env(cfg.templates_dir), "ico_complaint.txt",
                               sent_date=(r["sent_at"] or "")[:10], chases=r["chases"],
                               chase_date=chase["ts"][:10] if chase else "", today=date.today().isoformat(),
                               refused=r["state"] == "refused", **render.context(p, b, rid, sender))
    link = None  # the confirmation link the inbox job could not open itself
    if r["state"] == "needs_verification":
        found = next((URL_RE.search(e["detail"]) for e in events if e["type"] == "state:needs_verification"), None)
        link = found.group(0).rstrip(".,;") if found else None
    actions = [(k, v) for k, v in ACTIONS.items()
               if not (k == "submitted" and b["method"] == "email") and not (k == "requeue" and b["method"] != "email")]
    return _page("detail", r=r, b=b, p=p, events=[_event(e) for e in events], ico=ico, details=_details(p),
                 actions=actions, guide=TODO.get(r["state"]), link=link, reply_days=cfg.reply_days)


@app.post("/r/{rid}", dependencies=[Depends(auth)])
def act(rid: int, action: str = Form(...), note: str = Form("")):
    con, _, _ = ctx()
    if not con.execute("SELECT 1 FROM requests WHERE id=?", (rid,)).fetchone():
        raise HTTPException(404)
    note = f"by hand: {note}".strip(": ")
    if action == "submitted":
        db.set_state(con, rid, "acknowledged", note, sent_at=db.now(), due_at=scheduler._ts(cfg.reply_days))
    elif action in ("confirmed", "no_data", "skipped"):
        scheduler.close(con, rid, action, note)
    elif action == "requeue":
        db.set_state(con, rid, "queued", note)
    elif action == "needs_review":
        db.set_state(con, rid, "needs_review", note)
    else:
        raise HTTPException(400)
    con.commit()
    return RedirectResponse(f"/r/{rid}", status_code=303)


@app.get("/mail", response_class=HTMLResponse, dependencies=[Depends(auth)])
def mail():
    return _page("mail", page="mail", user=cfg.gmail_user, reply=cfg.reply_address, tag=cfg.reply_tag,
                 label=inbox.LABEL, jobs=_jobs(db.connect(cfg.db)))


@app.get("/mail/optout-gmail-filters.xml", dependencies=[Depends(auth)])
def gmail_filters():
    return Response(inbox.gmail_filters(cfg), media_type="application/xml",
                    headers={"Content-Disposition": 'attachment; filename="optout-gmail-filters.xml"'})
