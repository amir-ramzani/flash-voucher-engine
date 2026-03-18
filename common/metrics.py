"""Minimal CloudWatch Embedded Metric Format (EMF) emitter.

Metrics are aggregated in-process and flushed as one JSON log line every few seconds;
the awslogs driver ships it to CloudWatch Logs, which extracts the metrics. No
PutMetricData calls on the hot path, no extra IAM permissions.

Latencies are kept as a log-bucketed histogram (Values/Counts arrays, ~5% resolution) so
CloudWatch can compute p50/p99 over them.
"""

import json
import logging
import math
import sys
import threading
import time
from collections import Counter, defaultdict

log = logging.getLogger(__name__)

_BUCKETS_PER_DECADE = 20  # 10**(1/20) ≈ 12% bucket width => ~6% max error
_MAX_VALUES = 100  # EMF limit per metric


def _bucket(v: float) -> float:
    if v <= 0:
        return 0.0
    return round(10 ** (round(math.log10(v) * _BUCKETS_PER_DECADE) / _BUCKETS_PER_DECADE), 3)


class Metrics:
    def __init__(self, namespace: str, service: str, enabled: bool):
        self.namespace = namespace
        self.service = service
        self.enabled = enabled
        self._lock = threading.Lock()
        self._hist: dict[str, Counter] = defaultdict(Counter)
        self._counts: Counter = Counter()
        self._gauges: dict[str, float] = {}

    def timing_ms(self, name: str, value_ms: float) -> None:
        if self.enabled:
            with self._lock:
                self._hist[name][_bucket(value_ms)] += 1

    def incr(self, name: str, n: int = 1) -> None:
        if self.enabled:
            with self._lock:
                self._counts[name] += n

    def gauge(self, name: str, value: float) -> None:
        if self.enabled:
            with self._lock:
                self._gauges[name] = value

    def flush(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            hist, counts, gauges = self._hist, self._counts, self._gauges
            self._hist, self._counts, self._gauges = defaultdict(Counter), Counter(), {}
        if not (hist or counts or gauges):
            return
        doc: dict = {"Service": self.service}
        defs = []
        for name, c in hist.items():
            items = sorted(c.items())[:_MAX_VALUES]
            doc[name] = {"Values": [v for v, _ in items], "Counts": [n for _, n in items]}
            defs.append({"Name": name, "Unit": "Milliseconds"})
        for name, n in counts.items():
            doc[name] = n
            defs.append({"Name": name, "Unit": "Count"})
        for name, v in gauges.items():
            doc[name] = v
            defs.append({"Name": name, "Unit": "Count"})
        doc["_aws"] = {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {"Namespace": self.namespace, "Dimensions": [["Service"]], "Metrics": defs}
            ],
        }
        sys.stdout.write(json.dumps(doc) + "\n")
        sys.stdout.flush()
