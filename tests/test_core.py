import os
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    brokers = tmp_path / "brokers"
    brokers.mkdir()
    for slug, b in {
        "uk-one": {"name": "UK One Ltd", "domain": "ukone.co.uk", "jurisdiction": "uk", "method": "email",
                   "contact": "dpo@ukone.co.uk", "priority": 1},
        "us-two": {"name": "US Two Inc", "domain": "ustwo.com", "jurisdiction": "us", "method": "email",
                   "contact": "privacy@ustwo.com", "priority": 3},
        "people-site": {"name": "People Site", "domain": "peoplesite.com", "jurisdiction": "us",
                        "method": "form", "contact": "https://peoplesite.com/optout", "recheck_months": 3},
    }.items():
        (brokers / f"{slug}.yaml").write_text(yaml.safe_dump(b))
    people = tmp_path / "people.yaml"
    people.write_text(yaml.safe_dump({"sender": "alex", "people": {
        "alex": {"full_name": "Alex Test", "emails": ["a@example.com"],
                  "addresses": [{"current": True, "text": "1 High St, Town, AB1 2CD"}]},
        "sam": {"full_name": "Sam Test", "emails": ["s@example.com"]},
    }}))
    monkeypatch.setenv("OPTOUT_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("OPTOUT_BROKERS", str(brokers))
    monkeypatch.setenv("OPTOUT_PEOPLE", str(people))
    monkeypatch.setenv("OPTOUT_TEMPLATES", str(ROOT / "templates"))
    monkeypatch.setenv("OPTOUT_DRY_RUN", "true")
    monkeypatch.setenv("GMAIL_USER", "sender@gmail.com")
    from optout import config, db
    cfg = config.load()
    con = db.connect(cfg.db)
    db.sync_brokers(con, cfg.brokers_dir)
    people, sender = config.load_people(cfg)
    return cfg, con, people, sender


def test_ensure_requests_once_per_pair(env):
    from optout import scheduler
    cfg, con, people, sender = env
    assert scheduler.ensure_requests(con, people) == 6
    assert scheduler.ensure_requests(con, people) == 0
    states = {(r["person"], r["broker"]): r["state"] for r in con.execute("SELECT * FROM requests")}
    assert states[("alex", "uk-one")] == "queued"
    assert states[("alex", "people-site")] == "manual_todo"  # no recipe for it


def test_dry_run_send_writes_eml_without_state_change(env):
    from optout import scheduler
    cfg, con, people, sender = env
    scheduler.ensure_requests(con, people)
    out = scheduler.send_queued(cfg, con, people, sender, 10, dry_run=True)
    assert len(out) == 4
    emls = list(cfg.outbox.glob("*.eml"))
    assert len(emls) == 4
    import email, email.policy
    text = "\n".join(email.message_from_bytes(p.read_bytes(), policy=email.policy.default)
                     .get_body().get_content() for p in emls)
    assert "Article 17" in text and "California Consumer Privacy Act" in text
    assert "authorised me to act as their agent" in text  # Sam's requests, sent by Alex
    assert con.execute("SELECT count(*) FROM requests WHERE state='queued'").fetchone()[0] == 4


def test_send_chase_overdue_and_recheck(env, monkeypatch):
    from optout import db, scheduler
    cfg, con, people, sender = env
    scheduler.ensure_requests(con, people)
    sent = []
    monkeypatch.setattr("optout.send.Mailer.send", lambda self, msg: sent.append(msg) or "sent")
    scheduler.send_queued(cfg, con, people, sender, 1, dry_run=False)
    r = con.execute("SELECT * FROM requests WHERE state='sent'").fetchone()
    assert r["broker"] == "uk-one" and r["message_ids"]
    # deadline passes -> reminder, threaded on the original
    con.execute("UPDATE requests SET due_at='2000-01-01T00:00:00+00:00' WHERE id=?", (r["id"],))
    scheduler.chase_and_escalate(cfg, con, people, sender, 5, dry_run=False)
    r2 = con.execute("SELECT * FROM requests WHERE id=?", (r["id"],)).fetchone()
    assert r2["state"] == "chased" and r2["chases"] == 1
    assert sent[-1]["In-Reply-To"] == r["message_ids"]
    con.execute("UPDATE requests SET due_at='2000-01-01T00:00:00+00:00' WHERE id=?", (r["id"],))
    scheduler.chase_and_escalate(cfg, con, people, sender, 5, dry_run=False)
    assert con.execute("SELECT state FROM requests WHERE id=?", (r["id"],)).fetchone()[0] == "overdue"
    # confirmed -> closed, then a new round once recheck_at passes
    scheduler.close(con, r["id"], "confirmed")
    assert scheduler.ensure_requests(con, people) == 0
    con.execute("UPDATE requests SET recheck_at='2000-01-01T00:00:00+00:00' WHERE id=?", (r["id"],))
    assert scheduler.ensure_requests(con, people) == 1
    assert con.execute("SELECT max(round) FROM requests WHERE broker='uk-one' AND person=?",
                       (r["person"],)).fetchone()[0] == 2


def test_quota(env):
    from optout import scheduler
    cfg, con, *_ = env
    assert scheduler.run_quota(cfg, con, hour=3) == 0
    assert scheduler.run_quota(cfg, con, hour=10) == 4  # 40/day over 12 hours


@pytest.mark.parametrize("text,label", [
    ("We have received your request and will respond within one month.", "acknowledged"),
    ("Your personal data has been deleted from our systems.", "confirmed"),
    ("We have added your details to our suppression list.", "confirmed"),
    ("We do not hold any personal data about you.", "no_data"),
    ("We were unable to locate any records matching your details.", "no_data"),
    ("Please provide a copy of your passport or driving licence.", "needs_id"),
    ("Please click the link below to confirm your request: https://x.com/confirm?t=1", "needs_verification"),
    ("These rights are only available to California residents.", "refused"),
    ("I am currently away until Monday.", "autoreply"),
    ("Hello, see attached.", "needs_review"),
])
def test_classify(text, label):
    from optout.classify import classify
    assert classify(text)[0] == label


def test_strip_quoted_ignores_our_request():
    from optout.inbox import strip_quoted
    from optout.classify import classify
    reply = ("Thanks, we have received your request.\n\nOn Mon, 20 Oct 2026 at 10:00, Alex <x@example.com> wrote:\n"
             "> If you hold no personal data about me, please confirm that.\n")
    assert classify(strip_quoted(reply))[0] == "acknowledged"
    unquoted = "Ticket created.\n\nDear Data Protection Officer,\nIf you hold no personal data about me..."
    assert "no personal data" not in strip_quoted(unquoted)


def test_verify_links_only_on_broker_domain():
    from optout.verify import candidate_links
    text = ("Confirm: https://www.spokeo.com/optout/confirm?id=9 or https://evil.example/confirm "
            "and https://mail.spokeo.com/track/open")
    assert candidate_links(text, "spokeo.com") == ["https://www.spokeo.com/optout/confirm?id=9"]


def test_form_helpers():
    from optout.forms import names, postcodes, RECIPES
    p = {"full_name": "Sam Example", "other_names": ["Sam Former (maiden name)", "Sam Sample"],
         "addresses": [{"text": "1 Example Close, Testtown, XX1 1AB"}, {"current": True, "text": "Sample View, XX12CD"},
                       {"text": "Old House, XX1 2CD"}]}
    assert postcodes(p) == ["XX1 2CD", "XX1 1AB"]
    assert names(p) == [("Sam", "Example"), ("Sam", "Former"), ("Sam", "Sample")]
    assert "192-com" in RECIPES


def test_phone_and_address_helpers():
    from optout.forms import current_address, uk_phones, RECIPES
    p = {"phones": ["+44 7700 900123", "01632 960123", "07700900123", "+1 212 555 0100"],
         "addresses": [{"text": "Old, XX1 1AB"}, {"current": True, "text": "Sample View, XX1 2CD"}]}
    assert uk_phones(p) == ["07700900123", "01632960123"]
    assert current_address(p) == "Sample View, XX1 2CD"
    assert current_address({"addresses": [{"text": "Only, AB1 2CD"}]}) == "Only, AB1 2CD"
    assert {"telephone-preference-service", "mailing-preference-service", "eyeota-dun-bradstreet",
            "privaterecords", "locatefamily"} <= RECIPES


def test_new_recipe_takes_over_untouched_manual_task(env, monkeypatch):
    from optout import scheduler
    cfg, con, people, sender = env
    scheduler.ensure_requests(con, people)
    monkeypatch.setattr(scheduler, "FORM_RECIPES", {"people-site"})
    scheduler.ensure_requests(con, people)
    assert {r[0] for r in con.execute("SELECT state FROM requests WHERE broker='people-site'")} == {"form_queued"}


def test_portal_senders():
    from optout.inbox import PORTALS
    assert PORTALS.search("m.onetrust.com")
    assert PORTALS.search("transcend.io")
    assert not PORTALS.search("notonetrust.com")
    assert not PORTALS.search("gmail.com")


def test_indented_quote_is_stripped():
    from optout.inbox import strip_quoted
    from optout.classify import classify
    reply = ("I have searched the details you provided but have been unable to locate any information relating to you.\n\n"
             "                            On\n\n                            Wed, 21 Oct at 10:07 AM\n\n"
             "                            , Alex <x@example.com> wrote:\n\n"
             "                             Dear Data Protection Officer, ... If you hold no data, please confirm that.")
    assert classify(strip_quoted(reply))[0] == "no_data"


def test_portal_receipt_is_acknowledged():
    from optout.classify import classify
    assert classify("Your request has been successfully submitted. Your Request ID is H4Z5")[0] == "acknowledged"
    assert classify("(Request ID: X) Request logged successfully")[0] == "acknowledged"


def test_redirect_to_web_form_needs_human():
    from optout.classify import classify
    assert classify("please use our online privacy request form at https://privacy.deepsync.com/")[0] == "needs_review"
    assert classify("please visit our Consumer Request Portal (https://legal.epsilon.com/dsr/)")[0] == "needs_review"


def test_known_sender_covers_closed_brokers_but_not_webmail(env):
    from optout.inbox import known_sender, match_request
    cfg, con, people, sender = env
    con.execute("INSERT INTO brokers (slug, name, domain, contact, enabled) VALUES "
                "('webmail-broker', 'Webmail Broker', 'webmailbroker.com', 'someone@gmail.com', 1)")
    assert known_sender(con, "mail.ukone.co.uk")  # no request yet, still ours
    assert known_sender(con, "m.onetrust.com")
    assert not known_sender(con, "gmail.com")
    assert not known_sender(con, "example.org")
    assert match_request(con, "gmail.com", "Family calendar", []) is None


def test_portal_completion_is_confirmed():
    from optout.classify import classify
    assert classify("Data Deletion Successfully Completed")[0] == "confirmed"


def test_bundled_brokers_all_load(tmp_path):
    from optout import config, db
    con = db.connect(tmp_path / "t.db")
    brokers = config._bundled("brokers")
    assert db.sync_brokers(con, brokers) == len(list(brokers.glob("*.yaml"))) > 0
    bad = con.execute("SELECT slug FROM brokers WHERE name='' OR contact='' "
                      "OR method NOT IN ('email','form','manual') OR jurisdiction NOT IN ('uk','eu','us')").fetchall()
    assert [r["slug"] for r in bad] == []
    not_mail = con.execute("SELECT slug FROM brokers WHERE method='email' AND contact NOT LIKE '%_@_%'").fetchall()
    assert [r["slug"] for r in not_mail] == []


def test_every_recipe_imports_and_has_a_broker():
    import importlib
    from optout import config
    from optout.forms import RECIPES
    for slug in RECIPES:
        mod = importlib.import_module(f"optout.forms.{slug.replace('-', '_')}")
        assert callable(mod.run), slug
        assert (config._bundled("brokers") / f"{slug}.yaml").is_file(), slug


def test_forms_respect_global_dry_run(env, capsys, monkeypatch):
    from optout import cli, forms
    monkeypatch.setattr(forms, "run", lambda *a, **k: pytest.fail("forms.run called in dry-run mode"))
    cli.main(["forms"])
    assert "dry-run mode" in capsys.readouterr().out


def test_missing_people_file_is_a_clear_error(env, monkeypatch, tmp_path):
    from optout import config
    monkeypatch.setenv("OPTOUT_PEOPLE", str(tmp_path / "nope.yaml"))
    with pytest.raises(SystemExit, match="no people file"):
        config.load_people(config.load())


def test_example_people_file_is_inert(env, monkeypatch):
    from optout import config
    monkeypatch.setenv("OPTOUT_PEOPLE", str(ROOT / "deploy" / "people.yaml.example"))
    people, sender = config.load_people(config.load())
    assert people == {} and sender["full_name"]


def test_dashboard_without_password_is_loopback_only(env, monkeypatch, capsys):
    import uvicorn
    from optout import cli, config
    bound = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: bound.append(kw["host"]))
    monkeypatch.delenv("OPTOUT_WEB_PASSWORD", raising=False)
    for asked, got in [(None, "127.0.0.1"), ("0.0.0.0", "127.0.0.1"), ("192.0.2.7", "127.0.0.1"),
                       ("::1", "::1"), ("localhost", "localhost")]:
        monkeypatch.delenv("OPTOUT_WEB_HOST", raising=False)
        if asked:
            monkeypatch.setenv("OPTOUT_WEB_HOST", asked)
        assert config.load().web_bind == got, asked
    monkeypatch.setenv("OPTOUT_WEB_HOST", "0.0.0.0")
    cli.main(["web"])
    assert bound == ["127.0.0.1"] and "OPTOUT_WEB_PASSWORD is not set" in capsys.readouterr().err
    monkeypatch.setenv("OPTOUT_WEB_PASSWORD", "pw")
    cli.main(["web"])
    assert bound[-1] == "0.0.0.0" and capsys.readouterr().err == ""


