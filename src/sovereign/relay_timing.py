"""Timing calibration for one relay storage endpoint."""

from __future__ import annotations

import math
import statistics
import threading
import time
from collections import deque


PRESENCE_LIVENESS_MARGIN = 2.0
TIMING_CALIBRATION_SECONDS = 300.0


class RelayTiming:
    """Transient timing model expressed in the relay server's clock."""

    def __init__(self, timestamp_resolution_seconds: float = 1.0):
        self.timestamp_resolution_seconds = max(
            0.0, float(timestamp_resolution_seconds),
        )
        self._offset_samples = deque(maxlen=30)
        self._roundtrip_samples = deque(maxlen=30)
        self._cycle_samples = deque(maxlen=30)
        self._peer_presence: dict[str, tuple[float, float]] = {}
        self._last_probe_monotonic: float | None = None
        self._lock = threading.RLock()

    @staticmethod
    def _spread(samples) -> float:
        values = list(samples)
        return max(values) - min(values) if len(values) > 1 else 0.0

    def observe_server_clock(
        self, started_monotonic: float, started_wall: float,
        ended_monotonic: float, ended_wall: float,
        server_mtime: float | None, roundtrip_seconds: float | None = None,
    ) -> None:
        if server_mtime is None:
            return
        operation_seconds = max(0.0, ended_monotonic - started_monotonic)
        server_midpoint = (
            float(server_mtime) + self.timestamp_resolution_seconds / 2
        )
        local_midpoint = (started_wall + ended_wall) / 2
        uncertainty = (
            operation_seconds / 2 + self.timestamp_resolution_seconds / 2
        )
        with self._lock:
            self._offset_samples.append((
                server_midpoint - local_midpoint, uncertainty,
            ))
            if roundtrip_seconds is not None:
                self._roundtrip_samples.append(
                    max(0.0, float(roundtrip_seconds)),
                )
                self._last_probe_monotonic = ended_monotonic

    def observe_cycle(self, duration_seconds: float) -> None:
        with self._lock:
            self._cycle_samples.append(max(0.0, float(duration_seconds)))

    def observe_peer_presence(
        self, peer_id: str, server_mtime: float | None,
        poll_interval_seconds: float,
    ) -> None:
        if not peer_id or server_mtime is None:
            return
        with self._lock:
            self._peer_presence[peer_id] = (
                float(server_mtime), max(0.1, float(poll_interval_seconds)),
            )

    def probe_due(self, now_monotonic: float | None = None) -> bool:
        now_monotonic = (
            time.monotonic() if now_monotonic is None else now_monotonic
        )
        with self._lock:
            return (
                self._last_probe_monotonic is None
                or now_monotonic - self._last_probe_monotonic
                >= TIMING_CALIBRATION_SECONDS
            )

    def _summary(self) -> dict:
        with self._lock:
            offsets = list(self._offset_samples)
            roundtrips = list(self._roundtrip_samples)
            cycles = list(self._cycle_samples)
            peers = dict(self._peer_presence)
        offset = (
            statistics.median(value for value, _ in offsets)
            if offsets else None
        )
        uncertainty = max(
            (min(item[1] for item in offsets) if offsets else 0.0),
            self.timestamp_resolution_seconds / 2,
        )
        return {
            "offset": offset,
            "uncertainty": uncertainty,
            "roundtrip": min(roundtrips) if roundtrips else None,
            "roundtrip_jitter": self._spread(roundtrips),
            "relay_cycle": statistics.median(cycles) if cycles else None,
            "cycle_jitter": self._spread(cycles),
            "peers": peers,
            "sample_count": len(roundtrips),
            "cycle_sample_count": len(cycles),
        }

    def server_now(self, local_wall: float | None = None) -> float | None:
        summary = self._summary()
        if summary["offset"] is None:
            return None
        return (
            time.time() if local_wall is None else local_wall
        ) + summary["offset"]

    def response_check_delay(
        self, stable_interval_seconds: float,
        published_server_time: float | None = None,
        local_wall: float | None = None,
    ) -> float:
        stable = max(0.1, float(stable_interval_seconds))
        summary = self._summary()
        if summary["offset"] is None or not summary["peers"]:
            return stable
        server_now = (
            time.time() if local_wall is None else local_wall
        ) + summary["offset"]
        published_at = (
            server_now if published_server_time is None
            else published_server_time
        )
        jitter = max(summary["roundtrip_jitter"], summary["cycle_jitter"])
        relay_work = max(
            summary["roundtrip"] or 0.0,
            summary["relay_cycle"] or 0.0,
            0.05,
        )
        response_margin = relay_work + jitter + summary["uncertainty"]
        candidates = []
        for peer_mtime, peer_interval in summary["peers"].values():
            age = server_now - peer_mtime
            stale_after = PRESENCE_LIVENESS_MARGIN * (
                stable + peer_interval
            )
            if age > stale_after:
                continue
            if peer_mtime >= published_at - summary["uncertainty"]:
                peer_poll = peer_mtime
            else:
                periods = max(
                    1,
                    math.ceil((published_at - peer_mtime) / peer_interval),
                )
                peer_poll = peer_mtime + periods * peer_interval
            candidates.append(
                max(0.05, peer_poll + response_margin - server_now),
            )
        return min(candidates) if candidates else stable

    def status_payload(self) -> dict:
        summary = self._summary()

        def milliseconds(value):
            return round(value * 1000, 1) if value is not None else None

        return {
            "calibrated": summary["offset"] is not None,
            "roundtrip_ms": milliseconds(summary["roundtrip"]),
            "roundtrip_jitter_ms": milliseconds(
                summary["roundtrip_jitter"],
            ),
            "relay_cycle_ms": milliseconds(summary["relay_cycle"]),
            "relay_cycle_jitter_ms": milliseconds(summary["cycle_jitter"]),
            "server_clock_offset_ms": milliseconds(summary["offset"]),
            "clock_uncertainty_ms": milliseconds(summary["uncertainty"]),
            "samples": summary["sample_count"],
            "cycle_samples": summary["cycle_sample_count"],
        }
