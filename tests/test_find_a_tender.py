import asyncio
from datetime import datetime, timezone

from uk_tenders_mcp import server
from uk_tenders_mcp.models import Tender
from uk_tenders_mcp.scoring import PROFILES, score_tender
from uk_tenders_mcp.sources import FIND_A_TENDER, parse_release, region_name

RAIL = PROFILES["rail_civils"]


def run(coro):
    return asyncio.run(coro)


def test_parse_find_a_tender_release(fts_payload):
    t = parse_release(fts_payload["releases"][0], FIND_A_TENDER)
    assert t.cpv_codes == ["45232451"]  # read from items[].additionalClassifications
    assert t.value_gbp == 750000  # net amount, not gross
    assert t.region == "South East" and t.sme_suitable is True and t.category == "works"
    assert t.source_url == "https://www.find-tender.service.gov.uk/Notice/000101-2026"


def test_region_names():
    assert region_name("UKI32") == "London"
    assert region_name("Northern Ireland") == "Northern Ireland"
    assert region_name(None) is None


def test_search_merges_sources_and_drops_dead_notices(mock_http):
    result = run(server.search_tenders(limit=50))
    titles = [t["title"] for t in result["tenders"]]
    # newest update wins, awards / cancelled / closed are gone, the cross-listed bridge appears once
    assert titles.count("Railway bridge parapet strengthening and culvert repairs") == 1
    assert "Awarded: resurfacing works" not in titles
    assert "Railway embankment stabilisation" not in titles
    assert "Drainage repairs (closed)" not in titles
    bridge = next(t for t in result["tenders"] if t["title"].startswith("Railway bridge"))
    assert bridge["source"] == "Find a Tender" and bridge["also_listed_on"] == ["Contracts Finder"]
    foul = next(t for t in result["tenders"] if t["title"].startswith("Foul water"))
    assert foul["deadline"] == "2099-12-08T12:00:00Z" and foul["days_left"] > 0
    assert set(result["sources"]) == {"Find a Tender", "Contracts Finder"}


def test_region_filter(mock_http):
    result = run(server.search_tenders(regions=["london"]))
    assert [t["region"] for t in result["tenders"]] == ["London"]


def test_one_source_down_still_answers_and_says_so(mock_http):
    mock_http.state["fts_down"] = True
    result = run(server.search_tenders())
    assert result["found"]
    assert result["sources"]["Find a Tender"]["ok"] is False
    assert result["sources"]["Contracts Finder"]["ok"] is True


def test_score_hides_no_go_by_default_and_carries_bid_fields(mock_http):
    result = run(server.score_tenders())
    assert all(r["verdict"] != "NO-GO" for r in result["results"])
    assert result["summary"]["NO-GO"] >= 1
    top = result["results"][0]
    assert top["verdict"] == "GO" and top["buyer"] and top["deadline"] and top["days_left"] is not None
    full = run(server.score_tenders(include_no_go=True))
    assert len(full["results"]) == full["scored"]


def test_software_for_a_rail_operator_is_no_go(fts_payload):
    t = parse_release(fts_payload["releases"][-1], FIND_A_TENDER)
    assert score_tender(t, RAIL).verdict == "NO-GO"


def test_ambulance_station_is_not_rail_work():
    """Regression: bare 'station' used to anchor the rail profile."""
    t = Tender(ocid="x", title="Refurbishment of Stansted Ambulance Station", category="works",
               value_gbp=123000, source_url="https://example.org")
    assert score_tender(t, RAIL).verdict == "NO-GO"


def test_tight_deadline_costs_points():
    now = datetime(2026, 11, 4, tzinfo=timezone.utc)
    base = dict(ocid="x", title="Railway culvert repairs", description="drainage works", category="works",
                value_gbp=300000, cpv_codes=["45232451"], source_url="https://example.org")
    roomy = score_tender(Tender(**base, deadline="2026-12-04T12:00:00Z"), RAIL, now)
    tight = score_tender(Tender(**base, deadline="2026-11-06T12:00:00Z"), RAIL, now)
    assert tight.score == roomy.score - 2 and tight.days_left == 2
    assert any("little time to bid" in r for r in tight.reasons)


def test_programme_scale_framework_is_no_go_even_with_strong_keywords():
    """Regression: a £450m rail works framework scored GO on keywords and CPV alone."""
    t = Tender(ocid="x", title="Railway programme support works framework", description="rail civils",
               category="works", value_gbp=450_000_000, cpv_codes=["45200000"], source_url="https://example.org")
    v = score_tender(t, RAIL)
    assert v.verdict == "NO-GO" and "subcontractor" in v.reasons[0]


def test_buyer_ticking_every_english_region_is_england_wide():
    """Regression: the first ticked region (North East) was shown for national notices."""
    from uk_tenders_mcp.sources import NUTS1
    english = [c for c in NUTS1 if c not in ("UKL", "UKM", "UKN")]
    release = {"ocid": "o", "id": "1-2026", "tender": {"title": "Grounds maintenance",
               "items": [{"deliveryAddresses": [{"region": c} for c in english]}]}}
    t = parse_release(release, FIND_A_TENDER)
    assert t.region == "England-wide" and "London" in t.regions


def test_passing_mention_in_description_is_not_enough():
    """Regression: a town festival scored GO on 'track record' and a drainage note in the description."""
    t = Tender(ocid="x", title="Filton Festival 2027", category="services", value_gbp=28000, sme_suitable=True,
               description="Event management. Bidders must show a track record. Site drainage is the council's.",
               source_url="https://example.org")
    v = score_tender(t, RAIL)
    assert v.verdict == "NO-GO" and "in passing" in v.reasons[0]


def test_feasibility_study_without_works_signal_is_no_go():
    t = Tender(ocid="x", title="Rail Freight Feasibility Study", category="services", value_gbp=72000,
               cpv_codes=["71311200"], description="Assess rail heads and sidings.", source_url="https://example.org")
    v = score_tender(t, RAIL)
    assert v.verdict == "NO-GO" and any("study, system or supply" in r for r in v.reasons)


def test_keyword_search_is_whole_word(mock_http):
    result = run(server.search_tenders(keywords=["rail"], limit=50))
    assert all("trail" not in t["title"].lower() for t in result["tenders"])
