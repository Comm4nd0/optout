"""Keyword classifier for broker replies. Anything it can't place goes to needs_review."""
import re

# order matters: first match wins
RULES = [
    ("autoreply", [r"out of (the )?office", r"automatic reply", r"auto-?reply", r"i am currently away"]),
    ("needs_id", [r"proof of (identity|address)", r"copy of (your|a) (passport|driv)", r"photo(graphic)? id",
                  r"government[- ]issued", r"identity document", r"driv(er'?s|ing) licen[cs]e",
                  r"verify your identity", r"confirm your identity"]),
    ("needs_verification", [r"confirm your (email|request|opt)", r"verify your (email|request)",
                            r"click (on )?(the|this) link", r"click here to (confirm|verify|complete)",
                            r"verification link", r"confirmation link", r"please confirm"]),
    # "use our form instead": a human has to go and fill it in
    ("needs_review", [r"(use|visit|submit (it |your request )?(via|through|using)) our (online )?"
                      r"(\w+ ){0,3}(form|portal)"]),
    ("no_data", [r"(do|does) not (hold|have|appear to have|retain) any (personal )?(data|information|records)",
                 r"no (personal )?(data|information|records) (about|relating|for|on|associated)",
                 r"(unable|not able) to (locate|find|identify) any", r"could not (locate|find) any",
                 r"no records? (were|was) found", r"not in our (database|records|systems?)",
                 r"no matching records"]),
    ("confirmed", [r"(has|have) been (deleted|erased|removed|suppressed|opted[- ]out)",
                   r"we have (now )?(deleted|erased|removed|suppressed|opted)",
                   r"added (you |your details |your data )?to our suppression",
                   r"(request|deletion|erasure|opt[- ]out) (is|has been|was) (now )?(complete|completed|processed|actioned|fulfilled)",
                   r"successfully (deleted|removed|processed|opted)",
                   r"(deletion|erasure|opt[- ]out|request) successfully (completed|processed|actioned)"]),
    ("refused", [r"(unable|not able) to (process|comply|action|fulfil)", r"cannot (process|comply|action)",
                 r"only (available|applies) to (california|residents)", r"not a (california )?resident",
                 r"we decline", r"(is|are) not subject to", r"does not apply to you"]),
    ("acknowledged", [r"(received|receipt of) your (request|email|message)", r"we will (respond|get back|be in touch|review)",
                      r"within (30|45|one|1) (days|month)", r"(ticket|case|reference) (number|#|no)",
                      r"thank you for (contacting|your (request|email|message))", r"being (processed|reviewed)",
                      r"request (has been |was )?(successfully )?(submitted|logged|received)"]),
]
COMPILED = [(label, [re.compile(p, re.I) for p in pats]) for label, pats in RULES]
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def classify(text: str) -> tuple[str, str]:
    """Return (label, matched phrase)."""
    for label, pats in COMPILED:
        for p in pats:
            m = p.search(text)
            if m:
                return label, m.group(0)
    return "needs_review", ""


def urls(text: str) -> list[str]:
    return [u.rstrip(".,;") for u in URL_RE.findall(text)]
