"""Eyeota GDPR deletion request (HubSpot form, no CAPTCHA)."""
import re

from . import NeedsHuman, captcha_check, names, submit

URL = "https://www.eyeota.com/data-subject-request-form"


def run(page, person, email):
    page.goto(URL, wait_until="domcontentloaded")
    page.wait_for_selector("input[name=firstname]", timeout=30000)
    captcha_check(page)
    agree = page.locator("#truste-consent-required")
    if agree.count() and agree.is_visible():
        agree.click()
    fn, sn = names(person)[0]
    page.fill("input[name=firstname]", fn)
    page.fill("input[name=lastname]", sn)
    page.fill("input[name=email]", email)
    for label in ("delete all records", "not use a hashed version"):
        page.get_by_label(re.compile(label, re.I)).check()
    page.select_option("select[name=regulation]", label="GDPR (Non US Requests)")
    emails = [e for e in person.get("emails") or [] if e.lower() != email.lower()]
    page.fill("textarea[name=message]",
              "Please erase all personal data you hold about me under Article 17 GDPR, including any data "
              "keyed to hashed versions of my email address(es): " + ", ".join([email, *emails]) + ".")
    submit(page.locator("input[type=submit]").first)
    page.wait_for_timeout(4000)
    body = page.inner_text("body")
    if not re.search(r"thank|received|submitted", body, re.I):
        raise NeedsHuman(f"Eyeota did not confirm: {page.url}")
    return f"GDPR erasure + hashed-email opt-out for {', '.join([email, *emails])}"