def test_requests_ask_for_replies_at_the_tagged_address(env, monkeypatch):
    from optout import config
    from optout.send import build
    cfg, con, people, sender = env
    assert cfg.reply_address == "sender+optout@gmail.com"
    msg = build(cfg, sender, "dpo@ukone.co.uk", "s", "b")
    assert msg["From"] == "Alex Test <sender@gmail.com>"  # brokers match the sender to their records
    assert msg["Reply-To"] == "Alex Test <sender+optout@gmail.com>"
    for user, tag in [("sender@gmail.com", ""), ("sender+mine@gmail.com", "optout"), ("", "optout")]:
        monkeypatch.setenv("GMAIL_USER", user)
        monkeypatch.setenv("OPTOUT_REPLY_TAG", tag)
        plain = config.load()
        assert plain.reply_address == user
        assert build(plain, sender, "dpo@ukone.co.uk", "s", "b")["Reply-To"] is None


def test_gmail_filter_file(env, monkeypatch, capsys):
    import xml.etree.ElementTree as ET
    from optout import cli
    ns = {"a": "http://www.w3.org/2005/Atom", "apps": "http://schemas.google.com/apps/2006"}

    def filters():
        cli.main(["gmail-filter"])
        root = ET.fromstring(capsys.readouterr().out)
        return [{p.get("name"): p.get("value") for p in e.findall("apps:property", ns)}
                for e in root.findall("a:entry", ns)]

    act = {"label": "optout", "shouldArchive": "true", "shouldNeverSpam": "true"}
    assert filters() == [{"to": "sender+optout@gmail.com", **act}, {"subject": "optout", **act}]
    monkeypatch.setenv("OPTOUT_REPLY_TAG", "")
    assert filters() == [{"subject": "optout", **act}]


