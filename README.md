# uk-tenders-mcp

Read-only MCP tools that let an AI agent find this week's UK public-sector tenders across **Find a Tender and Contracts Finder**, score them against a bid profile, and check a company on Companies House. Every result has a source link, a buyer, a value and a deadline.

Built by Ahmed Osman as a public, cleaned-up version of the tender-intelligence work I run in production. It uses public data only.

## What a run looks like

A live run on 4 Nov 2026, last 7 days, `rail_civils` profile:

```
Sources: Find a Tender 445 open tenders, Contracts Finder 41 (cross-listed duplicates merged)
Scored:  471    GO 13    MAYBE 3    NO-GO 455    in about 6 seconds

GO  13  Roads North of the Thames - Bridge Bearings        £500,000    8 days left
GO  13  Samlesbury Enterprise Zone - Foul Water Drainage   £2,000,000  22 days left
GO  12  HS2 Saltley Viaduct gabion wall                    £1,000,000  8 days left
GO  11  Lime Tree Wood landslip repair works               £1,440,000  29 days left
...
```

Version 0.1 read Contracts Finder alone and found 21 tenders in 3 days, none of them a GO. Since the Procurement Act 2023, most notices go to Find a Tender, which carries about ten times as many open tenders in a typical week.

## Tools

| Tool | What it's for | Returns |
| --- | --- | --- |
| `search_tenders` | "What tenders are out for bridge works in London this week?" | Open notices from both sources, soonest deadline first, filtered by keyword and region |
| `score_tenders` | "Which of this week's tenders are worth bidding for?" | GO and MAYBE first with score, reasons, buyer, value, deadline and days left; NO-GO counted but hidden unless asked |
| `list_profiles` | "What can you score against?" | Bid profiles and the value range each one bids in |
| `get_company` | "Is this subcontractor still active? Any insolvency history?" | Status, SIC codes, insolvency history, overdue accounts |

Every result carries `source_url`, and every tender result says which sources answered.

## Design decisions

- **Two sources, one list.** Both sites publish OCDS. Notices on both are merged into one record, and the Find a Tender record is kept because it carries more fields. It is marked `also_listed_on: ["Contracts Finder"]`.
- **Fast despite a slow API.** Find a Tender pages with a cursor, so one long window is about 25 sequential calls a week. The window is split into one-day slices fetched in parallel (4 at a time), which brings a week down from about 40 seconds to about 6. Results are cached for 10 minutes, so a search followed by a score costs one fetch.
- **Only live tenders.** Find a Tender mixes every notice type: awards, plans, cancellations. Only tender and tender-update notices are kept, the newest release per procurement wins, cancelled procurements are dropped, and so are tenders whose deadline has passed.
- **Partial results say so.** If one source fails, the other still answers and the result says which one failed, so the agent can tell the user the list may be incomplete.
- **Task-shaped, read-only and capped.** Four tools that match what people ask. `days` and `limit` are clamped, and descriptions are trimmed, so one call cannot flood the model's context.
- **Grounded by design.** A missing company returns `found: false` rather than a guess. The server tells the agent to cite sources and never fill gaps from memory.
- **Explainable scoring.** Rules, not a black box: keywords, anchor terms, exclusions, works CPV codes, the buyer's own works/services/goods label, value range, SME suitability and time left to bid. Every verdict lists its reasons.

## How scoring decides

| Rule | Effect |
| --- | --- |
| Excluded term (rolling stock, catering, cleaning services…) | NO-GO |
| No anchor term anywhere | NO-GO |
| Anchor only in passing: not in the title, no works CPV, fewer than two keywords | NO-GO |
| Value more than 10x the profile's ceiling | NO-GO: programme-scale, worth a look only as a subcontractor |
| Study, system or supply signals with no works CPV or works category | NO-GO |
| Keywords | +2 each, up to +6 |
| Works CPV (4520, 4522-4524, 45111) / related services CPV (71311, 71312) | +4 / +2 |
| Buyer classes it as works / goods | +2 / -3 |
| Value in range / out of range | +2 / -3 |
| Suitable for SMEs | +1 |
| Closes in under 7 days | -2 |

