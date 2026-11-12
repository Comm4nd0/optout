"""192.com C01 removal: one submission per postcode, all name variants on each.
192.com emails a confirmation link per submission; inbox.py opens it."""
import re

from . import NeedsHuman, captcha_check, names, postcodes

URL = "https://www.192.com/c01/new-request/"


def _solve_sum(page):
    """The 'complete this sum' check: a + b = c with one of the three as an input box."""
    for k in "XYZ":
        row = page.locator(f".humanCheckReport{k}")
        if row.count() and row.is_visible():
            val = {n: row.locator(f".hcReport{n}").inner_text().strip() for n in "XYZ" if n != k}
            v = {n: int(x) for n, x in val.items()}
            ans = v["X"] + v["Y"] if k == "Z" else v["Z"] - v["Y" if k == "X" else "X"]
            row.locator("input").fill(str(ans))
            return
    raise NeedsHuman("192.com human check layout changed")


def run(page, person, email):
    pcs = postcodes(person)
    if not pcs:
        raise NeedsHuman("no UK postcode in addresses")
    nms = names(person)[:5]  # the form takes the main name + 4 more
    done = []
    for pc in pcs:
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        captcha_check(page)
        page.wait_for_selector("#fullName\\.forename", timeout=30000)
        page.fill("#fullName\\.forename", nms[0][0])
        page.fill("#fullName\\.surname", nms[0][1])
        for i, (fn, sn) in enumerate(nms[1:]):
            add = page.get_by_text("Add people for removal")
            if add.count():
                add.first.click()
            page.fill(f'[name="otherFullNames[{i}].forename"]', fn)
            page.fill(f'[name="otherFullNames[{i}].surname"]', sn)
        page.fill("#postcode", pc)
        page.fill("#email", email)
        _solve_sum(page)
        with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
            page.click("#js-ont-c01-submit")
        page.wait_for_timeout(2000)
        body = page.inner_text("body")
        if not re.search(r"thank|confirm|check your (email|inbox)|sent", body, re.I):
            err = re.search(r"(please[^.\n]{0,120}|invalid[^.\n]{0,120}|error[^.\n]{0,120})", body, re.I)
            raise NeedsHuman(f"192.com did not confirm {pc}: {err.group(0) if err else page.url}")
        done.append(pc)
    return f"submitted for {', '.join(done)} with names {', '.join(' '.join(n) for n in nms)}; confirmation links will arrive by email"
