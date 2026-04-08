"""Stream consumer: Redis claim events -> DynamoDB.

    python -m worker.main

Delivery semantics: at-least-once. An event is XACKed only after DynamoDB accepted (or
had already accepted) it. If this process dies mid-batch, its pending events are picked
up by any consumer via XAUTOCLAIM once idle for ``worker_min_idle_ms``. Events delivered
more than ``worker_max_deliveries`` times are moved to the campaign's dead-letter stream.
"""

import logging
import os
import signal
import socket
import time

import redis

from common import dynamo as ddb
from common.keys import ACTIVE_CAMPAIGNS, CONSUMER_GROUP, campaign_id_from_stream, campaign_keys
from common.metrics import Metrics
from common.settings import Settings, get_settings
from worker.persister import Outcome, Persister, PoisonEvent

log = logging.getLogger("worker")


class Worker:
    def __init__(self, settings: Settings, redis_client: redis.Redis, dynamo: ddb.Dynamo):
        self.s = settings
        self.r = redis_client
        self.persister = Persister(
            dynamo, redis_client, settings.campaigns_table, settings.claims_table
        )
        self.metrics = Metrics(settings.metrics_namespace, "worker", settings.metrics_emf)
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"
        self.streams: list[str] = []
        self._autoclaim_cursor: dict[str, str] = {}
        self._stopping = False
        self._last_refresh = 0.0
        self._last_flush = time.monotonic()

    # ------------------------------------------------------------------ setup
    def refresh_streams(self) -> None:
        streams = []
        for campaign_id in sorted(self.r.smembers(ACTIVE_CAMPAIGNS)):
            stream = campaign_keys(campaign_id).events
            try:
                self.r.xgroup_create(stream, CONSUMER_GROUP, id="0", mkstream=True)
            except redis.ResponseError as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            streams.append(stream)
        self.streams = streams
        self._last_refresh = time.monotonic()

    def stop(self, *_):
        log.info("stopping after current batch")
        self._stopping = True

    # ------------------------------------------------------------------ processing
    def process(self, stream: str, msg_id: str, fields: dict[str, str]) -> None:
        campaign_id = campaign_id_from_stream(stream)
        t0 = time.perf_counter()
        try:
            outcome = self.persister.persist(campaign_id, fields)
        except PoisonEvent as exc:
            self.dead_letter(stream, msg_id, fields, str(exc))
            return
        except Exception:
            # Leave it pending; XAUTOCLAIM retries it after min_idle.
            log.exception("persist failed for %s %s", stream, msg_id)
            self.metrics.incr("PersistErrors")
            return
        self.r.xack(stream, CONSUMER_GROUP, msg_id)
        self.metrics.timing_ms("PersistLatency", (time.perf_counter() - t0) * 1000)
        self.metrics.incr(f"Persist{outcome.value.title()}")
        if outcome is Outcome.REVOKED:
            log.error("REVOKED claim: campaign=%s fields=%s", campaign_id, fields)

    def dead_letter(self, stream: str, msg_id: str, fields: dict[str, str], reason: str) -> None:
        dlq = campaign_keys(campaign_id_from_stream(stream)).dlq
        self.r.xadd(dlq, {**fields, "origId": msg_id, "reason": reason[:500]})
        self.r.xack(stream, CONSUMER_GROUP, msg_id)
        self.metrics.incr("DeadLettered")
        log.error("dead-lettered %s %s: %s", stream, msg_id, reason)

    def recover_stale(self) -> int:
        """Claim events another (dead) consumer left pending, retry or dead-letter them."""
        recovered = 0
        for stream in self.streams:
            cursor = self._autoclaim_cursor.get(stream, "0-0")
            resp = self.r.xautoclaim(
                stream,
                CONSUMER_GROUP,
                self.consumer,
                min_idle_time=self.s.worker_min_idle_ms,
                start_id=cursor,
                count=self.s.worker_batch_size,
            )
            next_cursor, messages = resp[0], resp[1]
            self._autoclaim_cursor[stream] = next_cursor
            for msg_id, fields in messages:
                if fields is None:  # entry was trimmed/deleted
                    self.r.xack(stream, CONSUMER_GROUP, msg_id)
                    continue
                info = self.r.xpending_range(
                    stream, CONSUMER_GROUP, min=msg_id, max=msg_id, count=1
                )
                deliveries = info[0]["times_delivered"] if info else 1
                if deliveries > self.s.worker_max_deliveries:
                    self.dead_letter(stream, msg_id, fields, f"exceeded {deliveries} deliveries")
                else:
                    self.process(stream, msg_id, fields)
                recovered += 1
        return recovered

    def read_new(self) -> int:
        if not self.streams:
            time.sleep(self.s.worker_block_ms / 1000)
            return 0
        resp = self.r.xreadgroup(
            CONSUMER_GROUP,
            self.consumer,
            {s: ">" for s in self.streams},
            count=self.s.worker_batch_size,
            block=self.s.worker_block_ms,
        )
        n = 0
        for stream, messages in resp or []:
            for msg_id, fields in messages:
                self.process(stream, msg_id, fields)
                n += 1
        return n

    def report_lag(self) -> None:
        pending = lag = 0
        for stream in self.streams:
            for g in self.r.xinfo_groups(stream):
                if g["name"] == CONSUMER_GROUP:
                    pending += g.get("pending") or 0
                    lag += g.get("lag") or 0
        self.metrics.gauge("StreamPending", pending)
        self.metrics.gauge("StreamLag", lag)

    def run_once(self) -> int:
        if time.monotonic() - self._last_refresh > self.s.worker_refresh_s:
            self.refresh_streams()
        n = self.recover_stale() + self.read_new()
        if time.monotonic() - self._last_flush > self.s.metrics_flush_s:
            self.report_lag()
            self.metrics.flush()
            self._last_flush = time.monotonic()
        return n

    def run(self) -> None:
        log.info("worker %s starting", self.consumer)
        while not self._stopping:
            try:
                self.run_once()
            except redis.ConnectionError:
                log.exception("redis unavailable; backing off")
                time.sleep(1)
        self.metrics.flush()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    s = get_settings()
    w = Worker(s, redis.Redis.from_url(s.redis_url, decode_responses=True), ddb.connect(s))
    signal.signal(signal.SIGTERM, w.stop)
    signal.signal(signal.SIGINT, w.stop)
    w.run()


if __name__ == "__main__":
    main()
