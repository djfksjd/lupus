"""Usage ledger (§11.2, USAGE-01, COST-01).

  * event_id makes replays no-ops
  * per provider session the ledger tracks the provider's running counter as far as it has
    been charged. A cumulative report (a resumed session reporting session totals) is charged
    by its increase over that counter and then becomes the counter; a delta report is charged
    as is and added to the counter. Mixing both modes in one session therefore cannot charge
    the same usage twice
  * every report advances the session's sequence; a cumulative report older than something
    already charged adds nothing, and neither does a late delta from before a cumulative total
    that already included it
  * a counter that goes DOWN is treated as a reset: the new value is charged in full once and
    becomes the new baseline. The anomaly is recorded
  * usage is recorded for any run, including one whose result was rejected as stale. Spend that
    happened is never dropped
  * what was not observed is stored as 'unknown'/'estimated', never as zero
Input, cached-input and output tokens all charge the goal budget's `tokens` line when one
exists (cached tokens are cheaper for the provider but they are still observed usage).
Subscription quota left is not observable and is never derived from these numbers.
"""

from __future__ import annotations

import json
from typing import Mapping

from . import budget, goals, runs
from .kernel import Kernel
from .util import LupusError, canonical_json

FIELDS = ("tokens_in", "tokens_out", "tokens_cached", "calls", "active_ms")


def record(
    k: Kernel,
    *,
    event_id: str,
    run_id: str,
    provider_session_id: str,
    mode: str,
    sequence: int,
    reported: Mapping[str, int],
    observation: str,
    source: str,
) -> dict:
    if mode not in ("delta", "cumulative"):
        raise LupusError("USAGE_MODE_INVALID", mode)
    values = {f: max(0, int(reported.get(f, 0))) for f in FIELDS}
    with k.tx():
        existing = k.one("SELECT applied FROM usage_event WHERE event_id = ?", event_id)
        if existing is not None:
            return {"applied": json.loads(existing["applied"]), "replayed": True}
        run = runs.get(k, run_id)
        goal = goals.get(k, run["goal_id"])
        mark = k.one("SELECT * FROM usage_watermark WHERE provider_session_id = ?", provider_session_id)
        anomaly = None
        counter = json.loads(mark["counter"]) if mark else {f: 0 for f in FIELDS}
        last_sequence = mark["last_sequence"] if mark else 0
        covered = mark["cumulative_sequence"] if mark else 0
        if mode == "delta" and sequence <= covered:
            # A late delta from before a cumulative total that already included it.
            applied, anomaly = {f: 0 for f in FIELDS}, "stale_sequence"
        elif mode == "delta":
            applied = dict(values)
            counter = {f: counter[f] + values[f] for f in FIELDS}
        elif mark is not None and sequence <= last_sequence:
            applied, anomaly = {f: 0 for f in FIELDS}, "stale_sequence"
        else:
            covered = sequence
            applied = {}
            for f in FIELDS:
                if values[f] < counter[f]:
                    applied[f], anomaly = values[f], "counter_reset"
                else:
                    applied[f] = values[f] - counter[f]
            counter = dict(values)
        k.run(
            "INSERT INTO usage_watermark(provider_session_id, last_sequence, cumulative_sequence, counter) "
            "VALUES (?,?,?,?) ON CONFLICT(provider_session_id) DO UPDATE SET "
            "last_sequence = excluded.last_sequence, cumulative_sequence = excluded.cumulative_sequence, "
            "counter = excluded.counter",
            provider_session_id, max(last_sequence, sequence), covered, canonical_json(counter),
        )
        k.run(
            "INSERT INTO usage_event(event_id, run_id, goal_id, provider_session_id, mode, sequence, "
            "reported, applied, observation, source, anomaly, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            event_id, run_id, goal["goal_id"], provider_session_id, mode, sequence,
            canonical_json(values), canonical_json(applied), observation, source, anomaly, k.now(),
        )
        budget.charge_direct(k, goal["budget_id"], "tokens",
                             applied["tokens_in"] + applied["tokens_cached"] + applied["tokens_out"])
        return {"applied": applied, "replayed": False, "anomaly": anomaly}


def totals(k: Kernel, goal_id: str) -> dict:
    """Totals with their observation level. Mixed levels are reported, not merged into one
    number that looks measured."""
    out = {f: 0 for f in FIELDS}
    levels: dict[str, int] = {}
    for row in k.q("SELECT applied, observation FROM usage_event WHERE goal_id = ?", goal_id):
        for f, v in json.loads(row["applied"]).items():
            out[f] += v
        levels[row["observation"]] = levels.get(row["observation"], 0) + 1
    return {"totals": out, "events_by_observation": levels}
