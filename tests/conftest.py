import json
from pathlib import Path

import httpx
import pytest

from uk_tenders_mcp import server

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cf_payload():
    return json.loads((FIXTURES / "contracts_finder_sample.json").read_text())


@pytest.fixture
def fts_payload():
    return json.loads((FIXTURES / "find_a_tender_sample.json").read_text())


@pytest.fixture
def mock_http(monkeypatch, cf_payload, fts_payload):
    """Route all HTTP to canned responses, so tests never touch the network."""
    company = {
        "company_number": "01234567",
        "company_name": "EXAMPLE CIVILS LIMITED",
        "company_status": "active",
        "date_of_creation": "2010-04-01",
        "registered_office_address": {"locality": "Basildon", "postal_code": "SS13 1AA"},
        "sic_codes": ["42990"],
        "has_insolvency_history": False,
        "accounts": {"overdue": True},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "www.contractsfinder.service.gov.uk":
            return httpx.Response(200, json=cf_payload)
        if host == "www.find-tender.service.gov.uk":
            if state.get("fts_down"):
                return httpx.Response(403)
            state["fts_calls"] = state.get("fts_calls", 0) + 1
            if state.get("fts_flaky") and state["fts_calls"] % 2 == 0:
                return httpx.Response(403)  # every other day-slice fails
            return httpx.Response(200, json=fts_payload)  # every day-slice gets the same page; dedupe handles it
        if host == "api.company-information.service.gov.uk":
            if path.endswith("/01234567"):
                return httpx.Response(200, json=company)
            return httpx.Response(404, json={"errors": [{"error": "company-profile-not-found"}]})
        return httpx.Response(500)

    state: dict[str, bool] = {}
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.state = state  # tests set client.state["fts_down"] = True
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.setattr(server, "_cache", {})
    return client
