"""PrivateRecords 'Your Privacy Choices' opt-out (do not sell/share) by email and phone.
Its record-removal path needs a US state and date of birth, so it doesn't fit UK people."""
import re

from . import NeedsHuman, captcha_check, squash, submit

URL = "https://www.privaterecords.net/api/helper/optOutLight/search"


def run(page, person, email):
    emails = person.get("emails") or [email]
    phones = [re.sub(r"[^\d+]", "", p) for p in person.get("phones") or []]
    done = []
    for i in range(max(len(emails), len(phones))):
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_selector("#r-optout", timeout=30000)
        captcha_check(page)
        page.check("#r-optout")
        if i < len(emails):
            page.fill("#oo-email", emails[i])
        if i < len(phones):
            page.fill("#oo-phone", phones[i])
        submit(page.locator("#submit-btn"))
        page.wait_for_timeout(3000)
        body = page.inner_text("body")
        if not re.search(r"thank|received|submitted|opted out|been recorded", body, re.I):
            raise NeedsHuman(f"PrivateRecords did not confirm: {squash(body)}")
        done += [x for x in (emails[i:i + 1] + phones[i:i + 1])]
    return f"do-not-sell opt-out for {', '.join(done)}"
