# optout

Self-hosted tool that asks data brokers to delete your personal data, and keeps asking until they do.

It emails an erasure request to each broker on its list from your own Gmail account, reads the
replies, sends a reminder when a deadline passes, drafts a regulator complaint when the reminder is
ignored, and asks again some months later because brokers re-acquire data. Brokers that only accept
a web form are either filled in automatically or queued on a small dashboard for you to do by hand.

It handles a household: one person's mailbox sends for everyone, acting as authorised agent for the
others.

**Before you use it**

- It is written for people living in the UK. The letters cite the UK GDPR and say you live in the
  UK, and the escalation step is a complaint to the ICO. Elsewhere you would need to rewrite the
  [letter templates](#letters).
- It sends real legal requests in your name, with your name, addresses, email addresses and phone
  numbers in them, to several hundred companies. Read the letters first (`optout preview`).
- It is not legal advice.
- It only works with Gmail.

## How it works

Each person and broker pair is one request, which moves through these states:

| State | Meaning |
| --- | --- |
| Queued | Waiting for its turn within the daily sending limit |
| Sent | Emailed; a reply is due in 30 days |
| Acknowledged | The broker says it is working on it |
| Click link / ID requested / Review reply / Refused | The reply needs you; it is on the dashboard |
| Reminder sent | No answer in 30 days, so a reminder went out as a reply to the original |
| Overdue | No answer to the reminder after another 30 days. For UK and EU brokers the dashboard shows a complaint draft for the ICO |
| Removed / No data held / Skipped | Closed. A new round opens after the broker's recheck period (3 to 6 months for most) |
| Form queued | A web form that a [recipe](#web-forms) will submit |
| Do by hand | A web form with no recipe, or a step only you can do: the dashboard shows the link and the details to paste |

Three jobs run on timers:

- **cycle** (hourly) creates requests, sends queued ones and sends reminders. Sending is spread
  across the day: 40 emails a day between 09:00 and 21:00 by default, so the first pass through the
  list takes a few weeks.
- **inbox** (every 15 minutes) reads Gmail over IMAP and matches replies to requests by the
  `[optout:123]` tag in the subject, then by thread, then by sender domain, then by the reference
  quoted in the body. A keyword classifier decides what the reply means; anything it cannot place
  goes to "Review reply". Matched mail is filed under the Gmail label `optout` and taken out of
  your inbox, and mail that needs you also gets `optout/needs-you`. Confirmation links are opened
  automatically, but only when they point at the broker's domain or the domain the reply came from.
- **forms** (daily) submits web forms for the brokers that have a recipe.

The dashboard shows how far along everything is, lists what needs you grouped by what you have to
do, and keeps the full history of each request with the emails in both directions.

## Keeping replies out of your inbox

Several hundred companies answering, most of them more than once, is a lot of mail. Two things keep
it away from you:

- Every request asks for replies at `you+optout@gmail.com` (a `Reply-To` header; the request still
  comes from your plain address). Gmail delivers that to the same mailbox, but the address marks
  the mail as an answer to one of these requests, so the inbox job files it whoever sent it.
- A pair of Gmail filters makes those answers skip the inbox altogether, so nothing sits there for
  up to 15 minutes and your phone stays quiet. They catch mail sent to the `+optout` address and
  mail with `optout` in the subject, put it under the label `optout`, and keep it out of spam. The
  inbox job reads it there exactly as before.

Gmail filters cannot be created with an app password, so importing them is a one-off job for you:

1. Get the filter file: **Mail → Download the Gmail filters** on the dashboard, or
   `optout gmail-filter > optout-gmail-filters.xml`.
2. In Gmail on a computer open **Settings → See all settings → Filters and blocked addresses**.
3. Choose **Import filters**, pick the file, then **Open file** and **Create filters**. Tick
   **Apply new filters to existing email** if answers are already sitting in the inbox, or run
   `optout refile`.

What still reaches the inbox first: bounce messages, and the occasional answer that ignores the
reply address and changes the subject. The inbox job clears those within 15 minutes if it can tell
they belong to a request or come from a broker. Set `OPTOUT_REPLY_TAG=` (empty) to go back to
plain replies; the subject filter and the 15-minute filing still work.

## The broker list

641 brokers ship in `brokers/`, one YAML file each:

| Source | Brokers | Notes |
| --- | --- | --- |
| [California Data Broker Registry](https://cppa.ca.gov/data_broker_registry/) | 557 | Registered US brokers, all contacted by email |
| [Big Ass Data Broker Opt-Out List](https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List) | 44 | US people-search sites, mostly web forms |
| UK list in `tools/uk_curated.yaml` | 40 | UK marketing-data and people-search companies, plus the Mailing and Telephone Preference Services and the open electoral register opt-out |

Of the 641, 579 are contacted by email, 55 want a web form and 7 are manual steps. The UK list is
sent first.

A broker file looks like this:

```yaml
name: Acxiom UK
domain: acxiom.co.uk
jurisdiction: uk          # uk, eu or us: picks the letter
method: email             # email, form or manual
contact: ukconsumerservices@acxiom.com
alt_contact: ukenquiries@acxiom.com
needs_id: false
recheck_months: 6
priority: 1               # 1 is sent first
notes: InfoBase UK consumer data.
source: https://www.acxiom.co.uk/privacy/
generated: true
enabled: true
```

To skip a broker or correct its details, edit its file and set `generated: false` so the importer
leaves it alone. To add one, add a file; the file name is its identifier.

`tools/import_registries.py` rebuilds the generated files from the three sources. It needs the
California registry export saved as `tools/ca_registry.csv`, which is not kept in this repository.

## Try it on your own machine

Nothing is sent in this mode; emails are written to files so you can read them. You need Python
3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/Comm4nd0/optout
cd optout
uv sync
cp deploy/people.yaml.example people.yaml    # fill in your details and set enabled: true
export OPTOUT_PEOPLE=$PWD/people.yaml OPTOUT_STATE=$PWD/state

uv run optout sync                 # load the brokers, create the requests
uv run optout preview --limit 3    # write three requests to state/outbox/*.eml
uv run optout status
uv run optout web                  # dashboard on http://localhost:8080
```

Without uv, `pip install -e .` in a virtual environment does the same job as `uv sync`.

## Install on a server

The installer is for Debian or Ubuntu with systemd, and has to be run as root from a checkout at
`/opt/optout`:

```sh
git clone https://github.com/Comm4nd0/optout /opt/optout
/opt/optout/deploy/install.sh
```

It installs uv if it is missing, builds a virtual environment, downloads Chromium for the form
recipes, creates `/etc/optout` and `/var/lib/optout`, and installs and starts the systemd timers and
the dashboard. It starts in dry-run mode with nobody enabled, so nothing goes anywhere yet.

Then:

1. Edit `/etc/optout/people.yaml` ([format below](#peopleyaml)) and set `enabled: true`.
2. Turn on 2-Step Verification for the Gmail account and create an
   [app password](https://myaccount.google.com/apppasswords) for it.
3. Run `optout-secrets`. It asks for the Gmail address, the app password and a password for the
   dashboard. Until the dashboard has a password it only answers on the server itself; setting
   one opens it to the network.
4. Check the result: `optout-run sync`, then `optout-run preview` and read a few of the emails in
   `/var/lib/optout/outbox/`. The dashboard is on port 8080; the user name is `admin`.
5. Import the [Gmail filters](#keeping-replies-out-of-your-inbox) so the answers skip your inbox.
6. When you are happy, set `OPTOUT_DRY_RUN=false` in `/etc/optout/optout.env` and run
   `systemctl restart optout-web`.

`optout-run <command>` runs any [command](#commands) with the service's settings.

To update: `git -C /opt/optout pull && /opt/optout/deploy/install.sh`. Your files in `/etc/optout`
are left alone.

`deploy.sh` at the top of the repository is a shortcut for one particular setup: it copies the
checkout into a Proxmox LXC container over ssh and runs the installer there.

The installer also drops a [Checkmk](https://checkmk.com/) local check in
`/usr/lib/check_mk_agent/local/`. It does nothing unless you run a Checkmk agent.

## Configuration

### people.yaml

```yaml
sender: alex             # whose Gmail sends the requests; acts as agent for everyone else
people:
  alex:
    full_name: Alex Example
    other_names: []      # maiden names, nicknames, common misspellings
    emails: [you@example.com]
    phones: ["+44 7700 900000"]
    addresses:
      - {current: true, text: "1 High Street, Town, AB1 2CD"}
      - {text: "Previous address, AB3 4EF"}
    dob: ""              # never put in emails; shown on the dashboard for forms that insist
  sam:
    full_name: Sam Example
    addresses:
      - {current: true, text: "1 High Street, Town, AB1 2CD"}
    agent_letter: /etc/optout/auth/sam.pdf   # their signed authorisation, attached to their requests
```

Names, addresses, email addresses and phone numbers go into the letters, so brokers can find the
records. Leave out anything you would rather they did not see. Requests are stored under the key (`alex`,
`sam`), so do not rename a key once requests exist. `enabled: false` on a person leaves them out.

`agent_letter` is optional. If you set it, the PDF has to exist; if you leave it out, the letters
say the authorisation is available on request.

### Settings

Settings are environment variables. On a server they live in `/etc/optout/optout.env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `OPTOUT_DRY_RUN` | `true` | While true, no email is sent and no form is submitted |
| `GMAIL_USER` | | Gmail address that sends the requests and receives the replies |
| `GMAIL_APP_PASSWORD` | | App password for that account |
| `OPTOUT_REPLY_TAG` | `optout` | Replies are asked for at `you+TAG@gmail.com`. Empty means your plain address |
| `OPTOUT_DAILY_LIMIT` | `40` | Emails per 24 hours, reminders included |
| `OPTOUT_SEND_HOURS` | `9-21` | Sending window, in the machine's local time |
| `OPTOUT_REPLY_DAYS` | `30` | Days to wait before the reminder |
| `OPTOUT_ESCALATE_DAYS` | `30` | Days after the reminder before a request is overdue |
| `OPTOUT_WEB_USER` | `admin` | Dashboard user name |
| `OPTOUT_WEB_PASSWORD` | | Dashboard password. If empty there is no login, and the dashboard only listens on `127.0.0.1` |
| `OPTOUT_WEB_HOST` | `0.0.0.0` | Address the dashboard listens on once it has a password |
| `OPTOUT_WEB_PORT` | `8080` | Dashboard port |
| `OPTOUT_PEOPLE` | `/etc/optout/people.yaml` | The people file |
| `OPTOUT_STATE` | `/var/lib/optout` | Where the database and the dry-run outbox live |
| `OPTOUT_DB` | `$OPTOUT_STATE/optout.db` | SQLite database |
| `OPTOUT_OUTBOX` | `$OPTOUT_STATE/outbox` | Where dry-run emails are written |
| `OPTOUT_BROKERS` | bundled `brokers/` | Broker directory |
| `OPTOUT_TEMPLATES` | bundled `templates/` | Letter templates |

## Commands

| Command | What it does |
| --- | --- |
| `optout sync` | Load the broker files and create any missing requests |
| `optout preview [--limit N]` | Write the next queued requests to `.eml` files; changes nothing |
| `optout status` | Count requests by state, and emails sent in the last 24 hours |
| `optout cycle` | One round of creating, sending and reminding (the hourly job) |
| `optout inbox [--days N]` | Read and act on replies from the last N days (default 7) |
| `optout refile [--days N]` | Label and archive broker mail already sitting in the inbox |
| `optout gmail-filter` | Print the Gmail filter file that makes replies skip the inbox |
| `optout forms [--limit N] [--only SLUG] [--dry-run]` | Submit queued web forms. `--dry-run` fills each form and stops before the submit button |
| `optout web` | Run the dashboard |
| `optout check` | Print a Checkmk local-check line: stale or failing jobs, and how much needs you |

## Web forms

`optout forms` drives a headless Chromium with [Playwright](https://playwright.dev/python/). The
installer sets this up; in a checkout, run `uv sync --extra forms` and
`uv run playwright install chromium`.

Recipes live in `optout/forms/`, one module per broker, named after the broker file:

| Recipe | What it does |
| --- | --- |
| `192_com` | 192.com removal request, one per postcode |
| `mailing_preference_service` | Registers your current address with the MPS |
| `telephone_preference_service` | Registers your UK numbers with the TPS |
| `eyeota_dun_bradstreet` | Eyeota GDPR deletion form |
| `privaterecords` | PrivateRecords do-not-sell opt-out |
| `locatefamily` | Looks for a listing; if there is one, it hands the removal to you |

A recipe stops and hands the request to you when it meets a CAPTCHA or a page that has changed.
To add one, write a module with `run(page, person, email)` that returns a short summary, raises
`NeedsHuman` when it cannot finish, and sends through `submit()` so `--dry-run` works.
`tools/survey_forms.py` reports the fields and bot checks on each broker's form page, which helps
decide which forms are worth a recipe.

## Letters

The letters are Jinja templates in `templates/`, and the first line of each is the subject:

- `gdpr_erasure.txt` goes to UK and EU brokers: erasure under Article 17 and an objection to
  direct marketing under Article 21.
- `us_delete.txt` goes to US brokers. It relies on the UK GDPR where that applies and otherwise
  asks the broker to treat it as a California-style deletion and do-not-sell request.
- `chase.txt` is the reminder.
- `ico_complaint.txt` is the complaint draft shown on the dashboard.

To change them, copy the directory, edit, and point `OPTOUT_TEMPLATES` at your copy. Keep
`[optout:{{ rid }}]` in the subject: replies are matched on it.

## Security and privacy

- `people.yaml`, the database and the outbox hold personal details, and `optout.env` holds the
  Gmail app password. The installer makes their directories readable by root only. Do not commit
  them; the `.gitignore` covers the usual names.
- A Gmail app password gives full access to the mailbox. The inbox job reads the headers of all
  recent mail to find replies, and reads the body only of mail from a broker or a privacy-request
  service, or sent to the `+optout` reply address.
- The dashboard shows those personal details and can change request states. Without a password
  it only answers on the machine it runs on. With one it listens on every interface by default
  and speaks plain HTTP, so keep it on a network you trust and do not expose it to the internet.
- Brokers sometimes answer by asking for identity documents. The tool never sends any; those
  requests wait on the dashboard for you to decide.

## Development

```sh
uv sync --extra dev
uv run pytest
```

The tests use no network and send nothing.

## Licence

The code is under the [MIT licence](LICENSE).

The broker list is compiled from several sources. The 44 entries taken from the Big Ass Data Broker
Opt-Out List by Yael Grauer, and the copy of that list in `tools/badbool.md`, are under
[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/), which does not allow
commercial use. [DATA-LICENSE.md](DATA-LICENSE.md) has the details and says how to remove them.
