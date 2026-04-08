"""Durable path: idempotent persistence, the transactional cap, stale-event recovery."""

from common import dynamo as ddb
from common.keys import CONSUMER_GROUP, REVOKED
from worker.main import Worker
from worker.persister import Outcome, Persister


def event(user: str, cid: str) -> dict:
    return {"user": user, "code": f"{cid}.ABCDEFGHJK", "ts": "1700000000000"}


def persister(dynamo, r, settings):
    return Persister(dynamo, r, settings.campaigns_table, settings.claims_table)


def issued_counter(dynamo, cid) -> int:
    return int(dynamo.campaigns.get_item(Key={"campaignId": cid})["Item"]["issued"])


def test_persist_is_idempotent(dynamo, r, settings, make_campaign, campaign_id):
    make_campaign(campaign_id, stock=5)
    p = persister(dynamo, r, settings)
    assert p.persist(campaign_id, event("alice", campaign_id)) is Outcome.ISSUED
    assert p.persist(campaign_id, event("alice", campaign_id)) in (
        Outcome.ISSUED,
        Outcome.DUPLICATE,
    )
    assert issued_counter(dynamo, campaign_id) == 1
    assert len(ddb.query_all_claims(dynamo, campaign_id)) == 1


def test_cap_revokes_over_issued_claims(dynamo, r, settings, make_campaign, campaign_id):
    """Redis lost writes in a failover and sold 2 vouchers of a stock of 1."""
    k = make_campaign(campaign_id, stock=1)
    p = persister(dynamo, r, settings)
    assert p.persist(campaign_id, event("alice", campaign_id)) is Outcome.ISSUED
    assert p.persist(campaign_id, event("bob", campaign_id)) is Outcome.REVOKED

    rows = {i["userId"]: i["status"] for i in ddb.query_all_claims(dynamo, campaign_id)}
    assert rows == {"alice": ddb.ISSUED, "bob": ddb.REVOKED}
    assert issued_counter(dynamo, campaign_id) == 1
    assert r.hget(k.claims, "bob") == REVOKED


def test_worker_drains_stream_and_acks(dynamo, r, settings, scripts, make_campaign, campaign_id):
    k = make_campaign(campaign_id, stock=3)
    for u in ("a", "b", "c"):
        scripts.claim(keys=[k.meta, k.stock, k.claims, k.events], args=[u, f"{campaign_id}.X{u}"])
    w = Worker(settings, r, dynamo)
    w.refresh_streams()
    assert w.read_new() == 3
    assert issued_counter(dynamo, campaign_id) == 3
    assert r.xpending(k.events, CONSUMER_GROUP)["pending"] == 0


def test_crashed_consumer_events_are_recovered(
    dynamo, r, settings, scripts, make_campaign, campaign_id
):
    k = make_campaign(campaign_id, stock=2)
    for u in ("a", "b"):
        scripts.claim(keys=[k.meta, k.stock, k.claims, k.events], args=[u, f"{campaign_id}.X{u}"])
    # A consumer reads the events, then "crashes" before persisting/acking.
    dead = Worker(settings, r, dynamo)
    dead.consumer = "dead-consumer"
    dead.refresh_streams()
    r.xreadgroup(CONSUMER_GROUP, dead.consumer, {k.events: ">"}, count=10)
    assert r.xpending(k.events, CONSUMER_GROUP)["pending"] == 2

    w = Worker(settings, r, dynamo)  # min_idle=0 in tests
    w.refresh_streams()
    assert w.recover_stale() == 2
    assert issued_counter(dynamo, campaign_id) == 2
    assert r.xpending(k.events, CONSUMER_GROUP)["pending"] == 0


def test_poison_event_is_dead_lettered(dynamo, r, settings, make_campaign, campaign_id):
    k = make_campaign(campaign_id, stock=2)
    r.xadd(k.events, {"user": "x"})  # missing code/ts
    w = Worker(settings, r, dynamo)
    w.refresh_streams()
    w.read_new()
    assert r.xlen(k.dlq) == 1
    assert r.xpending(k.events, CONSUMER_GROUP)["pending"] == 0


def test_max_deliveries_dead_letters(dynamo, r, settings, make_campaign, campaign_id):
    k = make_campaign(campaign_id, stock=2)
    r.xadd(k.events, event("a", campaign_id))
    w = Worker(settings, r, dynamo)
    w.refresh_streams()
    settings.worker_max_deliveries = 2
    w.persister.persist = lambda *_: (_ for _ in ()).throw(RuntimeError("dynamo down"))
    w.read_new()  # delivery 1 fails
    for _ in range(3):  # deliveries 2, 3 -> dead-letter on 3
        w._autoclaim_cursor.clear()
        w.recover_stale()
    assert r.xlen(k.dlq) == 1
    assert r.xpending(k.events, CONSUMER_GROUP)["pending"] == 0
