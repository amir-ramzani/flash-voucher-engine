from common.codes import new_code, parse_code
from tests.conftest import window
from worker.main import Worker


def create(client, auth, cid, stock=2, **win):
    return client.post(
        "/campaigns",
        headers=auth("admin", admin=True),
        json={"campaignId": cid, "totalStock": stock, **window(**win)},
    )


def drain(settings, r, dynamo):
    w = Worker(settings, r, dynamo)
    w.refresh_streams()
    w.read_new()


def test_create_campaign_requires_admin(client, auth, campaign_id):
    resp = client.post(
        "/campaigns",
        headers=auth("bob"),
        json={"campaignId": campaign_id, "totalStock": 1, **window()},
    )
    assert resp.status_code == 403
    assert client.post("/campaigns", json={}).status_code == 401


def test_create_campaign_is_idempotent(client, auth, campaign_id):
    body = {"campaignId": campaign_id, "totalStock": 5, **window()}
    h = auth("admin", admin=True)
    assert client.post("/campaigns", headers=h, json=body).status_code == 201
    assert client.post("/campaigns", headers=h, json=body).status_code == 200
    assert client.post("/campaigns", headers=h, json={**body, "totalStock": 6}).status_code == 409


def test_full_lifecycle(client, auth, campaign_id, settings, r, dynamo):
    assert create(client, auth, campaign_id, stock=1).status_code == 201
    alice = auth("alice")

    won = client.post(f"/campaigns/{campaign_id}/claim", headers=alice).json()
    assert won["result"] == "WON"
    code = won["voucherCode"]
    assert client.post(f"/campaigns/{campaign_id}/claim", headers=alice).json() == {
        "result": "ALREADY",
        "voucherCode": code,
    }
    assert (
        client.post(f"/campaigns/{campaign_id}/claim", headers=auth("bob")).json()["result"]
        == "SOLD_OUT"
    )

    # Before the worker runs: PENDING from Redis, redeem asks to retry.
    mine = client.get(f"/campaigns/{campaign_id}/my-voucher", headers=alice).json()
    assert mine["status"] == "PENDING" and mine["voucherCode"] == code
    assert client.post(f"/vouchers/{code}/redeem", headers=alice).status_code == 409

    drain(settings, r, dynamo)
    mine = client.get(f"/campaigns/{campaign_id}/my-voucher", headers=alice).json()
    assert mine["status"] == "ISSUED"

    # Someone else can't redeem alice's code.
    assert client.post(f"/vouchers/{code}/redeem", headers=auth("bob")).status_code == 404
    redeemed = client.post(f"/vouchers/{code}/redeem", headers=alice)
    assert redeemed.status_code == 200 and redeemed.json()["status"] == "REDEEMED"
    again = client.post(f"/vouchers/{code}/redeem", headers=alice)
    assert again.status_code == 409


def test_claim_outside_window(client, auth, campaign_id):
    create(client, auth, campaign_id, start_offset_s=3600)
    resp = client.post(f"/campaigns/{campaign_id}/claim", headers=auth("a"))
    assert resp.status_code == 409 and resp.json()["detail"] == "NOT_STARTED"
    assert client.post("/campaigns/missing/claim", headers=auth("a")).status_code == 404


def test_rate_limit(client, auth, campaign_id, settings):
    create(client, auth, campaign_id, stock=1)
    settings.rate_limit_per_sec = 2
    codes = [
        client.post(f"/campaigns/{campaign_id}/claim", headers=auth("spam")).status_code
        for _ in range(4)
    ]
    assert codes == [200, 200, 429, 429]


def test_bad_codes():
    code = new_code("my-sale")
    assert parse_code(code) == "my-sale"
    for bad in ("", "nodot", "my-sale.short", "UPPER.ABCDEFGHJK", "x.ABCDEFGHJ0"):
        try:
            parse_code(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
