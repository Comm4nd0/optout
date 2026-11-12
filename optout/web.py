"""LAN dashboard: progress grid, 'needs you' queue, per-request log and actions."""
import secrets
from datetime import date

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, DictLoader, select_autoescape

from . import config, db, render, scheduler

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

BASE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{% block title %}Optout{% endblock %}</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--mute:#6b7280;--line:#e3e6ea;--acc:#2f5bd3;
--good:#1f8a4c;--good-bg:#e3f5ea;--info:#2f5bd3;--info-bg:#e6edfc;--warn:#9a6500;--warn-bg:#fdf1d8;
--bad:#b42318;--bad-bg:#fde7e5;--muted-bg:#eef0f3}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#191d23;--fg:#e6e8eb;--mute:#9aa3ae;--line:#2a3038;
--acc:#7aa2ff;--good:#6fd39b;--good-bg:#173323;--info:#8fb0ff;--info-bg:#1b2742;--warn:#f0c060;--warn-bg:#3a2e14;
--bad:#ff8a80;--bad-bg:#3d1c1a;--muted-bg:#242a32}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}
main{max-width:1100px;margin:0 auto;padding:20px 16px 60px}a{color:var(--acc);text-decoration:none}
a:hover{text-decoration:underline}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 10px}
.sub{color:var(--mute);margin:0 0 18px}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}
.stat b{display:block;font-size:24px}.stat span{color:var(--mute);font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--mute)}
.wrap{overflow-x:auto}.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;white-space:nowrap}
.good{color:var(--good);background:var(--good-bg)}.info{color:var(--info);background:var(--info-bg)}
.warn{color:var(--warn);background:var(--warn-bg)}.bad{color:var(--bad);background:var(--bad-bg)}
.muted{color:var(--mute);background:var(--muted-bg)}.small{font-size:13px;color:var(--mute)}
form.filters{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 10px}
input,select,button,textarea{font:inherit;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:7px;padding:5px 9px}
button{cursor:pointer}button.primary{background:var(--acc);border-color:var(--acc);color:#fff}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;font-size:13px;margin:6px 0 0}
.actions{display:flex;flex-wrap:wrap;gap:8px}.ev{border-top:1px solid var(--line);padding:10px 0}
.banner{background:var(--warn-bg);color:var(--warn);border-radius:8px;padding:8px 12px;margin:0 0 14px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;margin:0}dt{color:var(--mute)}dd{margin:0;word-break:break-word}
</style></head><body><main>{% block body %}{% endblock %}</main></body></html>"""

INDEX = """{% extends "base" %}{% block body %}
<h1>Data broker opt-outs</h1>
<p class="sub">{{ people|join(", ") }} · {{ n_brokers }} brokers · sent {{ sent24 }}/{{ limit }} in the last 24h</p>
{% if dry %}<div class="banner">Dry-run mode: nothing is being sent. Set OPTOUT_DRY_RUN=false to go live.</div>{% endif %}
<div class="stats">
{% for label, n, tone in stats %}<div class="card stat"><b class="{{ tone }}" style="background:none">{{ n }}</b><span>{{ label }}</span></div>{% endfor %}
</div>
<h2>Needs you ({{ needs|length }})</h2>
{% if needs %}<div class="wrap"><table><tr><th>Broker</th><th>Person</th><th>Status</th><th>Since</th></tr>
{% for r in needs %}<tr><td><a href="/r/{{ r.id }}">{{ r.name }}</a></td><td>{{ r.person|title }}</td>
<td><span class="pill {{ tone[r.state] }}">{{ labels[r.state] }}</span></td><td class="small">{{ r.updated_at[:10] }}</td></tr>{% endfor %}
</table></div>{% else %}<p class="small">Nothing to do.</p>{% endif %}
<h2>All brokers</h2>
<form class="filters" method="get">
<input name="q" value="{{ q }}" placeholder="Search broker">
<select name="j"><option value="">All regions</option>{% for j in ["uk","eu","us"] %}<option {{ "selected" if j==jur }}>{{ j }}</option>{% endfor %}</select>
<select name="s"><option value="">All states</option>{% for k,v in labels.items() %}<option value="{{ k }}" {{ "selected" if k==st }}>{{ v }}</option>{% endfor %}</select>
<button>Filter</button></form>
<div class="wrap"><table><tr><th>Broker</th><th>Region</th><th>Method</th>{% for p in people %}<th>{{ p|title }}</th>{% endfor %}</tr>
{% for b in grid %}<tr><td>{{ b.name }}<div class="small">{{ b.domain }}</div></td><td>{{ b.jurisdiction|upper }}</td><td class="small">{{ b.method }}</td>
{% for p in people %}{% set r = b.reqs.get(p) %}<td>{% if r %}<a href="/r/{{ r.id }}"><span class="pill {{ tone[r.state] }}">{{ labels[r.state] }}</span></a>{% endif %}</td>{% endfor %}</tr>{% endfor %}
</table></div>{% if truncated %}<p class="small">Showing first {{ grid|length }}; filter to see more.</p>{% endif %}
{% endblock %}"""

DETAIL = """{% extends "base" %}{% block title %}{{ b.name }} · Optout{% endblock %}{% block body %}
<p><a href="/">← All brokers</a></p>
<h1>{{ b.name }} <span class="pill {{ tone[r.state] }}">{{ labels[r.state] }}</span></h1>
<p class="sub">{{ r.person|title }} · round {{ r.round }} · request optout:{{ r.id }}</p>
<div class="card"><dl>
<dt>Domain</dt><dd>{{ b.domain }}</dd><dt>Method</dt><dd>{{ b.method }}</dd>
<dt>Contact</dt><dd>{% if b.contact.startswith("http") %}<a href="{{ b.contact }}" target="_blank" rel="noopener">{{ b.contact }}</a>{% else %}{{ b.contact }}{% endif %}</dd>
{% if b.alt_contact %}<dt>Also</dt><dd>{% if b.alt_contact.startswith("http") %}<a href="{{ b.alt_contact }}" target="_blank" rel="noopener">{{ b.alt_contact }}</a>{% else %}{{ b.alt_contact }}{% endif %}</dd>{% endif %}
{% if b.needs_id %}<dt>ID</dt><dd>Broker usually asks for ID</dd>{% endif %}
{% if b.notes %}<dt>Notes</dt><dd>{{ b.notes }}</dd>{% endif %}
<dt>Sent</dt><dd>{{ r.sent_at or "–" }}</dd><dt>Reply due</dt><dd>{{ r.due_at or "–" }}</dd>
{% if r.recheck_at %}<dt>Re-request</dt><dd>{{ r.recheck_at[:10] }}</dd>{% endif %}
</dl></div>
{% if r.state in ("manual_todo","form_queued") %}<h2>Do it by hand</h2><div class="card">
<p>Open the contact link above, search for {{ p.full_name }}, and submit the removal. Details to copy:</p>
<pre>{{ details }}</pre><p class="small">Then press “I submitted it”. The request goes to Acknowledged with a {{ reply_days }}-day deadline.</p></div>{% endif %}
{% if ico %}<h2>ICO complaint draft</h2><pre>{{ ico }}</pre>{% endif %}
<h2>Actions</h2><form method="post" action="/r/{{ r.id }}" class="actions">
{% for val, text in actions %}<button name="action" value="{{ val }}" {{ 'class=primary' if loop.first }}>{{ text }}</button>{% endfor %}
<input name="note" placeholder="Note (optional)" style="flex:1;min-width:200px"></form>
<h2>History</h2><div class="card">{% for e in events %}<div class="ev"><b>{{ e.type }}</b> <span class="small">{{ e.ts }}</span>
{% if e.detail %}<pre>{{ e.detail }}</pre>{% endif %}</div>{% else %}<p class="small">No events.</p>{% endfor %}</div>
{% endblock %}"""

jenv = Environment(loader=DictLoader({"base": BASE, "index": INDEX, "detail": DETAIL}),
                   autoescape=select_autoescape(default=True))


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
def index(q: str = "", j: str = "", s: str = ""):
    con, people, _ = ctx()
    latest = con.execute(
        """SELECT r.*, b.name FROM requests r JOIN brokers b ON b.slug=r.broker
           WHERE r.id IN (SELECT max(id) FROM requests GROUP BY person, broker)""").fetchall()
    counts = {}
    for r in latest:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    stats = [
        ("Removed / no data", counts.get("confirmed", 0) + counts.get("no_data", 0), "good"),
        ("Waiting on broker", sum(counts.get(k, 0) for k in ("sent", "acknowledged", "chased")), "info"),
        ("Needs you", sum(counts.get(k, 0) for k in db.NEEDS_YOU), "warn"),
        ("Queued", counts.get("queued", 0) + counts.get("form_queued", 0), "muted"),
    ]
    needs = sorted((r for r in latest if r["state"] in db.NEEDS_YOU and r["state"] != "manual_todo"),
                   key=lambda r: r["updated_at"])
    manual = [r for r in latest if r["state"] == "manual_todo"]
    needs += manual[:50]
    sql, args = "SELECT * FROM brokers WHERE enabled=1", []
    if q:
        sql, args = sql + " AND (name LIKE ? OR domain LIKE ?)", args + [f"%{q}%", f"%{q}%"]
    if j:
        sql, args = sql + " AND jurisdiction=?", args + [j]
    brokers = con.execute(sql + " ORDER BY priority, name", args).fetchall()
    by_broker = {}
    for r in latest:
        by_broker.setdefault(r["broker"], {})[r["person"]] = r
    grid = []
    for b in brokers:
        reqs = by_broker.get(b["slug"], {})
        if s and not any(r["state"] == s for r in reqs.values()):
            continue
        grid.append({**dict(b), "reqs": reqs})
    html = jenv.get_template("index").render(
        people=list(people), n_brokers=len(brokers), sent24=scheduler.sent_last_24h(con),
        limit=cfg.daily_limit, dry=cfg.dry_run, stats=stats, needs=needs, grid=grid[:400],
        truncated=len(grid) > 400, q=q, jur=j, st=s, labels=LABELS, tone=TONE)
    return html


def _details(p):
    lines = [f"Name: {p['full_name']}"] + [f"Also known as: {n}" for n in p.get("other_names") or []]
    lines += [f"{'Address' if a.get('current') else 'Previous address'}: {a['text']}" for a in p.get("addresses") or []]
    lines += [f"Email: {e}" for e in p.get("emails") or []] + [f"Phone: {t}" for t in p.get("phones") or []]
    if p.get("dob"):
        lines.append(f"Date of birth (only if the form insists): {p['dob']}")
    return "\n".join(lines)


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
    actions = [(k, v) for k, v in ACTIONS.items()
               if not (k == "submitted" and b["method"] == "email") and not (k == "requeue" and b["method"] != "email")]
    return jenv.get_template("detail").render(
        r=r, b=b, p=p, events=events, ico=ico, details=_details(p), actions=actions,
        reply_days=cfg.reply_days, labels=LABELS, tone=TONE)


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
