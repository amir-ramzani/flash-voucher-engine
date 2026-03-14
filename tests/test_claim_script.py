"""The Lua claim script: the single place where over-redemption must be impossible."""

from concurrent.futures import ThreadPoolExecutor

from common.codes import new_code
from common.keys import campaign_keys
from tests.conftest import now_ms


def claim(scripts, cid, user):
    k = campaign_keys(cid)
    return scripts.claim(keys=[k.meta, k.stock, k.claims, k.events], args=[user, new_code(cid)])


def test_exactly_n_winners_under_contention(scripts, r, make_campaign, campaign_id):
    k = make_campaign(campaign_id, stock=25)
    users = [f"u{i}" for i in range(400)] * 2  # every user taps twice
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda u: (u, claim(scripts, campaign_id, u)), users))

    won = [(u, v) for u, (status, v) in results if status == "WON"]
    assert len(won) == 25
    assert len({u for u, _ in won}) == 25
    assert int(r.get(k.stock)) == 0
    assert r.hlen(k.claims) == 25
    assert r.xlen(k.events) == 25
    # Every response for a winner carries that winner's single code.
    codes = dict(won)
    for u, (status, v) in results:
        if u in codes:
            assert v == codes[u] and status in ("WON", "ALREADY")
        else:
            assert status == "SOLD_OUT"


def test_duplicate_tap_returns_same_code(scripts, make_campaign, campaign_id):
    make_campaign(campaign_id, stock=5)
    s1, code = claim(scripts, campaign_id, "alice")
    s2, again = claim(scripts, campaign_id, "alice")
    assert (s1, s2) == ("WON", "ALREADY") and again == code


def test_winner_keeps_code_after_sellout(scripts, make_campaign, campaign_id):
    make_campaign(campaign_id, stock=1)
    _, code = claim(scripts, campaign_id, "alice")
    assert claim(scripts, campaign_id, "bob")[0] == "SOLD_OUT"
    assert claim(scripts, campaign_id, "alice") == ["ALREADY", code]


def test_window(scripts, make_campaign):
    make_campaign("early", stock=5, start_ms=now_ms() + 60_000, end_ms=now_ms() + 120_000)
    make_campaign("late", stock=5, start_ms=now_ms() - 120_000, end_ms=now_ms() - 60_000)
    assert claim(scripts, "early", "u")[0] == "NOT_STARTED"
    assert claim(scripts, "late", "u")[0] == "ENDED"
    assert claim(scripts, "nope", "u")[0] == "NO_CAMPAIGN"


def test_seed_never_refills_stock(scripts, r, make_campaign, campaign_id):
    k = make_campaign(campaign_id, stock=3)
    claim(scripts, campaign_id, "a")
    assert scripts.seed(keys=[k.meta, k.stock], args=[3, now_ms() - 1000, now_ms() + 60_000]) == 2
