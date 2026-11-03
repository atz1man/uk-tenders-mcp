import asyncio

from uk_tenders_mcp import server


def run(coro):
    return asyncio.run(coro)


def test_tools_are_registered():
    tools = run(server.server.list_tools())
    names = {t.name for t in tools}
    assert {"search_tenders", "score_tenders", "get_company", "list_profiles"} <= names
    for t in tools:
        assert t.annotations is not None and t.annotations.read_only_hint is True


def test_search_filters_by_keyword_and_cites_source(mock_http):
    result = run(server.search_tenders(keywords=["fire door"]))
    assert result["found"] and result["count"] == 1
    assert result["tenders"][0]["source_url"].startswith("https://www.contractsfinder.service.gov.uk/Notice/")


def test_limits_are_clamped(mock_http):
    result = run(server.search_tenders(days=999, limit=0))
    assert result["returned"] == 1


def test_score_orders_go_first(mock_http):
    result = run(server.score_tenders(profile="rail_civils"))
    assert result["results"][0]["verdict"] == "GO"
    assert result["summary"]["GO"] == 1


def test_unknown_profile_lists_valid_ones(mock_http):
    result = run(server.score_tenders(profile="nope"))
    assert not result["found"] and "rail_civils" in result["profiles"]


def test_company_found(mock_http, monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    result = run(server.get_company("1234567"))
    assert result["found"]
    assert result["company"]["accounts_overdue"] is True
    assert result["company"]["source_url"].endswith("/company/01234567")


def test_company_not_found_says_so(mock_http, monkeypatch):
    monkeypatch.setenv("COMPANIES_HOUSE_API_KEY", "test-key")
    result = run(server.get_company("99999999"))
    assert result["found"] is False and "No company" in result["note"]


def test_company_without_key_or_bad_number(mock_http, monkeypatch):
    monkeypatch.delenv("COMPANIES_HOUSE_API_KEY", raising=False)
    assert "not set" in run(server.get_company("01234567"))["error"]
    assert "8 letters" in run(server.get_company("??"))["error"]
