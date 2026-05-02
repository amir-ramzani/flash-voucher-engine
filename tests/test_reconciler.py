from common.keys import campaign_keys
from reconciler.main import reconcile
from worker.main import Worker


def sell(scripts, k, cid, users):
    for u in users:
        scripts.claim(keys=[k.meta, k.stock, k.claims, k.events], args=[u, f"{cid}.X{u}"])


def test_clean_campaign_has_no_drift(dynamo, r, settings, scripts, make_campaign, campaign_id):
    k = make_campaign(campaign_id, stock=3)
    sell(scripts, k, campaign_id, ["a", "b", "c"])
    w = Worker(settings, r, dynamo)
    w.refresh_streams()
    w.read_new()
    assert reconcile(settings, r, dynamo, campaign_id, fix=False).drift == 0


def test_restores_claims_lost_in_redis(dynamo, r, settings, scripts, make_campaign, campaign_id):
    k = make_campaign(campaign_id, stock=3)
    sell(scripts, k, campaign_id, ["a", "b", "c"])
    w = Worker(settings, r, dynamo)
    w.refresh_streams()
    w.read_new()
    # Simulated failover: replica never saw b and c.
    r.hdel(k.claims, "b", "c")
    r.incrby(k.stock, 2)

    rep = reconcile(settings, r, dynamo, campaign_id, fix=True)
    assert sorted(rep.dynamo_only) == ["b", "c"]
    assert r.hget(k.claims, "b") == f"{campaign_id}.Xb"
    assert int(r.get(k.stock)) == 0
    assert reconcile(settings, r, dynamo, campaign_id, fix=False).drift == 0


def test_requeues_claims_missing_from_dynamo(
    dynamo, r, settings, scripts, make_campaign, campaign_id
):
    k = make_campaign(campaign_id, stock=2)
    sell(scripts, k, campaign_id, ["a"])
    r.delete(k.events)  # event lost (e.g. dead-lettered and purged)

    rep = reconcile(settings, r, dynamo, campaign_id, fix=True)
    assert rep.redis_only == ["a"]
    w = Worker(settings, r, dynamo)
    w.refresh_streams()
    w.read_new()
    assert reconcile(settings, r, dynamo, campaign_id, fix=False).drift == 0
    assert campaign_keys(campaign_id)  # sanity
