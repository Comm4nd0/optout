# Broker data: sources and licences

The code in this repository is under the MIT licence (see [LICENSE](LICENSE)). The broker list in
`brokers/` is built from three sources by `tools/import_registries.py`, and one of them carries
its own licence. Each broker file names where it came from in its `source:` field.

| Source | Files | Licence |
| --- | --- | --- |
| Big Ass Data Broker Opt-Out List (BADBOOL) | `tools/badbool.md`, and every `brokers/*.yaml` whose `source:` is `https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List` | CC BY-NC-SA 4.0 |
| California Data Broker Registry | every `brokers/*.yaml` with `category: ca-registry` | Public registry; see below |
| UK list | `tools/uk_curated.yaml` and the broker files built from it | MIT, like the code |

## Big Ass Data Broker Opt-Out List

The Big Ass Data Broker Opt-Out List is by Yael Grauer and lives at
<https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List>. It is licensed under
[Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/).

What this repository does with it:

- `tools/badbool.md` is an unmodified copy of the list's README as it stood after its
  27 September 2026 update.
- Each broker file built from it takes the entry's name, links and contact address, and keeps the
  entry's text as `notes`, with the link markup removed and the text cut to 400 characters.

Those files stay under CC BY-NC-SA 4.0. In short, and the licence text is what counts:

- You may not use them for commercial purposes.
- If you pass them on, changed or not, credit the original list and keep them under the same licence.

To use the rest of this project without those terms, delete `tools/badbool.md` and the broker files
that came from it:

```sh
grep -l 'Big-Ass-Data-Broker-Opt-Out-List' brokers/*.yaml | xargs rm
rm tools/badbool.md
```

## California Data Broker Registry

The registry is published by the California Privacy Protection Agency at
<https://cppa.ca.gov/data_broker_registry/>. The broker files built from it hold what each company
registered: its name, website, contact address and privacy-rights page. The registry export itself
(`tools/ca_registry.csv`) is not kept in this repository.

## UK list

`tools/uk_curated.yaml` was compiled for this project from each company's own published privacy
notice. The page used is in each entry's `source:` field.
