"""Settings come from the environment (systemd EnvironmentFile=/etc/optout/optout.env)."""
import os
from dataclasses import dataclass
from pathlib import Path

import yaml


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def _bundled(name: str) -> Path:
    """brokers/ and templates/: inside the package in an installed wheel, beside it in a checkout."""
    pkg = Path(__file__).resolve().parent
    inside = pkg / "data" / name
    return inside if inside.is_dir() else pkg.parent / name


@dataclass
class Config:
    db: Path
    brokers_dir: Path
    people_file: Path
    templates_dir: Path
    outbox: Path
    state_dir: Path
    gmail_user: str
    gmail_password: str
    reply_tag: str
    daily_limit: int
    send_hours: tuple[int, int]
    dry_run: bool
    reply_days: int
    escalate_days: int
    web_user: str
    web_password: str
    web_host: str
    web_port: int

    @property
    def reply_address(self) -> str:
        """Where brokers are asked to reply: you+optout@gmail.com, which reaches the same mailbox
        but lets one Gmail filter keep the answers out of the inbox."""
        user, _, domain = self.gmail_user.partition("@")
        if not (domain and self.reply_tag) or "+" in user:
            return self.gmail_user
        return f"{user}+{self.reply_tag}@{domain}"

    @property
    def web_bind(self) -> str:
        """Address the dashboard really listens on. Without a password it has no login,
        so it is only reachable from this machine, whatever OPTOUT_WEB_HOST says."""
        if self.web_password or self.web_host in ("127.0.0.1", "::1", "localhost"):
            return self.web_host
        return "127.0.0.1"


def load() -> Config:
    e = os.environ.get
    state = Path(e("OPTOUT_STATE", "/var/lib/optout"))
    h = e("OPTOUT_SEND_HOURS", "9-21").split("-")
    return Config(
        db=Path(e("OPTOUT_DB", str(state / "optout.db"))),
        brokers_dir=Path(e("OPTOUT_BROKERS", str(_bundled("brokers")))),
        people_file=Path(e("OPTOUT_PEOPLE", "/etc/optout/people.yaml")),
        templates_dir=Path(e("OPTOUT_TEMPLATES", str(_bundled("templates")))),
        outbox=Path(e("OPTOUT_OUTBOX", str(state / "outbox"))),
        state_dir=state,
        gmail_user=e("GMAIL_USER", ""),
        gmail_password=e("GMAIL_APP_PASSWORD", "").replace(" ", ""),
        reply_tag=e("OPTOUT_REPLY_TAG", "optout").strip(),
        daily_limit=int(e("OPTOUT_DAILY_LIMIT", "40")),
        send_hours=(int(h[0]), int(h[1])),
        dry_run=_bool(e("OPTOUT_DRY_RUN", "true")),
        reply_days=int(e("OPTOUT_REPLY_DAYS", "30")),
        escalate_days=int(e("OPTOUT_ESCALATE_DAYS", "30")),
        web_user=e("OPTOUT_WEB_USER", "admin"),
        web_password=e("OPTOUT_WEB_PASSWORD", ""),
        web_host=e("OPTOUT_WEB_HOST", "0.0.0.0"),
        web_port=int(e("OPTOUT_WEB_PORT", "8080")),
    )


def load_people(cfg: Config) -> tuple[dict, dict]:
    """people.yaml -> ({key: person}, sender). Disabled people are dropped; the sender
    (whose Gmail sends everything) acts as authorised agent for everyone else."""
    if not cfg.people_file.is_file():
        raise SystemExit(f"{cfg.people_file}: no people file. Copy deploy/people.yaml.example there "
                         "(or point OPTOUT_PEOPLE at your own) and fill it in.")
    data = yaml.safe_load(cfg.people_file.read_text()) or {}
    people = {}
    for key, p in (data.get("people") or {}).items():
        p["key"] = key
        for k in ("other_names", "emails", "phones", "addresses"):
            p[k] = p.get(k) or []
        p["addresses"] = [{"current": False, **a} for a in p["addresses"]]
        if p.get("enabled", True) and p.get("full_name"):
            people[key] = p
    sender_key = data.get("sender") or next(iter(people), None)
    sender = (data.get("people") or {}).get(sender_key)
    if not sender:
        raise SystemExit(f"{cfg.people_file}: sender '{sender_key}' not found under people")
    return people, sender
