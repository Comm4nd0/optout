#!/usr/bin/env python3
"""Load every form-method broker's contact page in headless Chromium and report what's on it:
CAPTCHA/bot walls, input fields, buttons. Used to decide which sites get a form recipe.

usage: survey_forms.py [slug ...] > survey.json
"""
import json
import sys
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
WALLS = {"recaptcha": "g-recaptcha|recaptcha/api", "hcaptcha": "hcaptcha", "turnstile": "cf-turnstile|challenges.cloudflare",
         "cf-challenge": "Just a moment|cf-chl|Attention Required", "datadome": "datadome", "perimeterx": "px-captcha|_pxhd"}

FIELDS_JS = """() => [...document.querySelectorAll('input,select,textarea,button')].filter(e => e.type !== 'hidden')
  .map(e => {const l = e.id && document.querySelector(`label[for="${e.id}"]`);
    return {tag: e.tagName.toLowerCase(), type: e.type || '', name: e.name || '', id: e.id || '',
            ph: e.placeholder || '', label: (l ? l.innerText : (e.getAttribute('aria-label') || '')).trim().slice(0, 60),
            text: e.tagName === 'BUTTON' ? e.innerText.trim().slice(0, 40) : '',
            visible: !!(e.offsetWidth || e.offsetHeight)}})"""


def main(slugs):
    import re
    brokers = []
    for f in sorted((ROOT / "brokers").glob("*.yaml")):
        b = yaml.safe_load(f.read_text())
        if b.get("method") == "form" and b.get("enabled", True) and (not slugs or f.stem in slugs):
            brokers.append((f.stem, b))
    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        for slug, b in brokers:
            page = browser.new_page(user_agent=UA, locale="en-GB")
            r = {"slug": slug, "url": b["contact"], "jurisdiction": b.get("jurisdiction")}
            try:
                resp = page.goto(b["contact"], wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                html = page.content()
                r.update(status=resp.status if resp else None, final_url=page.url, title=page.title()[:80],
                         walls=[k for k, p in WALLS.items() if re.search(p, html, re.I)],
                         iframes=[f.url[:120] for f in page.frames[1:] if f.url and f.url != "about:blank"][:6],
                         fields=[x for x in page.evaluate(FIELDS_JS) if x["visible"]][:40])
            except Exception as e:  # noqa: BLE001
                r["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            finally:
                page.close()
            out.append(r)
            print(f"{slug}: {r.get('status')} walls={r.get('walls')} fields={len(r.get('fields', []))} {r.get('error', '')}",
                  file=sys.stderr)
        browser.close()
    json.dump(out, sys.stdout, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])
