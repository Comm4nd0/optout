"""optout <command>: sync | cycle | inbox | refile | gmail-filter | preview | forms | status | web | check"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

from . import config, db, scheduler


def _ctx():
    cfg = config.load()
    con = db.connect(cfg.db)
    db.sync_brokers(con, cfg.brokers_dir)
    people, sender = config.load_people(cfg)
    return cfg, con, people, sender


def cmd_sync(a):
    cfg, con, people, _ = _ctx()
    n = con.execute("SELECT count(*) FROM brokers WHERE enabled=1").fetchone()[0]
    print(f"{n} brokers enabled, {len(people)} people, {scheduler.ensure_requests(con, people)} new requests")


def cmd_cycle(a):
    cfg, con, people, sender = _ctx()
    try:
        for line in scheduler.cycle(cfg, con, people, sender):
            print(line)
        db.beat(con, "cycle")
    except Exception as e:
        db.beat(con, "cycle", False, f"{type(e).__name__}: {e}")
        raise


def cmd_inbox(a):
    from . import inbox
    cfg, con, _, _ = _ctx()
    try:
        for line in inbox.poll(cfg, con, a.days):
            print(line)
        db.beat(con, "inbox")
    except Exception as e:
        db.beat(con, "inbox", False, f"{type(e).__name__}: {e}")
        raise


def cmd_refile(a):
    from . import inbox
    cfg, con, _, _ = _ctx()
    for line in inbox.refile(cfg, con, a.days):
        print(line)


def cmd_gmail_filter(a):
    from . import inbox
    sys.stdout.write(inbox.gmail_filters(config.load()))


def cmd_preview(a):
    """Render queued requests to .eml files without changing any state."""
    cfg, con, people, sender = _ctx()
    scheduler.ensure_requests(con, people)
    for line in scheduler.send_queued(cfg, con, people, sender, a.limit, dry_run=True):
        print(line)


def cmd_forms(a):
    from . import forms
    cfg, con, people, sender = _ctx()
    if cfg.dry_run and not a.dry_run:
        print("dry-run mode: no forms submitted (OPTOUT_DRY_RUN=false to go live)")
        return
    try:
        for line in forms.run(cfg, con, people, a.limit, a.only, a.dry_run):
            print(line)
        if not a.dry_run:
            db.beat(con, "forms")
    except Exception as e:
        db.beat(con, "forms", False, f"{type(e).__name__}: {e}")
        raise


def cmd_status(a):
    cfg, con, *_ = _ctx()
    for r in con.execute("SELECT state, count(*) n FROM requests GROUP BY state ORDER BY n DESC"):
        print(f"{r['state']:20} {r['n']}")
    print(f"sent in last 24h: {scheduler.sent_last_24h(con)} / {cfg.daily_limit}"
          f"{'  (DRY RUN)' if cfg.dry_run else ''}")


def cmd_web(a):
    import uvicorn
    from .web import app
    cfg = config.load()
    if cfg.web_bind != cfg.web_host:
        print(f"OPTOUT_WEB_PASSWORD is not set, so the dashboard only answers on this machine "
              f"(http://127.0.0.1:{cfg.web_port}). Set a password to open it on {cfg.web_host}.",
              file=sys.stderr)
    uvicorn.run(app, host=cfg.web_bind, port=cfg.web_port, log_level="warning")


def cmd_check(a):
    """Checkmk local check: stale or failing jobs -> WARN/CRIT."""
    try:
        cfg = config.load()
        con = db.connect(cfg.db)
    except Exception as e:  # noqa: BLE001
        print(f'2 "Optout" - cannot open database: {e}')
        return
    now = datetime.now(timezone.utc)
    worst, msgs = 0, []
    for name, max_age in (("cycle", 2), ("inbox", 2)):
        r = con.execute("SELECT * FROM heartbeat WHERE name=?", (name,)).fetchone()
        if not r:
            worst, msgs = max(worst, 1), msgs + [f"{name} never ran"]
            continue
        age = now - datetime.fromisoformat(r["ts"])
        if not r["ok"]:
            worst, msgs = 2, msgs + [f"{name} failed: {r['detail']}"]
        elif age > timedelta(hours=max_age):
            worst, msgs = max(worst, 1), msgs + [f"{name} last ran {age.seconds // 3600}h ago"]
    counts = dict(con.execute("SELECT state, count(*) FROM requests GROUP BY state").fetchall())
    needs = sum(counts.get(s, 0) for s in db.NEEDS_YOU)
    done = counts.get("confirmed", 0) + counts.get("no_data", 0)
    perf = f"needs_you={needs}|done={done}|sent={counts.get('sent', 0) + counts.get('chased', 0)}"
    text = "; ".join(msgs) or f"OK, {done} done, {needs} need you"
    print(f'{worst} "Optout" {perf} {text}')


def main(argv=None):
    p = argparse.ArgumentParser(prog="optout")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync").set_defaults(f=cmd_sync)
    sub.add_parser("cycle").set_defaults(f=cmd_cycle)
    s = sub.add_parser("inbox")
    s.add_argument("--days", type=int, default=7)
    s.set_defaults(f=cmd_inbox)
    s = sub.add_parser("refile")
    s.add_argument("--days", type=int, default=60)
    s.set_defaults(f=cmd_refile)
    sub.add_parser("gmail-filter").set_defaults(f=cmd_gmail_filter)
    s = sub.add_parser("preview")
    s.add_argument("--limit", type=int, default=5)
    s.set_defaults(f=cmd_preview)
    s = sub.add_parser("forms")
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--only", help="broker slug")
    s.add_argument("--dry-run", action="store_true", help="fill forms up to the submit button; change nothing")
    s.set_defaults(f=cmd_forms)
    sub.add_parser("status").set_defaults(f=cmd_status)
    sub.add_parser("web").set_defaults(f=cmd_web)
    sub.add_parser("check").set_defaults(f=cmd_check)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    sys.exit(main())
