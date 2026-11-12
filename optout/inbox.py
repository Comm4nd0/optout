"""Poll Gmail over IMAP, match broker replies to requests, classify them, act on them."""
import email
import email.policy
import html
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from xml.sax.saxutils import escape, quoteattr

from . import db, scheduler, verify
from .classify import classify

TAG = re.compile(r"optout:(\d+)")
LABEL = "optout"
NEEDS_LABEL = "optout/needs-you"
# hosted privacy portals mail from their own domain, so these never match a broker by sender
PORTAL_DOMAINS = ("onetrust.com", "transcend.io", "trustarc.com", "ketch.com", "securiti.ai", "osano.com",
                  "truyo.com", "datagrail.io", "mineos.ai", "saymine.com", "trustsuperset.com")
# a broker whose contact is a webmail address must not claim every sender on that domain
SHARED_DOMAINS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
                  "icloud.com", "aol.com", "protonmail.com", "proton.me", "zendesk.com"}
PORTALS = re.compile(r"(^|\.)(" + "|".join(map(re.escape, PORTAL_DOMAINS)) + r")$")
FIELDS = "FROM TO CC DELIVERED-TO SUBJECT IN-REPLY-TO REFERENCES"
QUOTE_CUT = re.compile(
    r"^\s*(On\s[\s\S]{5,200}?wrote:|-{2,}\s*Original Message\s*-{2,}|_{10,}|From: .+|-+ ?Forwarded message ?-+"
    r"|Dear Data Protection Officer,|I am writing (to exercise|on behalf))", re.I | re.M)


def _hdr(v):
    return str(make_header(decode_header(v))) if v else ""


def _plain(addr: str) -> str:
    """you+tag@gmail.com -> you@gmail.com"""
    user, at, domain = addr.lower().partition("@")
    return user.split("+", 1)[0] + at + domain


def to_reply_address(cfg, hmsg) -> bool:
    """Sent to the tagged address that only our own requests give out, so it answers one of them."""
    tagged = cfg.reply_address.lower()
    if not tagged or tagged == cfg.gmail_user.lower():
        return False
    to = [_hdr(hmsg[h]) for h in ("To", "Cc", "Delivered-To") if hmsg[h]]  # no blanks: getaddresses is strict
    return any(addr.lower() == tagged for _, addr in getaddresses(to))


def by_tag(con, text):
    """The request whose 'optout:123' reference appears in the text, if any."""
    m = TAG.search(text)
    return con.execute("SELECT * FROM requests WHERE id=?", (int(m.group(1)),)).fetchone() if m else None


def gmail_filters(cfg) -> str:
    """A file for Gmail's 'Import filters'. Answers to our requests then skip the inbox and go
    straight under the label; the inbox job still reads them there."""
    rules = [("subject", "optout")]  # the [optout:123] tag in our subjects survives in most replies
    if cfg.reply_address != cfg.gmail_user:
        rules.insert(0, ("to", cfg.reply_address))
    entries = "".join(f"""  <entry>
    <category term="filter"></category>
    <title>Mail Filter</title>
    <content></content>
    <apps:property name="{name}" value={quoteattr(value)}/>
    <apps:property name="label" value="{LABEL}"/>
    <apps:property name="shouldArchive" value="true"/>
    <apps:property name="shouldNeverSpam" value="true"/>
  </entry>
""" for name, value in rules)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:apps="http://schemas.google.com/apps/2006">
  <title>Mail Filters</title>
  <id>tag:mail.google.com,2008:filters:</id>
  <updated>{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}</updated>
  <author><name>optout</name><email>{escape(cfg.gmail_user)}</email></author>
{entries}</feed>
"""


def body_text(msg) -> str:
    plain, htm = [], []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart" or part.get_filename():
            continue
        try:
            text = part.get_content()
        except Exception:  # noqa: BLE001 - odd charsets
            text = part.get_payload(decode=True).decode("utf-8", "replace")
        if part.get_content_type() == "text/plain":
            plain.append(text)
        elif part.get_content_type() == "text/html":
            # keep hrefs so verification links survive
            text = re.sub(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', r"\2 \1 ", text, flags=re.I | re.S)
            text = re.sub(r"<(script|style).*?</\1>", "", text, flags=re.I | re.S)
            htm.append(html.unescape(re.sub(r"<[^>]+>", " ", text)))
    return "\n".join(plain) if plain else "\n".join(htm)


def strip_quoted(text: str) -> str:
    """Drop the quoted copy of our own request so its wording doesn't fool the classifier."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    text = "\n".join(lines)
    m = QUOTE_CUT.search(text)
    return text[:m.start()] if m and m.start() > 0 else text


