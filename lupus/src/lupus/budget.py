"""Budgets: caps, reservations, settlement (§6.2, §11.2, BUDGET-01, LOOP-04).

Rules fixed here:
  * a reservation is charged against the goal budget AND every ancestor (shared parent budget)
  * committed = used + reserved + safety_reserved; a reservation must fit under cap
  * unknown usage is never refunded: a reservation whose owner died is settled at its full
    amount with observation='estimated'
  * safety reservations (verification / safe shutdown / rollback) cannot be re-assigned to work
  * caps change only through a recorded user decision
"""

from __future__ import annotations

import json
from typing import Mapping

from .kernel import Kernel
from .util import LupusError, canonical_json, new_id

RESERVABLE = ("calls", "attempts", "active_ms")
DIRECT = ("tokens", "storage_bytes")
REQUIRED_CAPS = ("calls", "attempts", "active_ms")


def create(k: Kernel, label: str, caps: Mapping[str, int], parent_budget_id: str | None = None) -> str:
    missing = [d for d in REQUIRED_CAPS if d not in caps]
    if missing:
        # A goal without call/attempt/time caps may not run autonomously (§11.2).
        raise LupusError("BUDGET_CAP_REQUIRED", ",".join(missing))
    with k.tx():
        budget_id = new_id("bud")
        k.run(
            "INSERT INTO budget(budget_id, parent_budget_id, label, created_at) VALUES (?,?,?,?)",
            budget_id, parent_budget_id, label, k.now(),
        )
        for dimension, cap in caps.items():
            k.run(
                "INSERT INTO budget_line(budget_id, dimension, cap) VALUES (?,?,?)",
                budget_id, dimension, int(cap),
            )
        return budget_id


def _chain(k: Kernel, budget_id: str) -> list[str]:
    chain, current = [], budget_id
    while current is not None:
        if current in chain:
            raise LupusError("BUDGET_CYCLE", current)
        chain.append(current)
        row = k.one("SELECT parent_budget_id FROM budget WHERE budget_id = ?", current)
        if row is None:
            raise LupusError("BUDGET_NOT_FOUND", current)
        current = row["parent_budget_id"]
    return chain


def snapshot(k: Kernel, budget_id: str) -> dict[str, dict[str, int]]:
    out = {}
    for row in k.q("SELECT * FROM budget_line WHERE budget_id = ? ORDER BY dimension", budget_id):
        out[row["dimension"]] = {
            "cap": row["cap"],
            "used": row["used"],
            "reserved": row["reserved"],
            "safety_reserved": row["safety_reserved"],
            "available": row["cap"] - row["used"] - row["reserved"] - row["safety_reserved"],
        }
    return out


def exhausted_dimensions(k: Kernel, budget_id: str) -> list[str]:
    """Reservable dimensions with nothing left anywhere in the chain (including overruns)."""
    out = set()
    for bid in _chain(k, budget_id):
        for dimension, line in snapshot(k, bid).items():
            if line["used"] > line["cap"] or (dimension in RESERVABLE and line["available"] <= 0):
                out.add(dimension)
    return sorted(out)


def reserve(
    k: Kernel,
    budget_id: str,
    kind: str,
    purpose: str,
    amounts: Mapping[str, int],
    owner_run_id: str | None = None,
) -> str:
    amounts = {d: int(n) for d, n in amounts.items() if int(n) > 0}
    if not amounts:
        raise LupusError("RESERVATION_EMPTY", purpose)
    for dimension in amounts:
        if dimension not in RESERVABLE:
            raise LupusError("DIMENSION_NOT_RESERVABLE", dimension)
    column = "safety_reserved" if kind == "safety" else "reserved"
    with k.tx():
        chain = _chain(k, budget_id)
        for bid in chain:
            lines = snapshot(k, bid)
            for dimension, line in lines.items():
                if line["used"] > line["cap"]:
                    raise LupusError("BUDGET_EXHAUSTED", f"{bid}:{dimension} overrun")
            for dimension, amount in amounts.items():
                line = lines.get(dimension)
                if line is None:
                    if bid == budget_id:
                        raise LupusError("BUDGET_DIMENSION_UNBOUNDED", dimension)
                    continue
                if amount > line["available"]:
                    raise LupusError(
                        "BUDGET_EXHAUSTED", f"{bid}:{dimension} need={amount} available={line['available']}"
                    )
        for bid in chain:
            for dimension, amount in amounts.items():
                k.run(
                    f"UPDATE budget_line SET {column} = {column} + ? WHERE budget_id = ? AND dimension = ?",
                    amount, bid, dimension,
                )
        reservation_id = new_id("rsv")
        k.run(
            "INSERT INTO reservation(reservation_id, budget_id, kind, purpose, owner_run_id, amounts, "
            "status, created_at) VALUES (?,?,?,?,?,?, 'HELD', ?)",
            reservation_id, budget_id, kind, purpose, owner_run_id, canonical_json(amounts), k.now(),
        )
        return reservation_id


