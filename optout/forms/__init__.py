"""Web-form opt-outs with Playwright. Each recipe module exposes run(page, person, email) -> str
(email = the mailbox that receives confirmation links) and
raises NeedsHuman when it hits a CAPTCHA, a changed layout, or anything it can't handle."""
import importlib
import re
import pkgutil

from .. import db, scheduler


class NeedsHuman(Exception):
    pass


class NotListed(Exception):
    """The site has no record for this person."""


class DryRun(Exception):
    """Raised by submit() under --dry-run: the form was filled but not sent."""


DRY = False


def submit(locator):
    """The one click that sends the request; under --dry-run stop just before it."""
    if DRY:
        raise DryRun("stopped before submit")
    locator.click()


RECIPES = {m.name.replace("_", "-") for m in pkgutil.iter_modules(__path__) if not m.name.startswith("_")}


def run(cfg, con, people, limit=10, only=None, dry_run=False) -> list[str]:
    """Submit queued forms. dry_run fills each form up to its submit button and changes no state."""
    from playwright.sync_api import sync_playwright

    global DRY
    DRY = dry_run

    states = ("form_queued", "manual_todo") if dry_run else ("form_queued",)
    sql = f"""SELECT r.*, b.contact FROM requests r JOIN brokers b ON b.slug=r.broker
              WHERE r.state IN ({",".join("?" * len(states))}) AND b.enabled=1"""
    args = list(states)
    if only:
        sql, args = sql + " AND r.broker=?", [*args, only]
    rows = [r for r in con.execute(sql + " ORDER BY r.id", args).fetchall() if r["broker"] in RECIPES][:limit]
    out = []
    if not rows:
        return out
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for r in rows:
            person = people.get(r["person"])
            if not person:
                continue
            mod = importlib.import_module(f".{r['broker'].replace('-', '_')}", __name__)
            page = browser.new_page(user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
            if dry_run:
                try:
                    out.append(f"#{r['id']} {r['broker']}: dry run, no submit reached: {mod.run(page, person, cfg.gmail_user)}")
                except Exception as e:  # noqa: BLE001 - report only, state is left alone
                    out.append(f"#{r['id']} {r['broker']}: dry run: {type(e).__name__}: {e}")
                finally:
                    page.close()
                continue
            try:
                info = mod.run(page, person, cfg.gmail_user)
                db.set_state(con, r["id"], "acknowledged", f"form submitted: {info}",
                             sent_at=db.now(), due_at=scheduler._ts(cfg.reply_days))
                out.append(f"#{r['id']} {r['broker']}: submitted")
            except NotListed as e:
                scheduler.close(con, r["id"], "no_data", f"not listed: {e}")
                out.append(f"#{r['id']} {r['broker']}: not listed")
            except Exception as e:  # noqa: BLE001 - anything else goes to a human
                kind = "needs a human" if isinstance(e, NeedsHuman) else f"{type(e).__name__}"
                db.set_state(con, r["id"], "manual_todo", f"form recipe {kind}: {e}")
                out.append(f"#{r['id']} {r['broker']}: manual ({e})")
            finally:
                page.close()
                con.commit()
        browser.close()
    return out


POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?) ?(\d[A-Z]{2})\b", re.I)


def postcodes(person) -> list[str]:
    """Distinct UK postcodes from the person's addresses, current first."""
    out = []
    for a in sorted(person.get("addresses") or [], key=lambda a: not a.get("current")):
        m = POSTCODE.search(a["text"])
        pc = f"{m.group(1)} {m.group(2)}".upper() if m else None
        if pc and pc not in out:
            out.append(pc)
    return out


def uk_phones(person) -> list[str]:
    """UK numbers in national format (07..., 01...); anything else is dropped."""
    out = []
    for ph in person.get("phones") or []:
        n = re.sub(r"[^\d+]", "", str(ph))
        n = re.sub(r"^(\+44|0044)", "0", n)
        if re.fullmatch(r"0\d{9,10}", n) and n not in out:
            out.append(n)
    return out


def current_address(person) -> str:
    addrs = person.get("addresses") or []
    cur = [a for a in addrs if a.get("current")] or addrs
    return cur[0]["text"] if cur else ""


def names(person) -> list[tuple[str, str]]:
    """(forename, surname) for the full name and every alias; '(maiden name)'-style notes dropped."""
    out = []
    for n in [person["full_name"], *(person.get("other_names") or [])]:
        parts = re.sub(r"\(.*?\)", "", n).split()
        if len(parts) >= 2 and (parts[0], parts[-1]) not in out:
            out.append((parts[0], parts[-1]))
    return out


def squash(text: str, limit: int = 200) -> str:
    """Page text on one line, for error messages."""
    return re.sub(r"\s+", " ", text)[:limit]


def captcha_check(page):
    html = page.content().lower()
    if any(k in html for k in ("g-recaptcha", "h-captcha", "hcaptcha", "cf-turnstile", "challenge-platform",
                               "are you a robot", "verify you are human", "just a moment...",
                               "performing security verification")):
        raise NeedsHuman("CAPTCHA / bot check")
