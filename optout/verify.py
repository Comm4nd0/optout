"""Open confirmation links from broker emails, but only on the broker's own domain."""
import re
import urllib.request
from urllib.parse import urlparse

from .classify import urls

HINT = re.compile(r"confirm|verif|opt-?out|optout|validate|activate|unsubscribe|remov|suppress|privacy", re.I)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def registrable(host: str) -> str:
    """Rough eTLD+1 (enough for .com/.co.uk style domains)."""
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "org", "ac", "gov", "com", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def candidate_links(text: str, broker_domain: str, sender_domain: str = "") -> list[str]:
    allowed = {registrable(d) for d in (broker_domain, sender_domain) if d}
    out = []
    for u in urls(text):
        host = urlparse(u).hostname or ""
        if registrable(host) in allowed and HINT.search(u) and u not in out:
            out.append(u)
    return out


def open_link(url: str) -> tuple[bool, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read(200_000).decode("utf-8", "replace")
            ok = 200 <= r.status < 400
            # pages that still need a button press are left for a human (or a form recipe)
            if re.search(r"<form[^>]*>.*?(confirm|submit|verify)", body, re.I | re.S):
                return False, f"HTTP {r.status}, page has a form to submit"
            return ok, f"HTTP {r.status}"
    except Exception as e:  # noqa: BLE001 - any failure means a human should look
        return False, f"{type(e).__name__}: {e}"
