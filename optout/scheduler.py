"""Request lifecycle: create requests, send them within quota, chase, escalate, re-request."""
import math
from datetime import datetime, timedelta, timezone

from . import db, render
from .send import Mailer, build

FORM_RECIPES = None  # filled lazily from optout.forms


def _ts(days=0):
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _has_recipe(slug):
    global FORM_RECIPES
    if FORM_RECIPES is None:
        try:
            from .forms import RECIPES
            FORM_RECIPES = set(RECIPES)
        except ImportError:
            FORM_RECIPES = set()
    return slug in FORM_RECIPES


def ensure_requests(con, people) -> int:
    """One open request per person x enabled broker; a closed one reopens after recheck_at."""
    made = 0
    brokers = con.execute("SELECT * FROM brokers WHERE enabled=1").fetchall()
    for key in people:
        for b in brokers:
            last = con.execute(
                "SELECT * FROM requests WHERE person=? AND broker=? ORDER BY id DESC LIMIT 1",
                (key, b["slug"])).fetchone()
            if last and (last["closed_at"] is None or (last["recheck_at"] or "9") > db.now()):
                continue
            rnd = last["round"] + 1 if last else 1
            if b["method"] == "email":
                state = "queued"
            elif b["method"] == "form" and _has_recipe(b["slug"]):
                state = "form_queued"
            else:
                state = "manual_todo"
            cur = con.execute(
                "INSERT INTO requests(person, broker, round, state, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)", (key, b["slug"], rnd, state, db.now(), db.now()))
            db.log(con, cur.lastrowid, f"state:{state}", f"round {rnd} created")
            made += 1
    # brokers that gained a form recipe: untouched manual tasks become automatic
    for r in con.execute("""SELECT r.id, r.broker FROM requests r WHERE r.state='manual_todo'
                            AND (SELECT count(*) FROM events e WHERE e.request_id=r.id) = 1""").fetchall():
        if _has_recipe(r["broker"]):
            db.set_state(con, r["id"], "form_queued", "form recipe available")
    con.commit()
    return made


def sent_last_24h(con) -> int:
    return con.execute("SELECT count(*) FROM events WHERE type IN ('sent','chase_sent') AND ts > ?",
                       (_ts(-1),)).fetchone()[0]


def run_quota(cfg, con, hour=None) -> int:
    """Spread the daily limit evenly over the sending window."""
    hour = datetime.now().hour if hour is None else hour
    start, end = cfg.send_hours
    if not (start <= hour < end):
        return 0
    per_run = math.ceil(cfg.daily_limit / max(1, end - start))
    return max(0, min(per_run, cfg.daily_limit - sent_last_24h(con)))


def _open_email_queue(con, limit):
    return con.execute(
        """SELECT r.*, b.name, b.contact, b.jurisdiction FROM requests r JOIN brokers b ON b.slug=r.broker
           WHERE r.state='queued' AND b.enabled=1 AND b.method='email'
           ORDER BY b.priority, r.round, b.slug, r.person LIMIT ?""", (limit,)).fetchall()


def send_queued(cfg, con, people, sender, limit, dry_run=None) -> list[str]:
    jenv = render.env(cfg.templates_dir)
    out = []
    with Mailer(cfg, dry_run) as mailer:
        for r in _open_email_queue(con, limit):
            person = people.get(r["person"])
            if not person:
                continue
            broker = con.execute("SELECT * FROM brokers WHERE slug=?", (r["broker"],)).fetchone()
            subject, body = render.render(jenv, render.template_for(broker),
                                          **render.context(person, broker, r["id"], sender))
            attach = [person["agent_letter"]] if person["key"] != sender["key"] and person.get("agent_letter") else []
            msg = build(cfg, sender, broker["contact"], subject, body, attach)
            result = mailer.send(msg)
            if mailer.dry_run:
                out.append(f"#{r['id']} {r['person']} -> {broker['slug']}: {result}")
                continue
            mids = (r["message_ids"] + " " + msg["Message-ID"]).strip()
            db.log(con, r["id"], "sent", f"To: {broker['contact']}\nSubject: {subject}\n\n{body}")
            db.set_state(con, r["id"], "sent", f"to {broker['contact']}", sent_at=db.now(),
                         due_at=_ts(cfg.reply_days), message_ids=mids)
            con.commit()
            out.append(f"#{r['id']} {r['person']} -> {broker['slug']}: sent")
    return out


