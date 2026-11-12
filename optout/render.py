"""Render request templates. Each template's first line is 'Subject: ...'."""
from jinja2 import Environment, FileSystemLoader, StrictUndefined


def env(templates_dir):
    return Environment(loader=FileSystemLoader(str(templates_dir)), undefined=StrictUndefined,
                       keep_trailing_newline=True)


def template_for(broker) -> str:
    return "gdpr_erasure.txt" if broker["jurisdiction"] in ("uk", "eu") else "us_delete.txt"


def render(jenv, name, **ctx) -> tuple[str, str]:
    text = jenv.get_template(name).render(**ctx)
    first, _, body = text.partition("\n")
    if not first.startswith("Subject: "):
        raise ValueError(f"{name}: first line must be 'Subject: ...'")
    return first[len("Subject: "):].strip(), body.lstrip("\n")


def context(person, broker, rid, sender):
    """sender is the person whose mailbox sends; anyone else is acting through them as agent."""
    return {"p": person, "broker": dict(broker), "rid": rid, "sender": sender,
            "agent": person["key"] != sender["key"], "letter": bool(person.get("agent_letter"))}
