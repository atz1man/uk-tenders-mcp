# uk-tenders-mcp

Read-only MCP tools that let an AI agent find UK public-sector tenders, score them against a bid profile, and check a company on Companies House, with a source link on every result.

Built by Ahmed Osman as a public, cleaned-up version of the tender-intelligence work I run in production. It uses public data only.

## What it does

| Tool | What it's for | Returns |
| --- | --- | --- |
| `search_tenders` | "What tenders are out for bridge works this week?" | Open notices from Contracts Finder, newest first, each with `source_url` |
| `list_profiles` | "What can you score against?" | The bid profiles available |
| `score_tenders` | "Which of this week's tenders are worth bidding for?" | GO / MAYBE / NO-GO, a score, the reasons, and `source_url` |
| `get_company` | "Is this subcontractor still active? Any insolvency history?" | Status, SIC codes, insolvency history, overdue accounts, `source_url` |

## Design decisions

- **Task-shaped tools, not table access.** The agent gets four tools that match what people ask, with descriptions that say when to use each. That makes tool choice reliable.
- **Read-only and capped.** Every tool is marked read-only. `days` and `limit` are clamped, so one call cannot flood the model's context.
- **Grounded by design.** Every record carries the URL it came from. A missing company returns `found: false` rather than a guess, and the server's instructions tell the agent to cite sources and never fill gaps from memory.
- **Explainable scoring.** Scoring is rules-based (keywords, required anchor terms, exclusions, CPV codes, value range) and returns its reasons. Rules are cheap, testable and easy to argue with. A learned model can replace the weights later, using real bid and pass decisions as labels.
- **Whole-word matching.** "rail" must not match "trail" or "guardrail". There's a test for it.
- **Resilient calls.** Timeouts, bounded retries with backoff on 429 and 5xx, and 404 treated as "not found", not as an error.

## A real bug the evaluation caught

On the first live run (3 Nov 2026), "Candidate Assessment Services" scored MAYBE for rail and civils. The reason was the keyword "platform", which matched an online assessment platform. I replaced it with rail-specific phrases ("station platform", "railway platform", "platform works"), added the case to the labelled set, and added a regression test so it cannot come back.

## Quick start

```bash
git clone <this repo>
cd uk-tenders-mcp
uv sync --extra dev              # or: pip install -e ".[dev]"
uv run pytest                    # 18 tests, no network needed
uv run python eval/run_eval.py   # scoring evaluation
```

Companies House lookups need a free API key from the [Companies House developer hub](https://developer.company-information.service.gov.uk/). Copy `.env.example` to `.env`, or set `COMPANIES_HOUSE_API_KEY` in your MCP client config.

### Use it in Claude Desktop

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "uk-tenders": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/uk-tenders-mcp", "run", "uk-tenders-mcp"],
      "env": { "COMPANIES_HOUSE_API_KEY": "your-key" }
    }
  }
}
```

Then ask: *"Score this week's tenders for rail and civils and show me the GOs with links."*

## Evaluation

`eval/run_eval.py` scores a labelled set and prints a confusion matrix. It fails if accuracy drops below 80%, or if any NO-GO case is scored GO, because a false GO wastes bid effort.

```
Profile: rail_civils   Cases: 13   Accuracy: 100%   False GO: 0
```

That result should be read honestly. The set is small, and I wrote most of it alongside the rules, so 100% shows the rules behave as designed, not that they generalise. The one case taken from live data (above) is the most valuable row in the file. The next step is to grow the set from real bid and pass decisions.

## Project layout

```
src/uk_tenders_mcp/
  server.py     MCP server and the four tools
  sources.py    Contracts Finder (OCDS) and Companies House clients
  scoring.py    bid profiles and GO / MAYBE / NO-GO rules
  models.py     typed records (Pydantic)
tests/          18 tests, all offline via a mocked HTTP transport
eval/           labelled set and evaluation script
.github/        CI: tests and evaluation on every push
```

## Limits and next steps

- Contracts Finder only. Find a Tender (above-threshold notices) is the next source to add.
- The notice link is built from the GUID inside each OCID.
- Scoring is rules-based. Next: learn weights from logged bid and pass decisions, and keep this rules version as the baseline to beat.
- No write tools, by design. Anything that acts, such as registering interest, would be a separate server, off by default, with human approval.

## Built with

Python 3.11+, the MCP Python SDK (v2), httpx, Pydantic, pytest. Licensed MIT.