def _original(con, rid):
    row = con.execute("SELECT detail FROM events WHERE request_id=? AND type='sent' ORDER BY id LIMIT 1",
                      (rid,)).fetchone()
    return row["detail"].split("\n\n", 1)[-1] if row else ""


def chase_and_escalate(cfg, con, people, sender, quota, dry_run=None) -> list[str]:
    out = []
    now = db.now()
    jenv = render.env(cfg.templates_dir)
    # second deadline missed -> needs you (ICO draft on the dashboard)
    for r in con.execute("SELECT id FROM requests WHERE state='chased' AND due_at < ?", (now,)).fetchall():
        db.set_state(con, r["id"], "overdue", "no reply after reminder")
        out.append(f"#{r['id']} overdue")
    # manual/form submissions that were never confirmed
    for r in con.execute("""SELECT r.id FROM requests r JOIN brokers b ON b.slug=r.broker
                            WHERE r.state='acknowledged' AND b.method!='email' AND r.due_at < ?""",
                         (now,)).fetchall():
        db.set_state(con, r["id"], "overdue", "form submission never confirmed")
        out.append(f"#{r['id']} overdue")
    con.commit()
    due = con.execute(
        """SELECT r.* FROM requests r JOIN brokers b ON b.slug=r.broker
           WHERE r.state IN ('sent','acknowledged') AND b.method='email' AND r.due_at < ?
           ORDER BY r.due_at LIMIT ?""", (now, quota)).fetchall()
    with Mailer(cfg, dry_run) as mailer:
        for r in due:
            person = people.get(r["person"])
            if not person:
                continue
            broker = con.execute("SELECT * FROM brokers WHERE slug=?", (r["broker"],)).fetchone()
            ctx = render.context(person, broker, r["id"], sender)
            subject, body = render.render(jenv, "chase.txt", sent_date=(r["sent_at"] or "")[:10],
                                          original=_original(con, r["id"]), **ctx)
            last_mid = (r["message_ids"].split() or [None])[-1]
            msg = build(cfg, sender, broker["contact"], subject, body, in_reply_to=last_mid)
            result = mailer.send(msg)
            if mailer.dry_run:
                out.append(f"#{r['id']} chase: {result}")
                continue
            db.log(con, r["id"], "chase_sent", f"Subject: {subject}\n\n{body}")
            db.set_state(con, r["id"], "chased", "reminder sent", chases=r["chases"] + 1,
                         due_at=_ts(cfg.escalate_days),
                         message_ids=(r["message_ids"] + " " + msg["Message-ID"]).strip())
            con.commit()
            out.append(f"#{r['id']} chased")
    return out


def close(con, rid, state, detail=""):
    """Close a request (confirmed/no_data/skipped) and schedule the next round."""
    r = con.execute("SELECT r.*, b.recheck_months FROM requests r JOIN brokers b ON b.slug=r.broker "
                    "WHERE r.id=?", (rid,)).fetchone()
    db.set_state(con, rid, state, detail, recheck_at=_ts(30 * (r["recheck_months"] or 6)))


def cycle(cfg, con, people, sender) -> list[str]:
    out = [f"{ensure_requests(con, people)} new requests"]
    if cfg.dry_run:
        out.append("dry-run mode: nothing sent (OPTOUT_DRY_RUN=false to go live)")
        return out
    quota = run_quota(cfg, con)
    chased = chase_and_escalate(cfg, con, people, sender, quota)
    out += chased
    quota = run_quota(cfg, con)
    out += send_queued(cfg, con, people, sender, quota)
    return out