def _all_mail(imap) -> str:
    _, boxes = imap.list()
    for raw in boxes:
        line = raw.decode()
        if "\\All" in line:
            return line.rsplit(' "/" ', 1)[-1]
    return '"[Gmail]/All Mail"'


def match_request(con, frm_domain, subject, refs):
    """-> request row or None. Tag in subject > our Message-ID in refs > sender domain."""
    r = by_tag(con, subject)
    if r:
        return r
    for ref in refs:
        r = con.execute("SELECT * FROM requests WHERE instr(message_ids, ?) > 0 ORDER BY id DESC LIMIT 1",
                        (ref,)).fetchone()
        if r:
            return r
    if frm_domain and verify.registrable(frm_domain) not in SHARED_DOMAINS:
        reg = verify.registrable(frm_domain)
        rows = con.execute(
            """SELECT r.* FROM requests r JOIN brokers b ON b.slug=r.broker
               WHERE r.closed_at IS NULL AND r.state NOT IN ('queued','manual_todo','form_queued')
                 AND (b.domain=? OR b.domain=? OR b.contact LIKE ?)
               ORDER BY r.id""", (frm_domain, reg, f"%@{reg}")).fetchall()
        if len(rows) == 1:
            return rows[0]
        if rows:
            return {"ambiguous": rows}
    return None


def known_sender(con, frm_domain) -> bool:
    """Mail from a privacy portal or any broker we've written to, even if no open request matches."""
    if not frm_domain:
        return False
    if PORTALS.search(frm_domain):
        return True
    reg = verify.registrable(frm_domain)
    if reg in SHARED_DOMAINS:
        return False
    return con.execute("SELECT 1 FROM brokers WHERE domain IN (?,?) OR contact LIKE ? LIMIT 1",
                       (frm_domain, reg, f"%@{reg}")).fetchone() is not None


def apply(con, req, label, phrase, text, frm_domain):
    """Update the request according to the classification."""
    rid = req["id"]
    broker = con.execute("SELECT * FROM brokers WHERE slug=?", (req["broker"],)).fetchone()
    detail = f"matched '{phrase}'" if phrase else ""
    if label == "autoreply":
        return
    if label == "needs_verification":
        links = verify.candidate_links(text, broker["domain"], frm_domain)
        for url in links[:2]:
            ok, info = verify.open_link(url)
            db.log(con, rid, "verify_link", f"{url} -> {info}")
            if ok:
                db.set_state(con, rid, "acknowledged", "confirmation link opened")
                return
        db.set_state(con, rid, "needs_verification",
                     "verification link needs a human" + (f": {links[0]}" if links else ""))
    elif label in ("confirmed", "no_data"):
        scheduler.close(con, rid, label, detail)
    elif label == "acknowledged":
        if req["state"] in ("sent", "chased", "overdue", "needs_review"):
            db.set_state(con, rid, "acknowledged", detail)
    else:  # needs_id, refused, needs_review
        db.set_state(con, rid, label, detail)


def _file(imap, uid):
    """Label as opt-out mail and take it out of the inbox."""
    imap.uid("STORE", uid, "+X-GM-LABELS", f'("{LABEL}")')
    imap.uid("STORE", uid, "-X-GM-LABELS", r"(\Inbox)")


def refile(cfg, con, days=60) -> list[str]:
    """Tidy mail that arrived before filing existed: archive it, flag what still needs a human."""
    out = []
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(cfg.gmail_user, cfg.gmail_password)
    try:
        imap.select(_all_mail(imap))
        _, data = imap.uid("SEARCH", "X-GM-RAW", f'"label:{LABEL} OR (in:inbox newer_than:{days}d)"')
        for uid in data[0].split():
            _, hd = imap.uid("FETCH", uid, f"(BODY.PEEK[HEADER.FIELDS ({FIELDS})])")
            hmsg = email.message_from_bytes(hd[0][1])
            frm = parseaddr(_hdr(hmsg["From"]))[1].lower()
            if _plain(frm) == _plain(cfg.gmail_user):
                continue
            subject = _hdr(hmsg["Subject"])
            refs = (hmsg["In-Reply-To"] or "").split() + (hmsg["References"] or "").split()
            frm_domain = frm.rsplit("@", 1)[-1] if "@" in frm else ""
            req = match_request(con, frm_domain, subject, refs)
            if isinstance(req, dict):
                needs = True
            elif req is not None:
                cur = con.execute("SELECT state FROM requests WHERE id=?", (req["id"],)).fetchone()["state"]
                needs = cur in db.NEEDS_YOU
            elif to_reply_address(cfg, hmsg) or known_sender(con, frm_domain):
                _, full = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                msg = email.message_from_bytes(full[0][1], policy=email.policy.default)
                needs = classify(subject + "\n" + strip_quoted(body_text(msg)))[0] in db.NEEDS_YOU
            else:
                continue
            _file(imap, uid)
            if needs:
                imap.uid("STORE", uid, "+X-GM-LABELS", f'("{NEEDS_LABEL}")')
            out.append(f"{'needs-you' if needs else 'filed':9} {frm}: {subject[:70]}")
    finally:
        imap.logout()
    return out


