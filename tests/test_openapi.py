"""Keeps docs/openapi.{json,yaml} and the Postman collection in sync with the code."""

import json

from scripts import build_postman, export_openapi


def test_openapi_spec_is_up_to_date():
    assert not export_openapi.is_stale(), "run `make openapi` and commit docs/openapi.*"


def test_spec_documents_every_route():
    spec = export_openapi.build_spec()
    assert set(spec["paths"]) == {
        "/health",
        "/campaigns",
        "/campaigns/{campaign_id}/claim",
        "/campaigns/{campaign_id}/my-voucher",
        "/vouchers/{code}/redeem",
    }
    assert "HTTPBearer" in spec["components"]["securitySchemes"]


def test_postman_collection_is_up_to_date():
    path = build_postman.OUT / "flash-voucher.postman_collection.json"
    assert path.exists() and json.loads(path.read_text()) == build_postman.collection(), (
        "run `make postman`"
    )
