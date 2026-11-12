"""TPS registration: residential, every UK number on one registration, address from the postcode lookup.
TPS emails an activation link; inbox.py opens it."""
import re

from . import NeedsHuman, captcha_check, current_address, names, postcodes, squash, submit, uk_phones

URL = "https://www.tpsonline.org.uk/register"


def _continue(page):
    page.get_by_role("button", name="Continue", exact=True).click()
    page.wait_for_timeout(2000)


def run(page, person, email):
    phones = uk_phones(person)
    if not phones:
        raise NeedsHuman("no UK phone numbers")
    pcs = postcodes(person)
    if not pcs:
        raise NeedsHuman("no UK postcode in addresses")
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    captcha_check(page)
    close = page.get_by_role("button", name="Save and close")  # cookie panel covers the buttons
    if close.count() and close.is_visible():
        close.click()
    page.get_by_text("Residential", exact=True).click()
    _continue(page)
    for i, ph in enumerate(phones):
        if i:
            page.get_by_role("button", name="Add another number").click()
            page.wait_for_timeout(500)
        page.locator("input[type=text]").nth(i).fill(ph)
    _continue(page)
    page.locator("input[type=text]").first.fill(pcs[0])
    page.get_by_role("button", name="Search").click()
    page.wait_for_timeout(3000)
    first = current_address(person).split(",")[0].strip()
    item = page.get_by_text(re.compile(r"^\s*" + re.escape(first), re.I))
    if not item.count():
        raise NeedsHuman(f"TPS address lookup has no '{first}' at {pcs[0]}")
    item.first.click()
    page.wait_for_timeout(500)
    _continue(page)
    page.get_by_text("To stop unwanted telephone marketing calls").click()
    _continue(page)
    page.get_by_text("Search engine", exact=True).click()
    _continue(page)
    page.get_by_placeholder("your email", exact=True).fill(email)
    page.get_by_placeholder("confirm your email").fill(email)
    page.get_by_placeholder("your name").fill(" ".join(names(person)[0]))
    captcha_check(page)
    submit(page.get_by_role("button", name="Continue", exact=True))
    page.wait_for_timeout(4000)
    body = page.inner_text("body")
    if not re.search(r"thank|activation (link|e-?mail) (has been|was) sent|check your (email|inbox)|success", body, re.I):
        raise NeedsHuman(f"TPS did not confirm: {squash(body)}")
    return f"registered {', '.join(phones)} at {first}, {pcs[0]}; activation link will arrive by email"
