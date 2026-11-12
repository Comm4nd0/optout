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