class FakeGmail:
    """Just enough of Gmail's IMAP for the inbox job: messages in All Mail, labels per message."""

    def __init__(self, *raw):
        self.msgs = {str(i + 1): m.encode() for i, m in enumerate(raw)}
        self.labels = {uid: {"\\Inbox"} for uid in self.msgs}

    def login(self, *a):
        pass

    def logout(self):
        pass

    def list(self):
        return "OK", [b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, box):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        import email
        import re
        if cmd == "SEARCH":
            return "OK", [" ".join(self.msgs).encode()]
        uids = args[0].decode() if isinstance(args[0], bytes) else args[0]
        if cmd == "STORE":
            op, names = args[1], set(re.findall(r'"([^"]+)"|(\\\w+)', args[2]))
            names = {a or b for a, b in names}
            self.labels[uids] = self.labels[uids] | names if op.startswith("+") else self.labels[uids] - names
            return "OK", [b""]
        out = []
        for uid in uids.split(","):
            raw = self.msgs[uid]
            wanted = re.search(r"HEADER\.FIELDS \(([^)]*)\)", args[1])
            if wanted:  # like the real server, send back only the headers that were asked for
                msg = email.message_from_bytes(raw)
                raw = "".join(f"{h}: {v}\r\n" for h in wanted.group(1).split() for v in msg.get_all(h, [])).encode()
            out += [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", out


def _mail(frm, to, subject, body, **headers):
    extra = "".join(f"{k.replace('_', '-')}: {v}\n" for k, v in headers.items())
    return f"From: {frm}\nTo: {to}\nSubject: {subject}\nMessage-ID: <{abs(hash((frm, subject, body)))}@test>\n{extra}\n{body}\n"


def test_inbox_job_files_everything_sent_to_the_tagged_address(env, monkeypatch):
    import imaplib
    from optout import db, inbox, scheduler
    cfg, con, people, sender = env
    scheduler.ensure_requests(con, people)
    ids = {(r["person"], r["broker"]): r["id"] for r in con.execute("SELECT * FROM requests")}
    for rid in (ids["alex", "uk-one"], ids["sam", "uk-one"], ids["alex", "us-two"]):
        db.set_state(con, rid, "sent", message_ids=f"<req{rid}@gmail.com>")
    alex, sam, us = ids["alex", "uk-one"], ids["sam", "uk-one"], ids["alex", "us-two"]
    tagged, plain = "sender+optout@gmail.com", "sender@gmail.com"
    gmail = FakeGmail(
        # 1, 2: our own requests as they sit in Sent Mail, whichever From Gmail kept
        _mail(f"Alex Test <{plain}>", "dpo@ukone.co.uk", f"Erasure request [optout:{alex}]", "Dear DPO"),
        _mail(f"Alex Test <{tagged}>", "privacy@ustwo.com", f"Deletion request [optout:{us}]", "Hello"),
        # 3: a helpdesk on a domain we have never heard of, new subject, quoting our reference
        _mail("Helpdesk <noreply@tickets.example.net>", tagged, "Case 48213 opened",
              f'We have received your request and will respond within 30 days.\n\n> reference "optout:{us}"'),
        # 4: same, but nothing ties it to a request and it wants ID
        _mail("privacy@elsewhere.example.org", "Someone Else <x@example.org>", "Your privacy request",
              "Please send a copy of your passport.", Delivered_To=tagged),
        # 5: ordinary mail to the plain address is none of our business
        _mail("friend@example.org", plain, "Lunch on Friday?", "Are you free? Please confirm."),
        # 6: two open requests at this broker; the quoted reference says which one this answers
        _mail("dpo@ukone.co.uk", tagged, "Your data",
              f'We confirm your personal data has been deleted.\n\n> Please quote reference "optout:{sam}"'),
    )
    monkeypatch.setattr(imaplib, "IMAP4_SSL", lambda host: gmail)
    out = inbox.poll(cfg, con)
    state = {r["id"]: r["state"] for r in con.execute("SELECT * FROM requests")}
    assert (state[alex], state[sam], state[us]) == ("sent", "confirmed", "acknowledged"), out
    filed = {uid: labels for uid, labels in gmail.labels.items()}
    assert filed["1"] == filed["2"] == filed["5"] == {"\\Inbox"}
    assert filed["3"] == filed["6"] == {"optout"}
    assert filed["4"] == {"optout", "optout/needs-you"}
    assert inbox.poll(cfg, con) == []  # nothing is processed twice


def _well_formed(page):
    """Every tag the templates open is closed again, in order."""
    from html.parser import HTMLParser
    void = {"meta", "link", "input", "br", "hr", "img", "path", "rect"}
    stack, bad = [], []

    class Check(HTMLParser):
        def handle_starttag(self, tag, attrs):
            if tag not in void:
                stack.append(tag)

        def handle_endtag(self, tag):
            if tag not in void and (not stack or stack.pop() != tag):
                bad.append(tag)

    Check().feed(page)
    return not stack and not bad


@pytest.fixture
def web(env, monkeypatch):
    from optout import web
    monkeypatch.setattr(web, "cfg", env[0])
    return web


def test_dashboard_pages_render_in_every_state(env, web):
    from optout import db, scheduler
    cfg, con, people, sender = env
    assert "No brokers match" not in web.index() and _well_formed(web.index())  # nothing sent yet
    scheduler.ensure_requests(con, people)
    ids = {(r["person"], r["broker"]): r["id"] for r in con.execute("SELECT * FROM requests")}
    rid = ids["alex", "uk-one"]
    db.log(con, rid, "sent", f"To: dpo@ukone.co.uk\nSubject: Erasure request [optout:{rid}]\n\nDear DPO")
    db.set_state(con, rid, "sent", "to dpo@ukone.co.uk", sent_at=db.now(), due_at=scheduler._ts(30))
    db.log(con, rid, "reply", "From: dpo@ukone.co.uk\nSubject: Re: Erasure request\nClass: needs_verification\n\n"
                              "Confirm here: https://ukone.co.uk/c?t=1&u=2 <script>alert(1)</script>")
    db.set_state(con, rid, "needs_verification", "verification link needs a human: https://ukone.co.uk/c?t=1&u=2.")
    con.commit()

    home = web.index()
    assert "Confirmation links to click" in home and f'href="/r/{rid}"' in home and _well_formed(home)
    assert "Forms to fill in by hand" in home  # people-site has no recipe
    page = web.detail(rid)
    assert 'class="btn btn-primary" href="https://ukone.co.uk/c?t=1&amp;u=2"' in page  # the link, minus the full stop
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;alert(1)" in page
    assert "Read as:</b> Click link" in page and _well_formed(page)

    for state in web.LABELS:  # the ICO draft, the by-hand details and the plain cases all go through here
        for r in (rid, ids["sam", "people-site"]):
            con.execute("UPDATE requests SET state=? WHERE id=?", (state, r))
            con.commit()
            page = web.detail(r)
            assert web.LABELS[state] in page and _well_formed(page), state
    con.execute("UPDATE requests SET state='overdue' WHERE id=?", (rid,))
    con.execute("UPDATE requests SET state='manual_todo' WHERE id=?", (ids["sam", "people-site"],))
    con.commit()
    assert "ICO complaint draft" in web.detail(rid)
    by_hand = web.detail(ids["sam", "people-site"])
    assert "Sam Test" in by_hand and 'data-copy="d1"' in by_hand and "I submitted it" in by_hand

    for query in ({"v": "all"}, {"v": "all", "s": "needs"}, {"s": "overdue"}, {"q": "ukone"}, {"j": "us"}, {"v": "todo"}):
        assert _well_formed(web.index(**query)), query
    assert "UK One Ltd" in web.index(s="overdue") and "US Two Inc" not in web.index(s="overdue")
    assert "UK One Ltd" in web.index(s="needs") and "No brokers match" in web.index(q="nothing-like-this")


def test_done_my_part_puts_a_request_back_to_waiting(env, web):
    from optout import db, scheduler
    cfg, con, people, sender = env
    scheduler.ensure_requests(con, people)
    ids = {(r["person"], r["broker"]): r["id"] for r in con.execute("SELECT * FROM requests")}
    rid, form = ids["alex", "uk-one"], ids["sam", "people-site"]
    db.set_state(con, rid, "sent", "to dpo@ukone.co.uk", sent_at="2026-01-05T09:00:00+00:00", due_at=scheduler._ts(-3))
    con.commit()
    assert 'value="waiting"' not in web.detail(rid)  # already waiting on the broker: nothing to hand back

    for state, button in web.WAITING.items():
        con.execute("UPDATE requests SET state=?, due_at=? WHERE id IN (?,?)", (state, scheduler._ts(-3), rid, form))
        con.commit()
        page = web.detail(rid)
        assert f'<button name="action" value="waiting" class="btn-primary">{button}</button>' in page, state
        assert "then a reminder is sent" in page and _well_formed(page)
        by_form = web.detail(form)  # a web-form broker gets the same button instead of "I submitted it"
        assert f">{button}</button>" in by_form and "I submitted it" not in by_form
        assert "then it comes back as overdue" in by_form
        assert web.act(rid, action="waiting", note="").status_code == 303
        r = con.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        assert (r["state"], r["sent_at"]) == ("acknowledged", "2026-01-05T09:00:00+00:00"), state
        assert scheduler._ts(cfg.reply_days - 1) < r["due_at"] <= scheduler._ts(cfg.reply_days)  # a fresh deadline
    last = con.execute("SELECT * FROM events WHERE request_id=? ORDER BY id DESC", (rid,)).fetchone()
    assert (last["type"], last["detail"]) == ("state:acknowledged", "by hand")
    assert web.index(s="waiting").count(f'href="/r/{rid}"') == 1 and f'href="/r/{rid}"' not in web.index(v="todo")
    assert scheduler.chase_and_escalate(cfg, con, people, sender, 5, dry_run=True) == []  # not chased straight away

    # pressed on a stale page: the inbox job closed the request in the meantime, and that stands
    scheduler.close(con, rid, "confirmed", "matched 'has been deleted'")
    con.commit()
    assert web.act(rid, action="waiting", note="clicked it").status_code == 303
    assert con.execute("SELECT state FROM requests WHERE id=?", (rid,)).fetchone()["state"] == "confirmed"


def test_mail_page_offers_the_gmail_filters(env, web):
    import xml.etree.ElementTree as ET
    page = web.mail()
    assert "sender+optout@gmail.com" in page and "/mail/optout-gmail-filters.xml" in page and _well_formed(page)
    download = web.gmail_filters()
    assert "attachment" in download.headers["content-disposition"]
    assert len(ET.fromstring(download.body).findall("{http://www.w3.org/2005/Atom}entry")) == 2
