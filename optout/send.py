"""Send mail through Gmail SMTP (app password), or write .eml files in dry-run mode."""
import smtplib
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path


def build(cfg, sender, to, subject, body, attachments=(), in_reply_to=None):
    msg = EmailMessage()
    msg["From"] = f'{sender["full_name"]} <{cfg.gmail_user or "dry-run@localhost"}>'
    if cfg.reply_address != cfg.gmail_user:  # same mailbox, but a Gmail filter can catch the answers
        msg["Reply-To"] = f'{sender["full_name"]} <{cfg.reply_address}>'
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(cfg.gmail_user.split("@")[-1] or "localhost"))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    for path in attachments:
        p = Path(path)
        msg.add_attachment(p.read_bytes(), maintype="application", subtype="pdf", filename=p.name)
    return msg


class Mailer:
    def __init__(self, cfg, dry_run=None):
        self.cfg = cfg
        self.dry_run = cfg.dry_run if dry_run is None else dry_run
        self.smtp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.smtp:
            try:
                self.smtp.quit()
            except smtplib.SMTPException:
                pass

    def send(self, msg) -> str:
        if self.dry_run:
            self.cfg.outbox.mkdir(parents=True, exist_ok=True)
            name = msg["Message-ID"].strip("<>").replace("@", "_") + ".eml"
            (self.cfg.outbox / name).write_bytes(bytes(msg))
            return f"dry-run: {self.cfg.outbox / name}"
        if not self.smtp:
            self.smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
            self.smtp.login(self.cfg.gmail_user, self.cfg.gmail_password)
        self.smtp.send_message(msg)
        return "sent"
