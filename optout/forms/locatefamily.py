"""LocateFamily: walk the surname pages for a listing of this person (forename + a UK row or one of
their postcodes). None -> not listed. A hit -> a human clicks its bin icon (the removal page is unknown)."""
import re

from . import NeedsHuman, NotListed, captcha_check, names, postcodes

BASE = "https://www.locatefamily.com"
ROWS_JS = """els => els.map(a => [a.getAttribute('href'),
    (a.closest('.row') || a.parentElement.parentElement.parentElement).innerText.replace(/\\s+/g, ' ')])"""


def run(page, person, email):
    pcs = {p.replace(" ", "") for p in postcodes(person)}
    hits, nearby = [], []
    for fn, sn in names(person):
        surname = re.sub(r"[^A-Z-]", "", sn.upper())
        n = 1
        while n <= 50:
            resp = page.goto(f"{BASE}/{surname[0]}/{surname[:3]}/{surname}-{n}.html", wait_until="domcontentloaded")
            if not resp or resp.status == 404:
                break
            captcha_check(page)
            for href, text in page.eval_on_selector_all("a[href*='/r.php']", ROWS_JS):
                flat = text.upper().replace(" ", "")
                at_home = any(pc in flat for pc in pcs)
                if not (at_home or "UNITED KINGDOM" in text.upper()):
                    continue
                first = re.search(r"[?&]F=([^&]*)", href).group(1).upper().rstrip(".")
                if first and (first == fn.upper() or (len(first) == 1 and first == fn[0].upper())):
                    hits.append(f"{BASE}{href} ({text[:90]})")
                elif at_home:
                    nearby.append(text[:90])
            if not page.locator(f"a[href$='{surname}-{n + 1}.html']").count():
                break
            n += 1
    if hits:
        raise NeedsHuman("listed; click the bin icon for: " + "; ".join(hits))
    raise NotListed("no UK listing under " + ", ".join(" ".join(n) for n in names(person))
                    + (f"; others at your postcodes: {'; '.join(nearby)}" if nearby else ""))
