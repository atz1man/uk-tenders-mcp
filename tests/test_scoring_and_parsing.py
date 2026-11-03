from uk_tenders_mcp.models import Tender
from uk_tenders_mcp.scoring import PROFILES, score_tender
from uk_tenders_mcp.sources import normalise_company_number, notice_url, parse_release

RAIL = PROFILES["rail_civils"]


def tender(title, description="", value=200_000, cpv=()):
    return Tender(ocid="x", title=title, description=description, value_gbp=value,
                  cpv_codes=list(cpv), source_url="https://example.org/x")


def test_parse_release_keeps_source_and_fields(cf_payload):
    t = parse_release(cf_payload["releases"][0])
    assert t.buyer == "Example Borough Council"
    assert t.value_gbp == 420000
    assert t.cpv_codes == ["45221111"]
    assert t.deadline == "2026-11-20T12:00:00Z"
    assert t.source_url.endswith("/Notice/1a2b3c4d-0001-4000-8000-000000000001")


def test_parse_release_finds_buyer_in_parties_and_item_cpv(cf_payload):
    t = parse_release(cf_payload["releases"][1])
    assert t.buyer == "Example Housing Trust"
    assert t.cpv_codes == ["45421131"]


def test_missing_fields_stay_empty_not_guessed():
    t = parse_release({"ocid": "no-guid-here", "tender": {}})
    assert t.title == "Untitled notice"
    assert t.buyer is None and t.value_gbp is None and t.cpv_codes == []
    assert "Search" in t.source_url  # falls back to the search endpoint, never invents a page


def test_strong_match_is_go():
    v = score_tender(tender("Railway bridge strengthening", "culvert and drainage works", cpv=["45221111"]), RAIL)
    assert v.verdict == "GO"
    assert any("CPV" in r for r in v.reasons)


def test_exclusion_beats_keywords():
    v = score_tender(tender("Station catering", "catering kiosks at railway stations"), RAIL)
    assert v.verdict == "NO-GO"
    assert "Excluded" in v.reasons[0]


def test_whole_word_matching_ignores_trail_and_guardrail():
    v = score_tender(tender("Trail resurfacing", "new guardrail along the canal"), RAIL)
    assert v.verdict == "NO-GO"


def test_value_out_of_range_is_penalised():
    v = score_tender(tender("Railway bridge works", "", value=20_000_000), RAIL)
    assert any("out of range" in r for r in v.reasons)


def test_company_number_normalisation():
    assert normalise_company_number("1234567") == "01234567"
    assert normalise_company_number(" sc123456 ") == "SC123456"
    assert normalise_company_number("not a number") is None


def test_notice_url_without_guid_falls_back():
    assert notice_url("abc").endswith("/OCDS/Search")


def test_software_platform_is_not_a_station_platform():
    """Regression: a live run scored an online assessment platform as rail work."""
    v = score_tender(tender("Candidate assessment services", "Online assessment platform for recruitment"), RAIL)
    assert v.verdict == "NO-GO"