def poll(cfg, con, days=7) -> list[str]:
    out = []
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(cfg.gmail_user, cfg.gmail_password)
    try:
        imap.select(_all_mail(imap))
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = imap.uid("SEARCH", None, f"SINCE {since}")
        uids = data[0].split()
        headers = []
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i:i + 200]).decode()
            _, hd = imap.uid("FETCH", chunk, f"(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID {FIELDS})])")
            for item in hd:
                if isinstance(item, tuple):
                    m = re.search(rb"UID (\d+)", item[0])
                    headers.append((m.group(1), email.message_from_bytes(item[1])))
        for uid, hmsg in headers:
            mid = (hmsg["Message-ID"] or f"uid-{uid.decode()}").strip()
            if con.execute("SELECT 1 FROM seen_mail WHERE msgid=?", (mid,)).fetchone():
                continue
            frm = parseaddr(_hdr(hmsg["From"]))[1].lower()
            if _plain(frm) == _plain(cfg.gmail_user):
                con.execute("INSERT OR IGNORE INTO seen_mail VALUES (?,?)", (mid, db.now()))
                continue
            subject = _hdr(hmsg["Subject"])
            refs = (hmsg["In-Reply-To"] or "").split() + (hmsg["References"] or "").split()
            frm_domain = frm.rsplit("@", 1)[-1] if "@" in frm else ""
            req = match_request(con, frm_domain, subject, refs)
            con.execute("INSERT OR IGNORE INTO seen_mail VALUES (?,?)", (mid, db.now()))
            if req is None and not (to_reply_address(cfg, hmsg) or known_sender(con, frm_domain)):
                continue
            _, full = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(full[0][1], policy=email.policy.default)
            text = body_text(msg)
            reply = strip_quoted(text)
            _file(imap, uid)
            if req is None or isinstance(req, dict):  # the reference we ask them to quote settles it
                req = by_tag(con, text) or req
            if req is None:  # an answer we can't tie to a request: file it, flag it if it wants something
                label, _ = classify(subject + "\n" + reply)
                if label in db.NEEDS_YOU:
                    imap.uid("STORE", uid, "+X-GM-LABELS", f'("{NEEDS_LABEL}")')
                out.append(f"{frm}: unmatched broker mail, {label}")
                continue
            if isinstance(req, dict):  # same broker, several open requests: let a human pick
                for r in req["ambiguous"]:
                    db.log(con, r["id"], "reply", f"From: {frm}\nSubject: {subject}\n\n{reply}")
                    db.set_state(con, r["id"], "needs_review", "reply could belong to either person")
                imap.uid("STORE", uid, "+X-GM-LABELS", f'("{NEEDS_LABEL}")')
                out.append(f"{frm}: ambiguous ({len(req['ambiguous'])} requests)")
                continue
            label, phrase = classify(subject + "\n" + reply)
            db.log(con, req["id"], "reply", f"From: {frm}\nSubject: {subject}\nClass: {label}\n\n{reply}")
            # the full text (with links) is needed for verification
            apply(con, req, label, phrase, text if label == "needs_verification" else reply, frm_domain)
            con.commit()
            state = con.execute("SELECT state FROM requests WHERE id=?", (req["id"],)).fetchone()["state"]
            if state in db.NEEDS_YOU:
                imap.uid("STORE", uid, "+X-GM-LABELS", f'("{NEEDS_LABEL}")')
            out.append(f"#{req['id']} {frm}: {label}")
        con.commit()
    finally:
        imap.logout()
    return out
