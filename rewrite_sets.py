#!/usr/bin/env python3
"""
Retroactively fix exercise names on already-logged Garmin strength activities.

Garmin's watch picks the WEIGHTED_* FIT variant of an exercise once a weight is
on the set (WEIGHTED_STANDING_ADDUCTION rather than STANDING_ADDUCTION), but not
consistently — some sessions carry only the plain name, and most sets carry a
candidate list with the plain name duplicated alongside the weighted one. This
script normalises those names so history is consistent and so
liftwsarah.py --pull-weights can find them.

DANGER: PUT /activity/{id}/exerciseSets has REPLACE-ALL semantics and Garmin
keeps no version history. The dump written by --dump is the ONLY rollback.
Always --dump before --apply. --restore replays a dump.

Usage (each step verifies before the next):
    python3 rewrite_sets.py --dump                 # back up every strength activity
    python3 rewrite_sets.py --plan                 # show what would change (default)
    python3 rewrite_sets.py --probe <activityId>   # no-op round-trip: does the server
                                                   # preserve what we send back?
    python3 rewrite_sets.py --apply [--only <id>]  # write
    python3 rewrite_sets.py --restore <activityId> # put the dumped payload back

Scope is one rename rule at a time, set by RULES below. Only sets with a real
weight (> 0) are touched — WEIGHTED_* on a bodyweight set would be a lie.
"""

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import liftwsarah as L  # noqa: E402  (reuses connect() and its token store)

BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
LOOKBACK_DAYS = 120

# Rename rules are DERIVED, not hand-listed: every plain FIT exercise name that has
# a WEIGHTED_ twin in its category maps to that twin. Applied only to ACTIVE sets
# carrying weight > 0, so bodyweight sets keep their plain name.
#
# fit_weighted_variants.json is generated from the garmin-fit-sdk *_exercise_name
# enums (see its _source key). Garmin validates names server-side and 400s on
# unknown ones, so a stale table fails loudly rather than corrupting data.
VARIANTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "fit_weighted_variants.json")

# Category/name pairs to leave alone even though a weighted twin exists. Use for
# movements where the logged weight isn't load borne by you (machine stacks that
# Garmin already treats as the exercise's own resistance), or anything you'd
# rather keep reading as the plain movement.
EXCLUDE: set[tuple[str, str]] = set()


def load_rules() -> dict[tuple[str, str], str]:
    with open(VARIANTS_PATH) as fh:
        data = json.load(fh)
    rules: dict[tuple[str, str], str] = {}
    for category, mapping in data["variants"].items():
        for plain, weighted in mapping.items():
            if (category, plain) not in EXCLUDE:
                rules[(category, plain)] = weighted
    return rules


RULES: dict[tuple[str, str], str] = load_rules()

# Garmin sends a candidate list per set: the exercise actually performed at the
# highest probability, plus duplicates at 99.609375 (= 255/256, FIT's "invalid"
# byte). After a rename the list would hold the same name several times over, so
# it is collapsed to one entry. Set to False to leave the list length alone.
#
# The surviving entry keeps the ORIGINAL top probability rather than asserting
# 100.0 — we're renaming what Garmin recorded, not claiming better confidence.
COLLAPSE_CANDIDATES = True


