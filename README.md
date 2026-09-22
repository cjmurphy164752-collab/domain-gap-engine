# Domain Gap Engine

A local-first research workflow for identifying **documented gaps between what
people intend to do and what actually happens** in a hardware or software domain.
It describes evidence and friction; it does not invent products, estimate demand,
or make investment/go/no-go decisions. Zero findings is valid. There is no quota.

Enter a domain to generate a private configuration, discovery queries, and three
importable n8n workflows: collection, report delivery, and a Telegram webhook.
The generator creates these deterministically; it does not use AI to invent APIs
or decide which websites you have permission to scrape.

## What works today

| Input | Use |
|---|---|
| Crossref bibliographic API | Research discovery metadata and DOI links; no article findings inferred |
| Selected GitHub release APIs | Software versions/dates; no issue text, release bodies, or user profiles |
| Optional CPSC product searches | Attributed US recall descriptions, incidents and remedies |
| Authorized local JSONL evidence | Your observations, reproducible task results or licensed evidence exports |

Local Ollama performs verbatim quote extraction, tentative gap interpretation,
and a separate skeptical review of nonempty proposals. Claims cite checked
observation IDs. Reports include intent, attempt, result, consequence, workaround,
alternatives, counterevidence, open questions, and qualitative evidence aspects.
Only substantive notices and authorized local evidence enter that analysis.

**A domain name alone is not evidence.** For a software domain, release metadata
will not establish developer pain. Supply dated, attributable task observations
or rights-reviewed evidence. Hardware recall coverage is also narrow and biased
toward reported safety problems. There is no universal market crawler here.

## Install

Python 3.11–3.13 on Linux or macOS; use Linux inside WSL for Windows. The lock
implementation uses POSIX `fcntl`. n8n and Ollama are separate prerequisites.
Ollama must be reachable at `127.0.0.1:11434` from the Python process; a Windows
Ollama instance is not necessarily WSL's localhost.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
domain-gap init --domain "embedded sensor debugging" --output local/sensors
```

Or run `domain-gap init` and enter your domain when prompted. Optional inputs:

```bash
domain-gap init --domain "desktop CAD collaboration" --output local/cad \
  --repository FreeCAD/FreeCAD --hour 23 --timezone America/New_York
domain-gap init --domain "portable energy storage" --output local/power \
  --product battery --product charger --port 5682
```

These examples configure discovery, not proven friction. Review and edit
`crossref_queries`, `cpsc_products` and `github_repositories` in the generated
`config.local.json`. Anonymous GitHub quotas can limit large/repeated scans.

Inference is **disabled by default**. To enable it, install a suitable model in
Ollama, pass its installed name via `--model`, or set `gap_analysis.enabled` and
`gap_analysis.model` in the private configuration. The reference context/input
budget assumes a Qwen-style byte-level tokenizer; other model families require
their own tokenizer/context validation. A model is not downloaded by this tool.
The configured context is 16,384 tokens, up to 2,500 output tokens, with a
conservative 12,000-byte combined message/schema ceiling. Verify available memory.

## Provide actual evidence

Append one JSON object per line to `evidence.local.jsonl`. Each record must include
`id`, `title`, an HTTPS attribution `url`, `published`, `evidence` (field names to
verbatim strings), and `rights`:

```json
{"id":"fixture-1","title":"SYNTHETIC import observation","url":"https://example.org/synthetic-observation","published":"2026-01-01","evidence":{"intent":"Import my measurement history without manual edits.","result":"The importer rejected the documented export format.","workaround":"I manually renamed columns before importing.","counterevidence":"After renaming, the import completed."},"rights":{"license":"user-authored","local_analysis_allowed":true,"permission_reference":"Synthetic example authored for this documentation"}}
```

This is a **synthetic format example**, not a real finding. Do not mix fixtures
into a production research corpus. Use evidence you authored or for which you
have verified local-analysis rights. Supported declarations are `user-authored`,
`CC0-1.0`, and `CC-BY-4.0`; a label alone does not grant rights. Retain attribution
and private permission records. Do not import personal health data or secrets.
See [SECURITY.md](SECURITY.md).

```bash
domain-gap collect --config local/sensors/config.local.json
```

Inspect the resulting Markdown and JSON in `local/sensors/state/reports/`.
The first manual run does not send a Telegram message.

## Connect n8n and Telegram

1. Run `domain-gap serve --config local/sensors/config.local.json` as a persistent
   local service. Keep it and n8n running; sleeping/offline machines miss schedules.
2. Import `n8n-workflows.local.json` into n8n. All three workflows start inactive.
3. In n8n, create a **Header Auth** credential using the header and randomly
   generated secret from `delivery.local.json`. Assign it to both scheduled HTTP
   nodes and the report webhook. Never paste this file into an issue or commit it.
4. Create your own Telegram credential in n8n, start a private conversation with
   your bot, and set the intended chat ID on the Telegram node. Incoming payloads
   cannot choose a different destination. This project includes no bot or API keys.
5. Confirm `delivery.local.json` matches the production webhook URL. Default n8n
   port is 5678. Defaults assume Python and n8n share the same host network. Docker
   containers have different loopbacks; configure networking deliberately.
6. Publish the Telegram webhook, then manually invoke
   `domain-gap deliver --config local/sensors/config.local.json` after inspecting
   a report. Verify one accepted message and its receipt. A second invocation
   should say `already_delivered`.
7. Publish the collection and delivery schedules when satisfied. Defaults are
   23:00 collection and 07:00 delivery in `America/New_York`, observing DST.

For multiple domains, use different output directories and ports, with one
service process per configuration. Generated webhook paths include a domain slug
and directory-derived suffix. No credentials are embedded in workflow exports.
The templates target n8n 2.x node versions; verify import behavior on your version.

## Reliability and limits

- A persistent SQLite catalog retains pending eligible evidence across missed scans.
- Source failures are disclosed. A 15-minute cooperative collection deadline and
  saved cursor resume deferred searches. An in-flight socket read can overrun its
  budget by up to its bounded timeout; OS/DNS stalls are not a hard process deadline.
- Analysis is bounded to 20 attempted records/20 minutes between records. The last
  record can exceed the time budget by its bounded model calls.
- Overlarge combined model inputs remain pending as `InputBudgetExceeded`. The
  existing per-field 6,000-character cap is disclosed; there is no automatic
  evidence-preserving long-document chunker yet.
- Factual reports are checkpointed before inference. Historical records are labeled
  when not refreshed; a recalled/reported problem may already have been resolved.
- Delivery records an uncertain state before posting. Ambiguous sends require
  manual reconciliation; automatic retry could duplicate a message. Directly
  reposting to n8n bypasses the Python ledger. This is not exactly-once delivery.
- Independent source corroboration, semantic scope filtering, human-scored accuracy
  evaluation and fully automatic source discovery are not implemented.

The included tests are deterministic software checks. They do not establish that
an LLM interpretation is true or that a market is underserved. Human evidence
review remains necessary. No source content is used for model training.

## Development

```bash
pytest -q
ruff check src tests
```

The public edition contains the gap-only collector and analysis pipeline, not the
legacy product-idea generation system or any private deployment data. Apache-2.0;
derived from Research Opportunity Engine by cjmurphy164752-collab.