GO at 7 or more, MAYBE at 4 or more.

## Bugs the live data caught

Each of these came from a real run, became a labelled eval case and a regression test, and cannot come back silently:

1. **"Platform" (3 Nov).** "Candidate Assessment Services" scored MAYBE because "platform" matched an online assessment platform. Now only rail-specific phrases count.
2. **"Station" (4 Nov).** An ambulance station refurbishment and a bus station cleaning contract came through, because bare "station" was an anchor term.
3. **Passing mentions (4 Nov).** A town festival scored GO on "track record" and a note about site drainage. Now an anchor must be in the title, or backed by a works CPV or two keywords, and "track" only counts in rail phrases.
4. **Programme scale (4 Nov).** A £450m rail works framework scored GO on keywords and CPV alone. Notices above 10x the profile ceiling are now NO-GO with a subcontracting note.
5. **Regions (4 Nov).** National frameworks showed as "North East" because buyers tick every region and the first was taken. Now all regions are kept, and labelled "England-wide" or "UK-wide" where that applies.

## Evaluation

`eval/run_eval.py` scores a labelled set and prints a confusion matrix. It fails if accuracy drops below 80%, or if any NO-GO case is scored GO, because a false GO wastes bid effort.

```
Profile: rail_civils   Cases: 33   Accuracy: 94%   False GO: 0

expected \ got         GO   MAYBE   NO-GO
GO                     8       0       0
MAYBE                  1       5       0
NO-GO                  0       1      18
```

How to read it honestly:

- 13 cases were written alongside the first rules. 18 are real Find a Tender notices that I labelled by hand and used while tuning this version, so they show the rules behave as intended, not that they generalise.
- The 2 misses are **held-out**: real notices scored after the rules were frozen, which I disagreed with on review. An asbestos refurbishment of a market scores GO (I'd say MAYBE), and a materials-and-tools supply framework scores MAYBE (I'd say NO-GO). They stay in the file as open misses rather than being tuned away.
- On the fresh 4 Nov run, I agreed with 14 of the 16 tenders the tool flagged GO or MAYBE.

The next step is labels from real bid and pass decisions, with this rules version as the baseline any learned model has to beat.

## Quick start

```bash
git clone https://github.com/atz1man/uk-tenders-mcp
cd uk-tenders-mcp
uv sync --extra dev              # or: pip install -e ".[dev]"
uv run pytest                    # 32 tests, no network needed
uv run python eval/run_eval.py   # scoring evaluation
```

Companies House lookups need a free API key from the [Companies House developer hub](https://developer.company-information.service.gov.uk/). Copy `.env.example` to `.env`, or set `COMPANIES_HOUSE_API_KEY` in your MCP client config. The tender sources need no key.

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

Then ask: *"Score this week's tenders for rail and civils in the North West and show me the GOs with deadlines and links."*

## Project layout

```
src/uk_tenders_mcp/
  server.py     MCP server, the four tools, caching and filters
  sources.py    Find a Tender and Contracts Finder (OCDS), merging, Companies House
  scoring.py    bid profiles and GO / MAYBE / NO-GO rules
  models.py     typed records (Pydantic)
tests/          32 tests, all offline via a mocked HTTP transport
eval/           labelled set (including real notices) and evaluation script
.github/        CI: tests and evaluation on every push
```

## Limits and next steps

- Two profiles (`rail_civils`, `fire_safety`). Profiles are plain data, so adding one is a few lines plus labelled cases.
- Cross-source matching uses title and buyer, so a notice retitled between sites will appear twice.
- Notices with no deadline are kept, and marked with `days_left: null`.
- No write tools, by design. Anything that acts, such as registering interest, would be a separate server, off by default, with human approval.

## Built with

Python 3.11+, the MCP Python SDK (v2), httpx, Pydantic, pytest. Licensed MIT.
