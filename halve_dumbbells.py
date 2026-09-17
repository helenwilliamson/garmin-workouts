#!/usr/bin/env python3
"""
One-shot migration: dumbbell sets logged as the COMBINED weight of both dumbbells
are rewritten to the single-dumbbell weight (halved).

The logging convention changed on CUTOVER below: sessions from that date carry the
weight of one dumbbell, sessions before it carry the pair's total. This script
brings the older half of the history into line with the newer half.

DANGER: halving is NOT idempotent. Running it twice over the same activity would
quarter the weights, not halve them. Two things stop that:

  1. LEDGER records every activity id written, and an activity in it is refused.
  2. The write only happens when the on-disk backup matches what Garmin currently
     holds, so a second pass over an already-halved activity fails its check.

Neither is a substitute for the backup. As with rewrite_sets.py, the PUT is
replace-all and Garmin keeps no version history, so backups/<id>.json is the ONLY
rollback -- restore with `rewrite_sets.py --restore <activityId>`.

Usage:
    python3 halve_dumbbells.py --plan     # what would change (default)
    python3 halve_dumbbells.py --apply    # write, verifying each activity
    python3 halve_dumbbells.py --ledger   # what has already been done
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liftwsarah as L  # noqa: E402  (reuses connect() and its token store)
import rewrite_sets as R  # noqa: E402  (reuses backups, activity listing, diffing)

# Sessions BEFORE this date logged both dumbbells together. On and after it, the
# logged number is one dumbbell. Established from the weights either side of it:
# the overhead press went 10/18 kg -> 5/8/9 kg and the lateral raise 8/12 -> 4/6,
# with no other change to either lift.
CUTOVER = "2026-09-08"

# Exercises whose FIT name says DUMBBELL but whose logged weight is not a pair:
#
#   CHEST_SUPPORTED_DUMBBELL_ROW -- actually the chest-supported MACHINE row. The
#       dumbbell enum is the closest FIT match (see EXERCISE_MAP in liftwsarah.py);
#       the weight is a stack. It also RISES across the cutover, 28 kg -> 42.5 kg,
#       which a convention change alone can't explain.
#   DUMBBELL_FRONT_SQUAT -- excluded at the lifter's request.
EXCLUDE_NAMES = {
    "CHEST_SUPPORTED_DUMBBELL_ROW",
    "DUMBBELL_FRONT_SQUAT",
}

LEDGER = os.path.join(R.BACKUP_DIR, "halved_dumbbells.json")


def load_ledger() -> dict:
    if not os.path.exists(LEDGER):
        return {"cutover": CUTOVER, "activities": {}}
    with open(LEDGER) as fh:
        return json.load(fh)


def save_ledger(ledger: dict) -> None:
    os.makedirs(R.BACKUP_DIR, exist_ok=True)
    with open(LEDGER, "w") as fh:
        json.dump(ledger, fh, indent=1)


def set_movement(s: dict) -> str | None:
    """The single movement a set records, or None if its top candidates disagree.

    `exercises` is a candidate list, so only the joint-highest probability entries
    say what was performed. Disagreement is left alone, never guessed at.
    """
    exercises = s.get("exercises") or []
    if not exercises:
        return None
    top_p = max((e.get("probability") or 0.0) for e in exercises)
    names = {e.get("name") for e in exercises if (e.get("probability") or 0.0) >= top_p}
    return names.pop() if len(names) == 1 else None


def halve_payload(payload: dict) -> tuple[dict, list[str]]:
    """Halve every qualifying dumbbell set in a copy of `payload`."""
    new = json.loads(json.dumps(payload))  # deep copy; payload is plain JSON
    changes: list[str] = []
    for s in new.get("exerciseSets") or []:
        if s.get("setType") != "ACTIVE":
            continue
        grams = s.get("weight")
        if not grams or grams <= 0:
            continue
        name = set_movement(s)
        if name is None or "DUMBBELL" not in name or name in EXCLUDE_NAMES:
            continue
        s["weight"] = grams / 2.0
        changes.append(f"set {s.get('messageIndex')} {name} "
                       f"({s.get('repetitionCount')} reps): "
                       f"{grams / 1000.0:g} kg -> {s['weight'] / 1000.0:g} kg")
    return new, changes


def targets(garmin, ledger: dict) -> list[tuple[dict, dict, dict, list[str]]]:
    """[(activity, live payload, halved payload, changes)] for activities in scope."""
    out = []
    for a in R.strength_activities(garmin):
        when = (a.get("startTimeLocal") or "")[:10]
        if not when or when >= CUTOVER:
            continue
        aid = str(a["activityId"])
        if aid in ledger.get("activities", {}):
            continue  # already halved; doing it again would quarter the weights
        payload = garmin.get_activity_exercise_sets(a["activityId"])
        new, changes = halve_payload(payload)
        if changes:
            out.append((a, payload, new, changes))
    return out


def describe(items) -> None:
    total = 0
    for a, _old, _new, changes in items:
        when = (a.get("startTimeLocal") or "")[:10]
        print(f"--- {when}  {a['activityId']}  {a.get('activityName', '')} ---")
        for c in changes:
            print(f"   {c}")
        total += len(changes)
    print(f"\n{total} sets across {len(items)} activities.")


def cmd_plan(garmin, ledger: dict) -> None:
    items = targets(garmin, ledger)
    if not items:
        print("Nothing to change.")
        return
    describe(items)
    print("Nothing written (--plan).")


def cmd_apply(garmin, ledger: dict) -> None:
    items = targets(garmin, ledger)
    if not items:
        print("Nothing to change.")
        return
    describe(items)
    print()
    for a, old, new, changes in items:
        aid = a["activityId"]
        when = (a.get("startTimeLocal") or "")[:10]
        path = R.backup_path(aid)

        # The backup is the only rollback, so it must exist AND still match Garmin.
        # A mismatch means either a stale dump or an activity already halved; both
        # are reasons to stop rather than write.
        if not os.path.exists(path):
            os.makedirs(R.BACKUP_DIR, exist_ok=True)
            with open(path, "w") as fh:
                json.dump(old, fh, indent=1)
            print(f"{when} {aid}: backed up -> {path}")
        else:
            with open(path) as fh:
                dumped = json.load(fh)
            if R.diff_payloads(dumped, old):
                print(f"{when} {aid}: SKIPPED — backup does not match live data. "
                      f"Re-dump it before halving.")
                continue

        garmin.set_activity_exercise_sets(aid, new)
        got = garmin.get_activity_exercise_sets(aid)
        problems = R.diff_payloads(new, got)
        if problems:
            print(f"{when} {aid}: WRITE VERIFY FAILED")
            for p in problems[:5]:
                print(f"      {p}")
            print(f"      restore with: python3 rewrite_sets.py --restore {aid}")
            continue
        ledger.setdefault("activities", {})[str(aid)] = {
            "date": when, "sets": len(changes),
            "halved_at": dt.datetime.now().isoformat(timespec="seconds"),
        }
        save_ledger(ledger)
        print(f"{when} {aid}: {len(changes)} sets halved, verified.")


def cmd_ledger(ledger: dict) -> None:
    acts = ledger.get("activities", {})
    if not acts:
        print("Ledger empty — nothing has been halved.")
        return
    print(f"cutover {ledger.get('cutover')}; {len(acts)} activities halved\n")
    for aid, info in sorted(acts.items(), key=lambda kv: kv[1].get("date", "")):
        print(f"  {info.get('date')}  {aid}  {info.get('sets')} sets  "
              f"at {info.get('halved_at')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", action="store_true",
                    help="show what would change without writing (the default)")
    ap.add_argument("--apply", action="store_true", help="write the halved weights")
    ap.add_argument("--ledger", action="store_true", help="show what has been done")
    args = ap.parse_args()

    ledger = load_ledger()
    if args.ledger:
        cmd_ledger(ledger)
        return
    garmin = L.connect()
    if args.apply:
        cmd_apply(garmin, ledger)
    else:
        cmd_plan(garmin, ledger)


if __name__ == "__main__":
    main()