def strength_activities(garmin, lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    """Every strength_training activity in the window, oldest first."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=lookback_days)).isoformat()
    acts = garmin.get_activities_by_date(start, today.isoformat())
    acts = [a for a in acts
            if (a.get("activityType") or {}).get("typeKey") == "strength_training"]
    return sorted(acts, key=lambda a: a.get("startTimeLocal") or "")


def backup_path(activity_id) -> str:
    return os.path.join(BACKUP_DIR, f"{activity_id}.json")


def _base(name: str | None) -> str | None:
    """Strip the WEIGHTED_ prefix so both variants of a movement compare equal."""
    if name and name.startswith("WEIGHTED_"):
        return name[len("WEIGHTED_"):]
    return name


def rewrite_payload(payload: dict) -> tuple[dict, list[str]]:
    """Apply RULES to a copy of `payload`. Returns (new_payload, change descriptions).

    A set is rewritten only when it is ACTIVE, carries weight > 0, and its
    top-probability candidate names a movement with a WEIGHTED_ twin.

    Only the top-probability candidates decide the movement: `exercises` is a
    candidate list, so trusting lower-confidence entries would rename a set to a
    movement that wasn't performed. Sets whose top candidates disagree about the
    movement are left untouched and reported, never guessed at.
    """
    new = json.loads(json.dumps(payload))  # deep copy; payload is plain JSON
    changes: list[str] = []

    for s in new.get("exerciseSets") or []:
        if s.get("setType") != "ACTIVE":
            continue
        weight = s.get("weight")
        if not weight or weight <= 0:
            continue

        exercises = s.get("exercises") or []
        if not exercises:
            continue

        top_p = max((e.get("probability") or 0.0) for e in exercises)
        top = [e for e in exercises if (e.get("probability") or 0.0) >= top_p]
        movements = {(e.get("category"), _base(e.get("name"))) for e in top}
        if len(movements) > 1:
            changes.append(f"set {s.get('messageIndex')}: SKIPPED — top candidates "
                           f"disagree: {sorted(movements)}")
            continue

        category, plain = movements.pop()
        if plain is None:
            continue
        target = RULES.get((category, plain))
        if target is None:
            continue  # no weighted twin in the FIT enum for this movement

        before = [(e.get("name"), e.get("probability")) for e in exercises]
        if COLLAPSE_CANDIDATES:
            s["exercises"] = [{"category": category, "name": target, "probability": top_p}]
        else:
            for e in exercises:
                if (e.get("category"), _base(e.get("name"))) == (category, plain):
                    e["name"] = target
        after = [(e.get("name"), e.get("probability")) for e in s["exercises"]]

        if before == after:
            continue  # already a single weighted entry — nothing to write
        changes.append(
            f"set {s.get('messageIndex')} ({s.get('repetitionCount')} reps @ "
            f"{weight / 1000.0:g} kg): {before} -> {after}"
        )

    return new, changes


def diff_payloads(sent: dict, got: dict) -> list[str]:
    """Field-by-field diff of what we PUT against what the server hands back."""
    problems: list[str] = []
    a = sent.get("exerciseSets") or []
    b = got.get("exerciseSets") or []
    if len(a) != len(b):
        problems.append(f"set count: sent {len(a)}, got {len(b)}")
        return problems
    for i, (sa, sb) in enumerate(zip(a, b)):
        for k in sorted(set(sa) | set(sb)):
            va, vb = sa.get(k), sb.get(k)
            if va != vb:
                problems.append(f"set {i} field {k!r}: sent {va!r}, got {vb!r}")
    return problems


def cmd_dump(garmin) -> None:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    acts = strength_activities(garmin)
    print(f"Dumping {len(acts)} strength activities to {BACKUP_DIR}\n")
    for a in acts:
        aid = a["activityId"]
        when = (a.get("startTimeLocal") or "")[:10]
        path = backup_path(aid)
        if os.path.exists(path):
            print(f"  {when}  {aid}  already dumped, skipped")
            continue
        payload = garmin.get_activity_exercise_sets(aid)
        with open(path, "w") as fh:
            json.dump(payload, fh, indent=1)
        n = len(payload.get("exerciseSets") or [])
        print(f"  {when}  {aid}  {n:>3} sets  -> {os.path.basename(path)}")


def collect_targets(garmin) -> list[tuple[dict, dict, dict, list[str]]]:
    """(activity, current payload, rewritten payload, changes) for activities that change."""
    out = []
    for a in strength_activities(garmin):
        payload = garmin.get_activity_exercise_sets(a["activityId"])
        new, changes = rewrite_payload(payload)
        if changes:
            out.append((a, payload, new, changes))
    return out


def cmd_plan(garmin, verbose: bool = False) -> None:
    print(f"{len(RULES)} rename rules derived from {os.path.basename(VARIANTS_PATH)}"
          f" ({len(EXCLUDE)} excluded); ACTIVE sets with weight > 0 only;"
          f" collapse candidates: {COLLAPSE_CANDIDATES}\n")

    targets = collect_targets(garmin)
    if not targets:
        print("Nothing to change.")
        return

    # Group by the rename actually performed, so the sweep can be reviewed by
    # movement rather than by scrolling hundreds of individual sets.
    by_move: dict[str, dict] = {}
    skipped: list[str] = []
    total = 0
    for a, _cur, _new, changes in targets:
        when = (a.get("startTimeLocal") or "")[:10]
        for c in changes:
            if "SKIPPED" in c:
                skipped.append(f"{when} {a['activityId']}: {c}")
                continue
            total += 1
            move = c.split("-> [('", 1)[1].split("'", 1)[0] if "-> [('" in c else "?"
            rec = by_move.setdefault(move, {"n": 0, "dates": set()})
            rec["n"] += 1
            rec["dates"].add(when)
            if verbose:
                print(f"   {when} {a['activityId']}  {c}")

    print(f"{'target name':44} {'sets':>4}  dates")
    for move, rec in sorted(by_move.items()):
        ds = sorted(rec["dates"])
        span = ds[0] if len(ds) == 1 else f"{ds[0]}..{ds[-1]} ({len(ds)} sessions)"
        print(f"  {move:42} {rec['n']:>4}  {span}")

    if skipped:
        print(f"\n{len(skipped)} set(s) skipped (ambiguous top candidates):")
        for s in skipped:
            print(f"  {s}")

    print(f"\n{total} sets across {len(targets)} activities. Nothing written (--plan).")


def cmd_probe(garmin, activity_id: int) -> None:
    """PUT the payload back UNCHANGED, then re-read and diff.

    This is the read-only-in-effect test of whether the server preserves what we
    send: any field that comes back different is one Garmin rewrites, and a
    rename would silently damage it.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    payload = garmin.get_activity_exercise_sets(activity_id)
    with open(backup_path(activity_id), "w") as fh:
        json.dump(payload, fh, indent=1)
    print(f"backed up -> {backup_path(activity_id)}")

    print(f"PUT unchanged payload ({len(payload.get('exerciseSets') or [])} sets)...")
    garmin.set_activity_exercise_sets(activity_id, payload)
    got = garmin.get_activity_exercise_sets(activity_id)

    problems = diff_payloads(payload, got)
    if not problems:
        print("Round-trip clean: server returned exactly what was sent.")
    else:
        print("Server altered these fields — treat a rewrite as unsafe until understood:")
        for p in problems:
            print(f"  {p}")


def cmd_apply(garmin, only: int | None) -> None:
    targets = collect_targets(garmin)
    if only is not None:
        targets = [t for t in targets if t[0]["activityId"] == only]
        if not targets:
            print(f"Activity {only} has nothing to change (or isn't in the window).")
            return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    for a, cur, new, changes in targets:
        aid = a["activityId"]
        when = (a.get("startTimeLocal") or "")[:10]
        path = backup_path(aid)
        if not os.path.exists(path):
            with open(path, "w") as fh:
                json.dump(cur, fh, indent=1)
            print(f"backed up {aid} -> {os.path.basename(path)}")

        print(f"--- {when}  {aid}  {a.get('activityName')} ---")
        for c in changes:
            print(f"   {c}")
        garmin.set_activity_exercise_sets(aid, new)
        got = garmin.get_activity_exercise_sets(aid)
        problems = diff_payloads(new, got)
        if problems:
            print("   !! server did not store what was sent:")
            for p in problems:
                print(f"      {p}")
        else:
            print("   written and verified (read-back matches).")


def cmd_restore(garmin, activity_id: int) -> None:
    path = backup_path(activity_id)
    if not os.path.exists(path):
        print(f"No dump at {path} — nothing to restore from.")
        return
    with open(path) as fh:
        payload = json.load(fh)
    garmin.set_activity_exercise_sets(activity_id, payload)
    got = garmin.get_activity_exercise_sets(activity_id)
    problems = diff_payloads(payload, got)
    print(f"restored {activity_id} from {os.path.basename(path)}"
          + ("" if not problems else f" — {len(problems)} field(s) differ:"))
    for p in problems:
        print(f"  {p}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump", action="store_true", help="back up every strength activity")
    ap.add_argument("--plan", action="store_true", help="show what would change (default)")
    ap.add_argument("--probe", type=int, metavar="ID",
                    help="no-op round-trip on one activity to test server fidelity")
    ap.add_argument("--apply", action="store_true", help="write the changes")
    ap.add_argument("--only", type=int, metavar="ID", help="restrict --apply to one activity")
    ap.add_argument("--restore", type=int, metavar="ID", help="replay the dumped payload")
    ap.add_argument("--verbose", action="store_true", help="--plan: list every set")
    args = ap.parse_args()

    garmin = L.connect()

    if args.dump:
        cmd_dump(garmin)
    elif args.probe is not None:
        cmd_probe(garmin, args.probe)
    elif args.restore is not None:
        cmd_restore(garmin, args.restore)
    elif args.apply:
        cmd_apply(garmin, args.only)
    else:
        cmd_plan(garmin, verbose=args.verbose)


if __name__ == "__main__":
    main()
