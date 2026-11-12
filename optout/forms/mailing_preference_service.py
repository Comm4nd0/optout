"""MPS registration for the current address, every name variant as an individual (max 5).
MPS emails an activation link; inbox.py opens it."""
import re

from . import NeedsHuman, captcha_check, current_address, names, postcodes, squash, submit

URL = "https://www.mpsonline.org.uk/register"


def _next(page):
    page.get_by_role("button", name="Next", exact=True).click()
    page.wait_for_timeout(2500)


def run(page, person, email):
    addr = current_address(person)
    pcs = postcodes({"addresses": [{"text": addr}]})
    if not pcs:
        raise NeedsHuman("current address has no UK postcode")
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    captcha_check(page)
    page.get_by_text("I wish to register to the Mailing Preference Service").click()
    _next(page)
    page.locator("input[name=mps_reg_type]").first.evaluate("e => e.click()")  # my address details
    _next(page)
    nms = names(person)[:5]
    for fn, sn in nms:
        boxes = page.locator("input:not([type=radio]):not([type=checkbox])")  # no type attribute on these
        boxes.nth(1).fill(fn)  # 0 is the optional title
        boxes.nth(2).fill(sn)
        page.get_by_role("button", name="Add individual").click()
        page.wait_for_timeout(800)
    page.get_by_placeholder("e.g. S75 2SD").fill(pcs[0])
    page.get_by_role("button", name="Search").click()
    page.wait_for_timeout(3000)
    first = addr.split(",")[0].strip().lower()
    sel = page.locator("select").first
    opts = [o for o in sel.locator("option").all_inner_texts() if o.lower().startswith(first)]
    if not opts:
        raise NeedsHuman(f"MPS address lookup has no '{first}' at {pcs[0]}")
    sel.select_option(label=opts[0])
    _next(page)
    page.locator("input[name=mps_hear_about]").nth(5).evaluate("e => e.click()")  # search engine
    _next(page)
    page.locator("input[name=mps_register_reason]").first.evaluate("e => e.click()")  # stop unwanted mail
    _next(page)
    body = page.inner_text("body")
    missing = [f"{fn} {sn}" for fn, sn in nms if f"{fn} {sn}".lower() not in body.lower()]
    if missing:
        raise NeedsHuman(f"MPS summary is missing {', '.join(missing)}")
    page.locator("input[type=email]").nth(0).fill(email)
    page.locator("input[type=email]").nth(1).fill(email)
    box = page.locator("input[type=checkbox]")
    if box.count():
        box.first.evaluate("e => e.checked || e.click()")
    else:
        page.get_by_text("Please confirm you have the authority").click()
    captcha_check(page)
    submit(page.get_by_role("button", name="Send", exact=True))
    page.wait_for_timeout(4000)
    body = page.inner_text("body")
    if not re.search(r"thank|activation e-?mail|check your (e-?mail|inbox)|success", body, re.I):
        raise NeedsHuman(f"MPS did not confirm: {squash(body)}")
    return f"registered {', '.join(' '.join(n) for n in nms)} at {opts[0]}; activation link will arrive by email"
