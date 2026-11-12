#!/usr/bin/env python3
"""Build brokers/*.yaml from the source lists in tools/.

Sources, highest precedence first (de-duplicated by registrable domain):
  uk_curated.yaml   hand-checked UK/EU brokers
  badbool.md        Big Ass Data Broker Opt-Out List (people-search sites)
  ca_registry.csv   California Data Broker Registry export

Only files with `generated: true` are overwritten, so hand edits survive: set
`generated: false` in a broker file after changing it.
"""
import csv
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
OUT = ROOT / "brokers"
EMAIL = re.compile(r"^[\w.+'-]+@[\w-]+(\.[\w-]+)+$")


def registrable(host):
    parts = host.lower().strip(".").split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "org", "ac", "gov", "com", "net") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def domain_of(url):
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    return registrable(urlparse(url).hostname or "")


def clean_email(s):
    s = (s or "").strip().strip("<>").replace(" ", "")
    s = re.sub(r"[\[(]at[\])]", "@", s, flags=re.I)
    s = re.sub(r"[\[(]dot[\])]", ".", s, flags=re.I)
    s = s.removeprefix("mailto:")
    return s.lower() if EMAIL.match(s) else ""


def slugify(name):
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s[:60] or "broker"


def from_curated():
    p = TOOLS / "uk_curated.yaml"
    if not p.exists():
        return []
    out = []
    for b in yaml.safe_load(p.read_text()) or []:
        b.setdefault("jurisdiction", "uk")
        b.setdefault("priority", 1)
        b.setdefault("recheck_months", 6)
        b.setdefault("category", "uk-curated")
        out.append(b)
    return out


def from_badbool():
    text = (TOOLS / "badbool.md").read_text()
    start = text.index("## People Search Sites")
    end = text.index("## Special Circumstances")
    out = []
    for sec in re.split(r"^### ", text[start:end], flags=re.M)[1:]:
        head, _, body = sec.partition("\n")
        flags = head
        name = re.sub(r"[^\w .&’'-]", "", head).strip()
        links = re.findall(r"\[([^\]]*)\]\((https?://[^)]+)\)", body)
        emails = [clean_email(e) for e in re.findall(r"<([^>]+@[^>]+)>", body)]
        emails = [e for e in emails if e]
        form = next((u for t, u in links if re.search(r"opt|remov|privacy|suppress|control", t + u, re.I)), "")
        site = links[0][1] if links else ""
        domain = domain_of(site) or domain_of(form)
        if not domain:
            continue
        notes = re.sub(r"\[([^\]]*)\]\([^)]+\)", r"\1", body).strip().replace("\n", " ")
        extra = [w for f, w in (("📞", "may need a phone call"), ("💰", "charges money"), ("🎫", "asks for ID"))
                 if f in flags]
        out.append({
            "name": name, "domain": domain, "jurisdiction": "us",
            "method": "form" if form else ("email" if emails else "manual"),
            "contact": form or (emails[0] if emails else site),
            "alt_contact": emails[0] if form and emails else "",
            "needs_id": "🎫" in flags,
            "recheck_months": 3,
            "priority": 2 if ("💐" in flags or "☠" in flags) else 3,
            "category": "people-search",
            "notes": ("; ".join(extra) + ". " if extra else "") + notes[:400],
            "source": "https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List",
        })
    return out


def from_ca():
    out = []
    with open(TOOLS / "ca_registry.csv", encoding="utf-8-sig", errors="replace") as f:
        for row in csv.DictReader(f):
            get = lambda k: next((v for kk, v in row.items() if kk and kk.startswith(k)), "") or ""
            name = (get("Doing Business As").strip() or get("Data broker name").strip())
            site = get("Data broker primary website")
            email = clean_email(get("Data broker primary contact email"))
            rights = get("Data broker's primary website that contains details").strip()
            domain = domain_of(site) or domain_of(rights)
            if not name or not domain:
                continue
            out.append({
                "name": name, "domain": domain, "jurisdiction": "us",
                "method": "email" if email else ("form" if rights.startswith("http") else "manual"),
                "contact": email or rights or site,
                "alt_contact": rights if email and rights.startswith("http") else "",
                "needs_id": False, "recheck_months": 6, "priority": 3, "category": "ca-registry",
                "notes": f"Collects precise geolocation: {get('Data broker collects consumers' + chr(39) + ' precise geolocation') or '?'}",
                "source": "https://cppa.ca.gov/data_broker_registry/",
            })
    return out


def main():
    OUT.mkdir(exist_ok=True)
    seen, slugs, written, kept = set(), set(), 0, 0
    for b in from_curated() + from_badbool() + from_ca():
        key = registrable(b["domain"])
        if key in seen:
            continue
        seen.add(key)
        slug = slugify(b["name"])
        while slug in slugs:
            slug += "-x"
        slugs.add(slug)
        path = OUT / f"{slug}.yaml"
        if path.exists() and not (yaml.safe_load(path.read_text()) or {}).get("generated", False):
            kept += 1
            continue
        b["generated"] = True
        b.setdefault("enabled", True)
        path.write_text(yaml.safe_dump(b, sort_keys=False, allow_unicode=True, width=100))
        written += 1
    removed = 0
    for path in OUT.glob("*.yaml"):
        if path.stem not in slugs and (yaml.safe_load(path.read_text()) or {}).get("generated", False):
            path.unlink()
            removed += 1
    print(f"{removed} stale removed, {written} written, {kept} hand-edited kept, {len(seen)} unique domains", file=sys.stderr)


if __name__ == "__main__":
    main()