def settle(
    k: Kernel,
    reservation_id: str,
    actual: Mapping[str, int] | None,
    observation: str,
) -> dict[str, int]:
    """Convert a hold into recorded usage. `actual=None` means "not observed": the full reserved
    amount is charged. Settling twice is a no-op that returns the first result."""
    with k.tx():
        row = k.one("SELECT * FROM reservation WHERE reservation_id = ?", reservation_id)
        if row is None:
            raise LupusError("RESERVATION_NOT_FOUND", reservation_id)
        if row["status"] == "SETTLED":
            return json.loads(row["actual"])
        amounts: dict[str, int] = json.loads(row["amounts"])
        if actual is None:
            charged, observation = dict(amounts), "estimated"
        else:
            charged = {d: max(0, int(actual.get(d, 0))) for d in amounts}
        column = "safety_reserved" if row["kind"] == "safety" else "reserved"
        overrun = {}
        for bid in _chain(k, row["budget_id"]):
            for dimension, held in amounts.items():
                k.run(
                    f"UPDATE budget_line SET {column} = {column} - ?, used = used + ? "
                    "WHERE budget_id = ? AND dimension = ?",
                    held, charged[dimension], bid, dimension,
                )
            for dimension, line in snapshot(k, bid).items():
                if line["used"] > line["cap"]:
                    overrun[f"{bid}:{dimension}"] = line["used"] - line["cap"]
        k.run(
            "UPDATE reservation SET status = 'SETTLED', actual = ?, observation = ?, settled_at = ? "
            "WHERE reservation_id = ?",
            canonical_json(charged), observation, k.now(), reservation_id,
        )
        if overrun:
            # Reported, never hidden: new reservations are refused from now on.
            k.emit("supervisor", "budget.overrun", "budget", row["budget_id"], overrun=overrun)
        return charged


def settle_orphans(k: Kernel, run_id: str) -> int:
    """A dead run's holds become estimated usage at the full reserved amount."""
    with k.tx():
        rows = k.q(
            "SELECT reservation_id FROM reservation WHERE owner_run_id = ? AND status = 'HELD'", run_id
        )
        for row in rows:
            settle(k, row["reservation_id"], None, "estimated")
        return len(rows)


def settle_unowned(k: Kernel) -> int:
    """Holds left by a supervisor that died: no owning run, or an owner that is already stopped.
    Charged in full as estimated usage, like any other unobserved spend."""
    with k.tx():
        rows = k.q(
            "SELECT reservation_id FROM reservation WHERE status = 'HELD' AND (owner_run_id IS NULL "
            "OR owner_run_id IN (SELECT run_id FROM run WHERE status = 'STOPPED'))")
        for row in rows:
            settle(k, row["reservation_id"], None, "estimated")
        return len(rows)


def charge_direct(k: Kernel, budget_id: str, dimension: str, amount: int) -> None:
    """Charge observed tokens / stored bytes. Lines that do not exist are simply not tracked."""
    if dimension not in DIRECT:
        raise LupusError("DIMENSION_NOT_DIRECT", dimension)
    if amount <= 0:
        return
    with k.tx():
        for bid in _chain(k, budget_id):
            k.run(
                "UPDATE budget_line SET used = used + ? WHERE budget_id = ? AND dimension = ?",
                int(amount), bid, dimension,
            )


def fits_direct(k: Kernel, budget_id: str, dimension: str, amount: int) -> bool:
    for bid in _chain(k, budget_id):
        line = snapshot(k, bid).get(dimension)
        if line is not None and line["used"] + amount > line["cap"]:
            return False
    return True


def raise_cap(k: Kernel, budget_id: str, dimension: str, new_cap: int, actor: str, reason: str) -> None:
    if actor != "user":
        raise LupusError("USER_AUTHORITY_REQUIRED", "budget caps are changed only by the user")
    with k.tx():
        row = k.one(
            "SELECT cap FROM budget_line WHERE budget_id = ? AND dimension = ?", budget_id, dimension
        )
        if row is None:
            raise LupusError("BUDGET_LINE_NOT_FOUND", f"{budget_id}:{dimension}")
        k.run(
            "UPDATE budget_line SET cap = ? WHERE budget_id = ? AND dimension = ?",
            int(new_cap), budget_id, dimension,
        )
        k.run(
            "INSERT INTO budget_change(budget_id, dimension, old_cap, new_cap, actor, reason, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            budget_id, dimension, row["cap"], int(new_cap), actor, reason, k.now(),
        )
        k.emit(actor, "budget.cap_changed", "budget", budget_id, dimension=dimension,
               old=row["cap"], new=int(new_cap), reason=reason)
