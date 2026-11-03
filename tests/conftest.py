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
def mock_http(monkeypatch, cf_payload):
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
        if host == "api.company-information.service.gov.uk":
            if path.endswith("/01234567"):
                return httpx.Response(200, json=company)
            return httpx.Response(404, json={"errors": [{"error": "company-profile-not-found"}]})
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "_client", client)
    return client
