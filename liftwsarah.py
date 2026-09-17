#!/usr/bin/env python3
"""
Build the 6 "Lift with Sarah" gym sessions as Garmin structured strength
workouts and upload them to Garmin Connect.

Structure per exercise (as requested):
  - N sets of the exercise, each ending on a REP count
  - a 90-second (1.5 min) timed rest between sets
  - a LAP-BUTTON rest between exercises (open-ended: press lap when ready)

Setup:
    python3 -m venv .venv && source .venv/bin/activate
    pip install garminconnect pydantic      # pydantic is REQUIRED (see note)
    export GARMIN_EMAIL="you@example.com"
    export GARMIN_PASSWORD="..."

Then:
    python3 garmin_liftwsarah.py            # build + upload all 4 sessions
    python3 garmin_liftwsarah.py --dry-run  # build + print, no upload/login
    python3 garmin_liftwsarah.py --pull-weights --dry-run  # pull recent weights, preview, no upload
    python3 garmin_liftwsarah.py --pull-weights            # pull recent weights, then upload
    python3 garmin_liftwsarah.py --schedule --no-upload    # fill the calendar, upload nothing
    python3 garmin_liftwsarah.py --schedule 8 --no-upload --dry-run   # preview 8 weeks

EDIT ME: entries are (name, sets, reps, weight_kg). Reps are placeholders
(3 x 10 throughout) since the video's numbers weren't in the source list, and
every weight defaults to 0 (bodyweight / not yet set) — fill in the kg per
exercise. Weight applies to all sets of that exercise.
"""

import datetime as dt
import json
import os
import sys

from garminconnect import Garmin
from garminconnect.workout import (
    BaseWorkout,
    WorkoutSegment,
    ExecutableStep,
    create_repeat_group,
    SportType,
    StepType,
    ConditionType,
    TargetType,
)

TOKENSTORE = os.path.expanduser("~/.garminconnect")
REST_BETWEEN_SETS_SECS = 90
# Per-exercise rest, where the default is wrong. 90 s is set by the heavy compounds;
# light accessory and cuff/core work recovers long before that, and paying the full
# rest on a 12-set exercise is most of what makes a session run long.
REST_OVERRIDES: dict[str, int] = {
    "Cable Woodchop": 45,   # light rotational work, one side at a time
    "Farmer's Walk": 45,
}
# The between-exercise rest is a lap-button step, i.e. open-ended, so the duration
# estimate has to assume a floor for it. 90 s minimum, same as the between-set rest.
EST_REST_BETWEEN_EXERCISES_SECS = 90
WEIGHT_LOOKBACK_DAYS = 56  # how far back --pull-weights looks for recent working weights

# A set under this many reps is a 1RM/heavy-single test, not a working set, so it
# must not set the prescribed weight. Sets with no rep count recorded are also
# skipped — we can't tell which kind they were.
MIN_WORKING_SET_REPS = 6

# Working-set rep target = reps logged at that weight PLUS this, so each session asks
# for one more than last time rather than just mirroring it. Set to 0 to prescribe
# exactly what was logged. NB: reps ratchet up indefinitely -- when a lift reaches
# the top of its useful rep range, raise the weight so the count resets.
REP_PROGRESSION = 1

# Once the rep target would reach REP_CEILING, add weight instead and drop back to
# REPS_AFTER_INCREASE. That's what stops reps climbing forever.
REP_CEILING = 11
REPS_AFTER_INCREASE = 6

# How much a single increment is, per equipment type:
BARBELL_STEP_KG = 2.5        # smallest pair of plates
DUMBBELL_STEP_KG = 2.0       # next dumbbell up the rack
PLATE_STEP_KG = 5.0          # loaded by hand, one plate at a time (hip thrust)
KG_PER_LB = 0.45359237

# Selectorised machines (Matrix) are pinned in POUNDS: 10 lb main plates plus add-on
# weights that sit between them, so the achievable resolution is finer than a plate.
# The add-ons are the ~1 kg / ~2 kg pieces on these machines, i.e. 2.5 lb (1.13 kg)
# and 5 lb (2.27 kg) -- which is also what every logged value fits best, to within
# the kg rounding.
MACHINE_GRID_LB = 2.5
# Resolution is not the same as a sensible jump: one 2.5 lb notch off a 210 lb calf
# raise would not cost a single rep, while +10 lb on a 32 lb cable stack is a 30%
# rise. So the increase scales with the load and is then snapped to the grid, with
# one grid notch as the floor.
MACHINE_STEP_FRACTION = 0.05

# Equipment type per exercise, where the FIT enum name doesn't give it away. Values:
# "barbell", "dumbbell", "plate" (PLATE_STEP_KG), "lb_stack", "assist" (an lb stack
# where progress means LESS weight). Anything unlisted is classified from its enum.
EQUIPMENT: dict[str, str] = {
    "Hip Thrusts (Smith)": "plate",   # enum says barbell, but loaded in 5 kg plates
    "Pull-ups": "assist",             # assisted machine: less help = progress
    "Chin-ups": "assist",
    "Farmer's Walk": "dumbbell",      # held dumbbells/handles; enum name doesn't say
}

# Garmin switches to the WEIGHTED_* FIT name once a set carries a weight, so a
# template step naming the plain variant disagrees with what the watch will log.
# EXERCISE_MAP below stays plain (one source of truth) and strength_step() swaps in
# the weighted twin whenever the step actually has a weight. Table is generated
# from the garmin-fit-sdk enums; see rewrite_sets.py, which applies the same rule
# to already-logged activities.
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fit_weighted_variants.json")) as _fh:
    WEIGHTED_VARIANTS: dict[str, dict[str, str]] = json.load(_fh)["variants"]

# Exercises where the logged kg is ASSISTANCE, not load: lower is harder. They are
# aggregated the opposite way by --pull-weights (least assist = best set) and must
# never be pulled with "heaviest wins", which would pick the easiest set and ratchet
# the assist upward every run.
ASSIST_EXERCISES = {"Pull-ups", "Chin-ups"}

# Exercises that get a single-rep max attempt straight after the warm-up, before the
# working sets. The weight is the HARDEST you've logged -- for an assisted lift that
# means the LEAST assistance, so the lowest number. Unlike the working-set lookup this
# ignores MIN_WORKING_SET_REPS, since a max attempt is by definition a short set.
# Empty: the pull-up max attempt was dropped 2026-09-17. Add a name back here to
# reinstate the single, in any session that exercise appears in.
ONE_REP_MAX_EXERCISES: set[str] = set()

# Exercises prescribed by TIME rather than reps: for these the "reps" column in
# SESSIONS holds SECONDS per set, and the step ends on a timer instead of a rep
# count. A carry is a duration, not a rep count, which is why this exists.
TIMED_EXERCISES: set[str] = {"Farmer's Walk"}

NO_TARGET = {
    "workoutTargetTypeId": TargetType.NO_TARGET,
    "workoutTargetTypeKey": "no.target",
    "displayOrder": 1,
}

# Garmin stores strength weight in KILOGRAMS (validated against a real export).
# Set unitKey to "pound" (unitId 7) if you'd rather log in lb.
WEIGHT_UNIT_KG = {"unitId": 8, "unitKey": "kilogram"}

# Maps each exercise to Garmin's FIT (category, exerciseName) enum strings, which
# populate Exercise settings -> Exercise on the watch (a free-text name lands in
# Notes instead). All validated against the FIT SDK enums. Two are the closest
# available match rather than exact (the FIT SDK snapshot has no entry for them):
#   Rear Delt Cable Flies    -> category FLYE with no exerciseName, i.e. the
#                               generic "Fly". Every one of the 13 flye_exercise_name
#                               enums is a specific variant (cable_crossover,
#                               dumbbell_flye, single_arm_standing_cable_reverse_flye,
#                               ...) and none is a plain two-arm cable rear delt fly.
#   Smith Bent-Over Row      -> REVERSE_GRIP_BARBELL_ROW, relabelled "Bent-Over Row"
#                               (two-arm; Garmin's newer plain barbell bent-over row
#                                postdates this SDK, so no name is available for it)
#   Pull-ups / Chin-ups      -> BAND_ASSISTED_PULL_UP / BAND_ASSISTED_CHIN_UP, both
#                               relabelled "Assisted ...". The FIT pull_up_exercise_name
#                               enum has NO plain assisted or machine-assisted entry —
#                               band-assisted is the only assisted variant, so it stands
#                               in for machine (weight-stack) assistance too.
#
# Assisted-lift weight caveat: Garmin's weight field is unsigned, so an assist load
# is logged as if it were resistance. The 32 kg on Pull-ups is the assist, i.e. it
# INFLATES Connect's volume/tonnage totals rather than reducing them. Lower numbers
# = harder set for these two exercises, opposite to every other row in SESSIONS.
EXERCISE_MAP: dict[str, tuple[str, str | None]] = {
    "Calf Raises (partial ROM)": ("CALF_RAISE", "STANDING_CALF_RAISE"),
    "Squats (Smith / hack / lunges)": ("SQUAT", "BARBELL_BACK_SQUAT"),
    "Bulgarian Split Squats (Smith)": ("LUNGE", "BARBELL_BULGARIAN_SPLIT_SQUAT"),
    "Leg Extensions": ("CRUNCH", "LEG_EXTENSIONS"),
    "Lying Leg Curls": ("LEG_CURL", "LEG_CURL"),
    "Hip Adduction": ("HIP_STABILITY", "STANDING_ADDUCTION"),  # -> WEIGHTED_ when loaded
    "Leg Press": ("SQUAT", "LEG_PRESS"),
    "Incline Press (Smith)": ("BENCH_PRESS", "INCLINE_SMITH_MACHINE_BENCH_PRESS"),
    "Seated Shoulder Press": ("SHOULDER_PRESS", "SEATED_BARBELL_SHOULDER_PRESS"),
    "DB Side Lateral Raises": ("LATERAL_RAISE", "DUMBBELL_LATERAL_RAISE"),
    "Cable Flies": ("FLYE", "CABLE_CROSSOVER"),
    "Skull Crushers": ("TRICEPS_EXTENSION", "LYING_EZ_BAR_TRICEPS_EXTENSION"),
    "Upright Rows": ("SHRUG", "BARBELL_UPRIGHT_ROW"),
    "Single-Arm Cable Push-downs": ("TRICEPS_EXTENSION", "TRICEPS_PRESSDOWN"),
    "Hanging Leg Raises": ("LEG_RAISE", "HANGING_LEG_RAISE"),
    "Decline Crunches": ("CRUNCH", "CRUNCH"),
    "Pull-ups": ("PULL_UP", "BAND_ASSISTED_PULL_UP"),                            # assisted; see note
    "Seated Single-Arm Cable Pulldown": ("PULL_UP", "LAT_PULLDOWN"),             # approx
    "Smith Machine Bent-Over Row": ("ROW", "REVERSE_GRIP_BARBELL_ROW"),          # two-arm; see note
    "Chin-ups": ("PULL_UP", "BAND_ASSISTED_CHIN_UP"),                            # assisted; see note
    "Standing Overhead Press": ("SHOULDER_PRESS", "OVERHEAD_BARBELL_PRESS"),
    "Preacher Curls": ("CURL", "ONE_ARM_PREACHER_CURL"),
    "Rear Delt Cable Flies": ("FLYE", None),   # generic "Fly" — see note above
    "Romanian Deadlifts": ("DEADLIFT", "BARBELL_STRAIGHT_LEG_DEADLIFT"),
    "Hip Thrusts (Smith)": ("HIP_RAISE", "BARBELL_HIP_THRUST_WITH_BENCH"),
    "Reverse Lunges": ("LUNGE", "BARBELL_REVERSE_LUNGE"),
    "Seated Leg Curls": ("LEG_CURL", "LEG_CURL"),
    "Squat (leg-extension alt.)": ("SQUAT", "BARBELL_HACK_SQUAT"),
    "Banded Glute / Back Extensions": ("HYPEREXTENSION", "STATIC_BACK_EXTENSION"),
    # --- Full_Body_Workout_Plan.pdf (2-day full-body routine) --------------------
    # All exact FIT matches except the chest-supported row, where the only enum is
    # the dumbbell version (Garmin has no chest-supported MACHINE row).
    "Incline Barbell Bench Press": ("BENCH_PRESS", "INCLINE_BARBELL_BENCH_PRESS"),
    "Chest-Supported Machine Row": ("ROW", "CHEST_SUPPORTED_DUMBBELL_ROW"),      # approx
    "Standing DB Shoulder Press": ("SHOULDER_PRESS", "OVERHEAD_DUMBBELL_PRESS"),
    "Barbell Back Squats": ("SQUAT", "BARBELL_BACK_SQUAT"),
    "Conventional Barbell Deadlifts": ("DEADLIFT", "BARBELL_DEADLIFT"),
    "Flat Barbell Bench Press": ("BENCH_PRESS", "BARBELL_BENCH_PRESS"),
    "Heavy Lat Pulldowns": ("PULL_UP", "LAT_PULLDOWN"),
    "Seated Cable Row": ("ROW", "SEATED_CABLE_ROW"),
    "DB Bicep Curls": ("CURL", "DUMBBELL_BICEPS_CURL"),
    "Tricep Rope Pushdowns": ("TRICEPS_EXTENSION", "ROPE_PRESSDOWN"),
    "Hanging Knee Raises": ("LEG_RAISE", "HANGING_KNEE_RAISE"),
    # Plain CRUNCH here: strength_step() derives WEIGHTED_CRUNCH once the step has a
    # weight, which is how the watch logs it too.
    "Weighted Crunches": ("CRUNCH", "CRUNCH"),
    "Standing Calf Raises": ("CALF_RAISE", "STANDING_CALF_RAISE"),
    # --- shoulder-sparing swaps (FB Express (Low shoulder)) ----------------------
    # All four are exact FIT matches. Chosen to keep the shoulder out of the ranges
    # that aggravate an impingement/cuff irritation: no vertical overhead press, no
    # loaded end-range hang, no abduction past ~60 degrees.
    "Flat DB Press": ("BENCH_PRESS", "NEUTRAL_GRIP_DUMBBELL_BENCH_PRESS"),
    "High-Incline DB Press": ("BENCH_PRESS", "NEUTRAL_GRIP_DUMBBELL_INCLINE_BENCH_PRESS"),
    "Face Pulls": ("ROW", "FACE_PULL"),
    # Plain LAT_PULLDOWN deliberately, not CLOSE_GRIP_LAT_PULLDOWN: it shares the
    # enum with the other two pulldowns below, so --pull-weights finds the existing
    # pulldown history instead of starting from nothing. Grip is in the label.
    "Neutral-Grip Lat Pulldown": ("PULL_UP", "LAT_PULLDOWN"),
    # --- movement-pattern gap fillers (carry / rotate / anti-extension) ----------
    # All three are exact FIT matches. Added because the full-body sessions covered
    # squat, hinge, push and pull but nothing else: no loaded carry, no rotation.
    "Farmer's Walk": ("CARRY", "FARMERS_WALK"),
    "Cable Woodchop": ("CHOP", "CABLE_WOODCHOP"),
    # Dead Bug is mapped but not currently in any session (dropped 2026-09-17: FB 2
    # Power's hanging knee raises already cover anti-extension). Kept so dropping it
    # back into SESSIONS is a one-line change.
    "Dead Bug": ("HIP_STABILITY", "DEAD_BUG"),   # -> WEIGHTED_DEAD_BUG when loaded
}

# Optional custom step labels for exercises Garmin has no exact entry for. Set as
# stepName *in addition to* the category/exerciseName above, so the step reads the
# way you want while still carrying a sensible underlying exercise. If the watch
# shows the underlying name instead of the label, we can drop the exerciseName for
# that exercise to force the label through.
DISPLAY_NAME_OVERRIDES: dict[str, str] = {
    "Romanian Deadlifts": "Romanian Deadlift",  # Garmin has no barbell RDL; mapped to straight-leg deadlift
    "Smith Machine Bent-Over Row": "Bent-Over Row",  # underlying enum is reverse-grip; label reads cleanly
    "Rear Delt Cable Flies": "Fly",  # generic FLYE category, no specific enum
    "Pull-ups": "Assisted Pull-up",  # underlying enum is band-assisted; label covers machine assist
    "Chin-ups": "Assisted Chin-up",
    "Chest-Supported Machine Row": "Chest-Supported Row",  # enum is the dumbbell version
    "High-Incline DB Press": "High-Incline DB Press",  # enum name doesn't say "high"
    "Neutral-Grip Lat Pulldown": "Neutral-Grip Lat Pulldown",  # enum is grip-agnostic
}

# --- the plan: (exercise name, sets, reps, weight_kg) --------------------------
# 4-day upper/lower split. The original day-3 sessions have been folded in:
# Lower 3 -> spread across Lower 1 / Lower 2, Upper 3 -> across Upper 1 / Upper 2.
# Exact duplicates were dropped: Hip Adduction & Seated Leg Curl (already in the
# lower days), Side Lateral Raises & Preacher Curls (already in the upper days).
SESSIONS: dict[str, list[tuple[str, int, int, float]]] = {
    "LWS Lower 1": [
        ("Calf Raises (partial ROM)", 3, 10, 0),
        ("Squats (Smith / hack / lunges)", 3, 10, 20),
        ("Bulgarian Split Squats (Smith)", 6, 10, 25),  # 3 per side
        ("Leg Extensions", 3, 10, 10),
        ("Lying Leg Curls", 3, 10, 14),
        ("Hip Adduction", 3, 10, 10),
        ("Leg Press", 3, 10, 15),                       # from Lower 3
    ],
    "LWS Upper 1": [
        ("Incline Press (Smith)", 3, 10, 20),
        ("Seated Shoulder Press", 3, 10, 12),
        ("DB Side Lateral Raises", 3, 10, 5),
        ("Skull Crushers", 3, 10, 12.5),
        ("Pull-ups", 3, 10, 32),        # 32 kg = assist, not added load (see note above)
        ("Upright Rows", 3, 10, 20),                    # from Upper 3
        ("Tricep Rope Pushdowns", 3, 10, 0),            # replaced the reverse-grip barbell row
        ("Hanging Leg Raises", 3, 10, 0),
        ("Decline Crunches", 3, 10, 0),
    ],
    # LWS Lower 2 / Upper 2 removed (unused). Their exercises stay in EXERCISE_MAP:
    # several are still WEIGHT_FROM sources for the full-body sessions, and the map is
    # what lets --pull-weights find their logged history.
    # --- Full_Body_Workout_Plan.pdf: 2-day full-body routine ---------------------
    # Sets from the PDF (3 throughout); reps fixed at 10 (PDF says 6-10) and rest at
    # the same 90 s as the LWS sessions (the PDF asks for 2-3 min). Weights
    # are NOT written here — every entry is 0 and inherited from the LWS exercise
    # named in WEIGHT_FROM below, so these sessions track the LWS numbers (and pick
    # up --pull-weights automatically).
    "FB 1 Express": [
        ("Leg Press", 3, 10, 0),                        # first exercise
        ("Hip Thrusts (Smith)", 3, 10, 0),
        ("Bulgarian Split Squats (Smith)", 6, 10, 0),   # 3 per side; the lunge pattern
        ("Romanian Deadlifts", 3, 10, 40),              # barbell; holds the RDL table weight
        ("Flat Barbell Bench Press", 3, 10, 0),
        ("Chest-Supported Machine Row", 3, 10, 0),
        ("Standing DB Shoulder Press", 3, 10, 0),
        ("Pull-ups", 3, 10, 0),                          # added, at the end
        ("Cable Woodchop", 4, 10, 0),                    # 2 per side; the rotate pattern
        # TIMED: the 60 is SECONDS per set, not reps (see TIMED_EXERCISES). Last on
        # purpose -- it wrecks grip for everything that would follow it.
        ("Farmer's Walk", 3, 60, 0),
    ],
    # Shoulder-sparing copy of FB 1 Express, for training around a shoulder that
    # doesn't tolerate overhead work (irritated 2026-09-01 doing pull-ups). Same
    # legs and same posterior-chain work; the four upper-body pressing/pulling
    # movements are swapped:
    #   Incline Barbell Bench Press -> Flat DB Press          (neutral grip, no fixed bar path)
    #   Standing DB Shoulder Press  -> High-Incline DB Press  (no vertical overhead press)
    #   DB Side Lateral Raises      -> Face Pulls             (no abduction past ~60 deg)
    #   Pull-ups                    -> Neutral-Grip Lat Pulldown  (no loaded end-range hang)
    # Weights inherit exactly as FB 1 Express does. Upper-body loads are NOT deloaded
    # here -- knock ~30% off the presses and the pulldown on the watch while the
    # shoulder is settling, and stay inside a pain-free range.
    "FB Express (Low shoulder)": [
        ("Hip Thrusts (Smith)", 3, 10, 0),
        ("Romanian Deadlifts", 3, 10, 40),           # barbell; holds the RDL table weight
        ("Flat DB Press", 3, 10, 0),
        ("Chest-Supported Machine Row", 3, 10, 0),   # chest pad keeps the shoulder quiet
        ("Leg Press", 3, 10, 0),
        ("High-Incline DB Press", 3, 10, 0),
        ("Face Pulls", 3, 12, 10),                   # 12 reps: cuff work, not a heavy set
        ("Neutral-Grip Lat Pulldown", 3, 10, 0),
    ],
    "FB 2 Power": [
        ("Standing Calf Raises", 3, 10, 0),             # added, first exercise
        ("Barbell Back Squats", 3, 10, 0),
        ("Conventional Barbell Deadlifts", 3, 10, 0),
        # Was Leg Press, a second squat pattern after the back squat. Swapped for the
        # lunge, same movement as FB 1 Express so there's one weight to maintain.
        # 4 sets here against Express's 6: this session already carries the back
        # squat and the deadlift, so the lunge is accessory work rather than the
        # main knee-dominant lift.
        ("Bulgarian Split Squats (Smith)", 4, 10, 0),   # 2 per side; the lunge pattern
        ("Hip Adduction", 3, 10, 0),                    # added, after the leg press
        ("Flat Barbell Bench Press", 3, 10, 0),
        # Heavy Lat Pulldowns removed here -- the session's second vertical pull, with
        # Pull-ups at the end. Its replacement (the carry) is last, not in this slot.
        ("Seated Cable Row", 3, 10, 0),
        ("Standing DB Shoulder Press", 3, 10, 0),       # was barbell overhead press
        ("DB Bicep Curls", 3, 10, 0),
        # Was Weighted Crunches: the same trunk flexion as the hanging knee raises
        # two rows down. Swapped for the rotate pattern.
        ("Cable Woodchop", 4, 10, 0),                   # 2 per side
        ("Tricep Rope Pushdowns", 3, 10, 0),
        ("Hanging Leg Raises", 3, 10, 0),               # was knee raises
        ("Pull-ups", 3, 10, 0),                         # added, after the knee raises
        # The carry that replaced Heavy Lat Pulldowns. Last on purpose: 3 x 60 s of
        # loaded carry ruins grip for the row and the pull-ups, which used to follow
        # it. TIMED: the 60 is SECONDS per set, not reps.
        ("Farmer's Walk", 3, 60, 0),
    ],
}

# Where each full-body exercise takes its weight from: {new exercise: LWS exercise}.
# Resolved at build time against the LWS table (or the pulled weight, when
# --pull-weights is on), so there is one number per movement to maintain.
#
# Several are deliberate approximations because the LWS plan has no equivalent
# lift — flagged inline. Check these against what you actually lift:
WEIGHT_FROM: dict[str, str] = {
    "Incline Barbell Bench Press": "Incline Press (Smith)",     # Smith -> free bar
    "Chest-Supported Machine Row": "Smith Machine Bent-Over Row",
    "Leg Press": "Leg Press",                                   # same movement
    "Standing DB Shoulder Press": "Seated Shoulder Press",      # seated barbell -> standing DB
    "Barbell Back Squats": "Squats (Smith / hack / lunges)",    # Smith/hack -> free bar
    "Conventional Barbell Deadlifts": "Romanian Deadlifts",     # RDL -> conventional pull
    "Flat Barbell Bench Press": "Incline Press (Smith)",        # incline Smith -> flat free bar
    "Heavy Lat Pulldowns": "Seated Single-Arm Cable Pulldown",   # single-arm -> two-arm bar
    "Seated Cable Row": "Smith Machine Bent-Over Row",
    "DB Bicep Curls": "Preacher Curls",
    "Tricep Rope Pushdowns": "Single-Arm Cable Push-downs",     # single-arm -> two-arm rope
    "Hanging Knee Raises": "Hanging Leg Raises",                # bodyweight either way
    "Weighted Crunches": "Decline Crunches",                    # same machine/cable stack
    # NB: the LWS calf raise is deliberately PARTIAL range of motion, which supports
    # a heavier load than a full-ROM standing raise. Inheriting it is a starting
    # point, not a validated weight.
    "Standing Calf Raises": "Calf Raises (partial ROM)",
    # Self-referencing: these full-body steps carry weight 0 in the table above, so
    # they need pointing at the LWS entry that holds the real number for the very
    # same movement. Same value, one place to change it.
    "Hip Adduction": "Hip Adduction",
    "Pull-ups": "Pull-ups",
    "Bulgarian Split Squats (Smith)": "Bulgarian Split Squats (Smith)",
    "DB Side Lateral Raises": "DB Side Lateral Raises",
    # Shoulder swaps. Both DB presses take the Smith incline press number, same as
    # the barbell presses they stand in for -- a starting point, not a validated
    # dumbbell weight (a pair of DBs at the Smith bar's load is a much harder set).
    "Flat DB Press": "Incline Press (Smith)",
    "High-Incline DB Press": "Incline Press (Smith)",
    # Shares the LAT_PULLDOWN enum with the pulldowns above, so --pull-weights fills
    # it from their history; this only covers the no-pull case, same as Heavy Lat
    # Pulldowns. Face Pulls has no equivalent lift in the plan at all, so it carries
    # a literal placeholder weight in the table instead of inheriting one.
    "Neutral-Grip Lat Pulldown": "Seated Single-Arm Cable Pulldown",
}

# Hand-set warm-up weights, for movements whose logged history offers no usable
# opener (a single set ever recorded, or every set at the same weight). These WIN
# over the pulled warm-up: they are a deliberate instruction, not a default. Drop an
# entry once the history has a real lighter opener of its own.
WARMUP_WEIGHTS: dict[str, float] = {
    "Flat Barbell Bench Press": 20.0,   # history has one 22.5 kg set, no opener
    "Romanian Deadlifts": 20.0,         # nothing logged in the window to open from
    "Conventional Barbell Deadlifts": 20.0,  # logged opener was 40 kg, too heavy to start on
}


# --- recurring calendar schedule ---------------------------------------------
# Which session lands on which weekday. Keys are datetime's weekday numbers,
# Monday = 0 ... Sunday = 6. Garmin has no repeating-workout API, so --schedule
# expands this into one calendar entry per date.
WEEKLY_SCHEDULE: dict[int, str] = {
    1: "FB 1 Express",   # Tuesday
    4: "FB 2 Power",     # Friday
}

# Per-date exceptions, which WIN over WEEKLY_SCHEDULE: a name swaps that day's
# session, None skips the day entirely. Delete an entry once its date is past --
# they are one-offs, not policy.
SCHEDULE_OVERRIDES: dict[str, str | None] = {
    # e.g. "2026-09-08": "FB Express (Low shoulder)",   # swap in shoulder-sparing session
    #      "2026-12-25": None,                          # skip the day
}

# How many weeks --schedule fills by default, counting from today inclusive. Short
# on purpose: re-running is cheap, and unpicking a long stretch of wrong entries
# isn't. Override with `--schedule N`.
SCHEDULE_WEEKS = 4


def strength_step(name: str, reps: int, order: int, weight_kg: float = 0.0) -> ExecutableStep:
    """One working set: do `reps` reps of `name` at `weight_kg`.

    Sets Garmin's FIT category/exerciseName (from EXERCISE_MAP) so the exercise
    shows under Exercise settings -> Exercise. If a name isn't mapped, it falls
    back to a free-text stepName so the exercise is still identifiable.

    weight_kg <= 0 means bodyweight / not set yet: the weight fields are omitted,
    which is how Garmin represents an unweighted step.

    For a name in TIMED_EXERCISES, `reps` is read as SECONDS and the step ends on a
    timer instead of a rep count.
    """
    # For a TIMED_EXERCISES movement `reps` is SECONDS and the step ends on a timer.
    if name in TIMED_EXERCISES:
        end_condition = {"conditionTypeId": ConditionType.TIME, "conditionTypeKey": "time",
                         "displayOrder": 2, "displayable": True}
    else:
        end_condition = {"conditionTypeId": ConditionType.REPS, "conditionTypeKey": "reps",
                         "displayOrder": 2, "displayable": True}
    step = ExecutableStep(
        stepOrder=order,
        stepType={"stepTypeId": StepType.INTERVAL, "stepTypeKey": "interval", "displayOrder": 3},
        endCondition=end_condition,
        endConditionValue=float(reps),
        targetType=NO_TARGET,
    )
    mapping = EXERCISE_MAP.get(name)
    if mapping is not None:
        category, exercise = mapping
        step.category = category
        if exercise is not None:
            if weight_kg and weight_kg > 0:
                # Weighted step -> use the WEIGHTED_ twin if the FIT enum has one,
                # matching what the watch records for a loaded set.
                exercise = WEIGHTED_VARIANTS.get(category, {}).get(exercise, exercise)
            step.exerciseName = exercise
        # exercise None: category only, i.e. the generic exercise for that
        # category (e.g. FLYE -> plain "Flye"), which is all Garmin offers when
        # no specific enum name fits.
    else:
        step.stepName = name  # unmapped: keep a readable label
    override = DISPLAY_NAME_OVERRIDES.get(name)
    if override is not None:
        step.stepName = override  # custom label on top of the mapped exercise
    if weight_kg and weight_kg > 0:
        step.weightValue = float(weight_kg)   # kilograms
        step.weightUnit = WEIGHT_UNIT_KG
    return step


def warmup_step(name: str, reps: int, order: int, weight_kg: float = 0.0) -> ExecutableStep:
    """First set of an exercise, marked as a warm-up rather than a working set.

    Identical to strength_step apart from the step type, so the exercise, label and
    weighted-variant handling all behave the same way.
    """
    step = strength_step(name, reps, order, weight_kg)
    step.stepType = {"stepTypeId": StepType.WARMUP, "stepTypeKey": "warmup", "displayOrder": 1}
    return step


def lap_button_rest(order: int) -> ExecutableStep:
    """Rest between exercises: ends when the lap button is pressed."""
    return ExecutableStep(
        stepOrder=order,
        stepType={"stepTypeId": StepType.REST, "stepTypeKey": "rest", "displayOrder": 8},
        endCondition={"conditionTypeId": ConditionType.LAP_BUTTON, "conditionTypeKey": "lap.button",
                      "displayOrder": 3, "displayable": True},
        targetType=NO_TARGET,
    )


def timed_rest(seconds: int, order: int) -> ExecutableStep:
    """Fixed rest between exercises."""
    return ExecutableStep(
        stepOrder=order,
        stepType={"stepTypeId": StepType.REST, "stepTypeKey": "rest", "displayOrder": 8},
        endCondition={"conditionTypeId": ConditionType.TIME, "conditionTypeKey": "time",
                      "displayOrder": 2, "displayable": True},
        endConditionValue=float(seconds),
        targetType=NO_TARGET,
    )


def table_weight(ex_name: str) -> float:
    """The weight SESSIONS prescribes for an exercise, first occurrence wins."""
    for exercises in SESSIONS.values():
        for nm, _sets, _reps, weight_kg in exercises:
            if nm == ex_name:
                return weight_kg
    return 0.0


def inherited_weights(overrides: dict[str, float]) -> dict[str, float]:
    """Resolve WEIGHT_FROM: {inheriting exercise: kg taken from its source lift}.

    The source's pulled weight wins over its table default, so the full-body
    sessions follow whatever the LWS sessions are currently prescribing.
    """
    out: dict[str, float] = {}
    for target, source in WEIGHT_FROM.items():
        kg = overrides.get(source, table_weight(source))
        if kg and kg > 0:
            out[target] = kg
    return out


def equipment_kind(ex_name: str) -> str:
    """Which increment applies to this exercise. EQUIPMENT wins, else read the enum."""
    if ex_name in EQUIPMENT:
        return EQUIPMENT[ex_name]
    pair = EXERCISE_MAP.get(ex_name)
    enum = (pair[1] or "") if pair else ""
    if "DUMBBELL" in enum:
        return "dumbbell"          # checked first: CHEST_SUPPORTED_DUMBBELL_ROW etc.
    if "BARBELL" in enum or "SMITH" in enum or "EZ_BAR" in enum:
        return "barbell"
    return "lb_stack"


def next_weight(ex_name: str, kg: float) -> float:
    """One increment up from `kg` for this exercise's equipment.

    Machine stacks are pinned in POUNDS even though the weight gets logged in kg, so
    the result isn't a round kg figure. The current weight is snapped to the nearest
    achievable pin+add-on combination first, since a kg-entered value rarely lands
    exactly on one. For "assist" the step goes DOWN, floored at zero.
    """
    kind = equipment_kind(ex_name)
    if kind == "barbell":
        return round(kg + BARBELL_STEP_KG, 1)
    if kind == "dumbbell":
        return round(kg + DUMBBELL_STEP_KG, 1)
    if kind == "plate":
        return round(kg + PLATE_STEP_KG, 1)

    pin_lb = round((kg / KG_PER_LB) / MACHINE_GRID_LB) * MACHINE_GRID_LB
    step_lb = max(MACHINE_GRID_LB,
                  round(pin_lb * MACHINE_STEP_FRACTION / MACHINE_GRID_LB) * MACHINE_GRID_LB)
    pin_lb = max(0.0, pin_lb - step_lb) if kind == "assist" else pin_lb + step_lb
    return round(pin_lb * KG_PER_LB, 1)


def inherited_reps(rep_overrides: dict[str, int]) -> dict[str, int]:
    """Resolve WEIGHT_FROM for rep counts: reps follow the weight they were done at.

    Inheriting a source's weight without its reps would prescribe someone else's
    load at an unrelated rep target.
    """
    return {target: rep_overrides[source]
            for target, source in WEIGHT_FROM.items() if source in rep_overrides}


def build_session(name: str, exercises: list[tuple[str, int, int, float]],
                  weight_overrides: dict[str, float] | None = None,
                  warmup_overrides: dict[str, float] | None = None,
                  rep_overrides: dict[str, int] | None = None,
                  max_attempts: dict[str, float] | None = None) -> BaseWorkout:
    """Build one strength workout.

    Every exercise opens with a WARM-UP set, then the remaining sets as working sets
    in a repeat group. The warm-up weight comes from warmup_overrides (what you last
    opened that movement with); with nothing to go on it falls back to the working
    weight, so the set is still marked warm-up but isn't a lighter load.

    rep_overrides sets the WORKING-set rep target to what was actually logged at that
    weight. The warm-up keeps the table's rep count: it isn't a performance target.
    """
    weight_overrides = weight_overrides or {}
    warmup_overrides = warmup_overrides or {}
    rep_overrides = rep_overrides or {}
    max_attempts = max_attempts or {}
    steps: list = []
    order = 1
    extra_sets = 0
    for i, (ex_name, sets, reps, weight_kg) in enumerate(exercises):
        wk = weight_overrides.get(ex_name, weight_kg)  # pulled/inherited weight wins
        warm_kg = warmup_overrides.get(ex_name, wk)
        # A timed step's "reps" are seconds, so a pulled rep count must not replace them.
        work_reps = reps if ex_name in TIMED_EXERCISES else rep_overrides.get(ex_name, reps)
        rest = REST_OVERRIDES.get(ex_name, REST_BETWEEN_SETS_SECS)

        # Set 1: warm-up, then its rest.
        steps.append(warmup_step(ex_name, reps, order, warm_kg))
        order += 1
        steps.append(timed_rest(rest, order))
        order += 1

        # Optional single max attempt, before the working sets while fresh.
        attempt_kg = max_attempts.get(ex_name) if ex_name in ONE_REP_MAX_EXERCISES else None
        if attempt_kg:
            steps.append(strength_step(ex_name, 1, order, attempt_kg))
            order += 1
            steps.append(timed_rest(rest, order))
            order += 1
            extra_sets += 1

        # Remaining sets: working sets in a repeat group.
        inner = [
            strength_step(ex_name, work_reps, 1, wk),
            timed_rest(rest, 2),
        ]
        group = create_repeat_group(iterations=max(1, sets - 1),
                                   workout_steps=inner, step_order=order)
        # Skip the 90s rest on the final set, so the only pause before the next
        # exercise is the lap-button rest (and the last exercise ends on a set).
        group.skipLastRestStep = True
        steps.append(group)
        order += 1
        # lap-button rest between exercises (not after the last one)
        if i < len(exercises) - 1:
            steps.append(lap_button_rest(order))
            order += 1

    # Rough duration estimate: ~40 s per set (a timed set takes its own seconds
    # instead), plus the between-set rests (the last
    # of each group is skipped), plus a floor for each open-ended between-exercise
    # rest. Understating this makes Garmin's predicted finish time useless.
    est = sum(s * (r if nm in TIMED_EXERCISES else 40)
              + (s - 1) * REST_OVERRIDES.get(nm, REST_BETWEEN_SETS_SECS)
              for nm, s, r, _ in exercises)
    est += max(0, len(exercises) - 1) * EST_REST_BETWEEN_EXERCISES_SECS
    est += extra_sets * (40 + REST_BETWEEN_SETS_SECS)  # max attempts and their rests
    # (max attempts only ever apply to heavy compounds, which keep the default rest)

    return BaseWorkout(
        workoutName=name,
        sportType={"sportTypeId": SportType.STRENGTH_TRAINING,
                   "sportTypeKey": "strength_training", "displayOrder": 5},
        estimatedDurationInSecs=int(est),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": SportType.STRENGTH_TRAINING,
                           "sportTypeKey": "strength_training", "displayOrder": 5},
                workoutSteps=steps,
            )
        ],
    )


def schedule_plan(weeks: int, start: dt.date | None = None) -> list[tuple[str, str]]:
    """[(YYYY-MM-DD, session name)] for `weeks` weeks from `start`, inclusive.

    WEEKLY_SCHEDULE gives the recurring pattern; SCHEDULE_OVERRIDES wins on the
    dates it names (None there means "no workout that day"). An override on a
    weekday the pattern doesn't cover still counts -- that's how a one-off lands.
    """
    start = start or dt.date.today()
    plan: list[tuple[str, str]] = []
    for offset in range(weeks * 7):
        day = start + dt.timedelta(days=offset)
        iso = day.isoformat()
        name = SCHEDULE_OVERRIDES.get(iso, WEEKLY_SCHEDULE.get(day.weekday()))
        if name is None:
            continue
        if name not in SESSIONS:
            raise SystemExit(f"schedule names an unknown workout: {name!r}\n"
                             f"available: {sorted(SESSIONS)}")
        plan.append((iso, name))
    return plan


def existing_schedule(garmin: Garmin, dates: list[str]) -> dict[str, list[dict]]:
    """{date: [scheduled workout items]} for every month the dates fall in.

    One request per month rather than per date. Only itemType "workout" entries
    are returned -- the calendar also carries activities, events and weigh-ins.
    """
    out: dict[str, list[dict]] = {}
    months = sorted({(int(d[:4]), int(d[5:7])) for d in dates})
    for year, month in months:
        payload = garmin.get_scheduled_workouts(year, month)
        for item in (payload.get("calendarItems") or []):
            if item.get("itemType") != "workout":
                continue
            out.setdefault(item.get("date"), []).append(item)
    return out


def apply_schedule(garmin: Garmin, plan: list[tuple[str, str]],
                   dry_run: bool = False) -> None:
    """Put each (date, session) on the Garmin calendar, idempotently.

    A date already carrying the right workoutId is left alone, so re-running adds
    only what's missing. A date carrying a DIFFERENT session of ours is replaced
    (Garmin has no reschedule-in-place, so that's unschedule + schedule). Anything
    else on the day -- another plan's workout, a Garmin-managed one -- is reported
    and left untouched: it isn't ours to move.
    """
    ids = {w.get("workoutName"): w.get("workoutId") for w in garmin.get_workouts(limit=200)}
    missing = sorted({nm for _d, nm in plan if nm not in ids})
    if missing:
        raise SystemExit(f"not uploaded to Garmin yet: {missing}\n"
                         f"upload them first (drop --no-upload), then schedule.")

    current = existing_schedule(garmin, [d for d, _nm in plan])
    for date_str, name in plan:
        want_id = ids[name]
        same = [it for it in current.get(date_str, []) if it.get("workoutId") == want_id]
        if same:
            print(f"  {date_str}  {name:28} already scheduled")
            continue
        # Ours, but the wrong session for this date -> replace it.
        stale = [it for it in current.get(date_str, [])
                 if it.get("title") in SESSIONS and not it.get("protectedWorkoutSchedule")]
        foreign = [it for it in current.get(date_str, [])
                   if it.get("title") not in SESSIONS]
        for it in foreign:
            print(f"  {date_str}  note: '{it.get('title')}' also scheduled, left alone")
        if dry_run:
            was = f"  (replaces '{stale[0].get('title')}')" if stale else ""
            print(f"  {date_str}  {name:28} would schedule{was}")
            continue
        for it in stale:
            garmin.unschedule_workout(it.get("id"))
            print(f"  {date_str}  unscheduled '{it.get('title')}'")
        result = garmin.schedule_workout(want_id, date_str)
        print(f"  {date_str}  {name:28} scheduled "
              f"(scheduleId {result.get('workoutScheduleId')})")


def connect() -> Garmin:
    """Log in, preferring the cached tokens in TOKENSTORE.

    GARMIN_EMAIL / GARMIN_PASSWORD are only needed when there are no valid
    cached tokens (first run, or after they expire).
    """
    garmin = Garmin(os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD"),
                    prompt_mfa=lambda: input("MFA code: ").strip())
    garmin.login(TOKENSTORE)
    return garmin


def _base_exercise(name: str | None) -> str | None:
    """Strip Garmin's WEIGHTED_ prefix so both variants of a move compare equal.

    The watch switches to the WEIGHTED_* FIT name once a weight is on the set
    (WEIGHTED_STANDING_ADDUCTION vs STANDING_ADDUCTION) but not consistently, so
    a lookup keyed on the exact name misses half the history.
    """
    if name and name.startswith("WEIGHTED_"):
        return name[len("WEIGHTED_"):]
    return name


def _wanted_keys() -> dict[tuple, list[str]]:
    """(category, base exerciseName) -> [plan names sharing it], e.g. both leg curls.

    Category-only mappings (exerciseName None) are skipped: they would match any
    logged set in that category whose exercise name is absent, i.e. act as a
    wildcard and pull in a weight from an unrelated movement.
    """
    wanted: dict[tuple, list[str]] = {}
    for ex_name, pair in EXERCISE_MAP.items():
        if pair[1] is None:
            continue
        wanted.setdefault((pair[0], _base_exercise(pair[1])), []).append(ex_name)
    return wanted


def _set_keys(s: dict, wanted: dict[tuple, list[str]]) -> list[tuple]:
    """Which wanted movement(s) a logged set represents, or [] if none/ambiguous.

    Garmin returns `exercises` as a candidate list, so only the highest-`probability`
    entries count -- crediting every entry would attribute the set's weight to
    movements that weren't performed.
    """
    candidates = s.get("exercises") or []
    if not candidates:
        return []
    top = max((e.get("probability") or 0.0) for e in candidates)
    keys = {(e.get("category"), _base_exercise(e.get("name")))
            for e in candidates if (e.get("probability") or 0.0) >= top}
    return [k for k in keys if k in wanted]


def pull_logged_weights(garmin: Garmin, lookback_days: int = WEIGHT_LOOKBACK_DAYS
                        ) -> tuple[dict[str, tuple[float, str, int]],
                                   dict[str, tuple[float, str]],
                                   dict[str, tuple[float, str]]]:
    """One pass over recent strength activities -> (working, warmup, hardest).

    working is {exercise: (kg, session date, reps)}; warmup and hardest are
    {exercise: (kg, date)}. Garmin's set weight is in GRAMS, so we /1000. Bodyweight
    moves (no logged weight) appear in none of them.

    hardest -- the toughest single set logged at ANY rep count (least assist for an
        assisted lift, most weight otherwise), used as the max-attempt target for
        ONE_REP_MAX_EXERCISES.

    working -- the HEAVIEST weight worked across the whole window (date reported is
        the most recent session at it), plus the reps to build on. Reps are the
        MINIMUM across that session's sets at that weight, then the MAXIMUM of those
        per-session figures: every set in a session must reach the number, but one
        weaker session can't hold back a later good one. Only sets of
        MIN_WORKING_SET_REPS or more count: anything shorter is a 1RM/heavy-single
        test and would prescribe a weight that can't be worked with. For
        ASSIST_EXERCISES "heaviest" inverts, since the kg is help given rather than
        load moved.

    warmup -- the FIRST weighted set of the MOST RECENT session containing the
        exercise: what you actually opened that movement with. No rep filter, since
        a warm-up sets no target and is often short.

    One pass because each activity costs an exerciseSets request.
    """
    today = dt.date.today()
    start = (today - dt.timedelta(days=lookback_days)).isoformat()

    def _when(a: dict) -> str:
        return a.get("startTimeLocal") or a.get("startTimeGMT") or ""

    # NB: the API rejects "strength_training" as a filter (it's a sub-type, not a
    # top-level type -> HTTP 400), so we fetch all activities and filter below.
    activities = garmin.get_activities_by_date(start, today.isoformat())
    activities = sorted(activities, key=_when, reverse=True)  # newest first

    wanted = _wanted_keys()
    working: dict[str, tuple[float, str, int]] = {}
    warmup: dict[str, tuple[float, str]] = {}
    hardest: dict[str, tuple[float, str]] = {}

    for act in activities:
        tk = (act.get("activityType") or {}).get("typeKey", "")
        if tk and tk != "strength_training":
            continue
        when = _when(act)[:10]
        try:
            payload = garmin.get_activity_exercise_sets(act.get("activityId"))
        except Exception:
            continue

        # key -> {grams: [reps, ...]} for every working set this session
        sets_by_weight: dict[tuple, dict[float, list[int]]] = {}
        opened_g: dict[tuple, float] = {}  # first weighted set (grams) this session
        for s in (payload.get("exerciseSets") or []):
            if s.get("setType") != "ACTIVE" or not s.get("weight") or s["weight"] <= 0:
                continue
            grams = float(s["weight"])
            for key in _set_keys(s, wanted):
                opened_g.setdefault(key, grams)  # first one wins: sets are in order
                # Hardest ever, at ANY rep count: the max attempt target.
                for ex_name in wanted[key]:
                    kg_here = round(grams / 1000.0, 1)
                    prev_h = hardest.get(ex_name)
                    harder = (kg_here < prev_h[0] if ex_name in ASSIST_EXERCISES
                              else kg_here > prev_h[0]) if prev_h else True
                    if harder:
                        hardest[ex_name] = (kg_here, when)
                reps = s.get("repetitionCount")
                if reps is None or reps < MIN_WORKING_SET_REPS:
                    continue  # 1RM test or unknown, not a working set
                sets_by_weight.setdefault(key, {}).setdefault(grams, []).append(reps)

        # This session's result per movement: the top weight, at the reps EVERY set on
        # it managed. Taking the minimum means one carrying set can't claim a rep
        # target the others didn't reach -- progression has to be earned on all of
        # them. Grouping by weight first keeps the lighter warm-up sets out of it.
        best: dict[tuple, tuple[float, int]] = {}
        for key, by_weight in sets_by_weight.items():
            assist = any(n in ASSIST_EXERCISES for n in wanted[key])
            top = min(by_weight) if assist else max(by_weight)
            best[key] = (top, min(by_weight[top]))

        for key, (grams, reps) in best.items():
            kg = round(grams / 1000.0, 1)
            for ex_name in wanted[key]:
                prev = working.get(ex_name)
                assist = ex_name in ASSIST_EXERCISES
                # best wins; newest date kept on ties
                if prev is None or (kg < prev[0] if assist else kg > prev[0]):
                    working[ex_name] = (kg, when, reps)
                elif kg == prev[0]:
                    working[ex_name] = (prev[0], prev[1], max(prev[2], reps))
        # Activities are newest first, so the first session to mention an exercise
        # is the most recent one -- don't let older sessions overwrite it.
        for key, grams in opened_g.items():
            for ex_name in wanted[key]:
                warmup.setdefault(ex_name, (round(grams / 1000.0, 1), when))

    return working, warmup, hardest


def replace_existing(garmin: Garmin, names: set[str]) -> dict[str, list]:
    """Delete any existing workouts whose name matches one we're about to upload.

    Garmin has no update-in-place, so replacing = delete + re-upload. Match is an
    exact name match. Returns {name: [deleted workout ids]}. NB: the new upload
    gets a fresh workoutId, so any calendar scheduling of the old one is dropped.
    """
    deleted: dict[str, list] = {}
    for w in garmin.get_workouts(limit=200):
        nm = w.get("workoutName")
        if nm in names:
            wid = w.get("workoutId")
            garmin.delete_workout(wid)
            deleted.setdefault(nm, []).append(wid)
    return deleted


def main() -> None:
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    pull = "--pull-weights" in args
    # --no-upload: touch the calendar only, leaving the uploaded workouts (and so
    # their workoutIds, and everything already scheduled) exactly as they are.
    no_upload = "--no-upload" in args
    # --schedule [N weeks]: expand WEEKLY_SCHEDULE / SCHEDULE_OVERRIDES onto the
    # Garmin calendar. Runs AFTER any upload, because replacing a workout gives it
    # a fresh workoutId and drops whatever that workout was scheduled for.
    schedule = "--schedule" in args
    weeks = SCHEDULE_WEEKS
    if schedule:
        i = args.index("--schedule")
        if i + 1 < len(args) and args[i + 1].isdigit():
            weeks = int(args[i + 1])
    # --only "NAME" (repeatable): upload just these workouts. Without it every
    # session is replaced, which drops the calendar scheduling of the ones you
    # didn't mean to touch (replace = delete + re-upload, new workoutId).
    args_only = [args[i + 1] for i, a in enumerate(args) if a == "--only" and i + 1 < len(args)]
    if no_upload and not schedule:
        raise SystemExit("--no-upload does nothing on its own; add --schedule.")

    garmin = None
    overrides: dict[str, float] = {}
    warmups: dict[str, float] = {}
    reps_by_ex: dict[str, int] = {}
    attempts: dict[str, float] = {}
    if pull:
        garmin = connect()
        print(f"Reading strength activities from the last {WEIGHT_LOOKBACK_DAYS} days...\n")
        found, opened, hardest = pull_logged_weights(garmin)
        attempts = {nm: kg for nm, (kg, _d) in hardest.items()
                    if nm in ONE_REP_MAX_EXERCISES and kg > 0}
        for nm, kg in attempts.items():
            print(f"  max attempt: {nm} 1 rep @ {kg:g} kg  (hardest logged, "
                  f"{hardest[nm][1]})")
        if attempts:
            print()
        print(f"Working set = heaviest weight worked in the last {WEIGHT_LOOKBACK_DAYS} days, "
              f"at the reps EVERY set on it reached, +{REP_PROGRESSION};\n"
              f"at {REP_CEILING} reps the weight goes up instead and reps reset to "
              f"{REPS_AFTER_INCREASE}. Warm-up = what you opened with last time.\n")
        print(f"  {'exercise':34} {'warm-up':>8} {'working':>9} {'reps':>8}   last session")
        for ex_name in EXERCISE_MAP:
            warm = opened.get(ex_name)
            if warm and warm[0] > 0:
                warmups[ex_name] = warm[0]
            if ex_name in found:
                kg, when, reps = found[ex_name]
                if kg > 0:
                    target = reps + REP_PROGRESSION
                    note = ""
                    if target >= REP_CEILING:
                        # Topped out on reps -> add weight, restart the rep ladder.
                        up = next_weight(ex_name, kg)
                        note = f"  ^ {kg:g}->{up:g} kg ({equipment_kind(ex_name)})"
                        kg, target = up, REPS_AFTER_INCREASE
                    overrides[ex_name] = kg
                    reps_by_ex[ex_name] = target
                    tag = "  (assist — least help)" if ex_name in ASSIST_EXERCISES else ""
                    ws = f"{warm[0]:g} kg" if warm and warm[0] > 0 else "--"
                    print(f"  {ex_name:34} {ws:>8} {kg:>6.1f} kg "
                          f"{f'{reps}->{target}':>8}   ({when}){tag}{note}")
                else:
                    # A logged set under ~50 g rounds to 0.0 — that's no usable
                    # weight, so don't let it wipe the table default.
                    print(f"  {ex_name:34} {'--':>8} {'--':>9} {'--':>5}   logged as 0.0 kg")
            else:
                print(f"  {ex_name:34} {'--':>8} {'--':>9} {'--':>5}   no recent logged weight")
        print()

    # Full-body sessions carry no weights of their own: fill them from their LWS
    # source lift (after --pull-weights, so they follow the pulled numbers).
    # Inheritance only FILLS GAPS — an exercise's own logged history always wins,
    # otherwise a stand-in weight would override the real thing (e.g. the barbell
    # deadlift taking its RDL's number despite having 56 days of its own sets).
    overrides = {**inherited_weights(overrides), **overrides}
    # WARMUP_WEIGHTS last: a hand-set opener is an instruction and outranks history.
    warmups = {**inherited_weights(warmups), **warmups, **WARMUP_WEIGHTS}
    reps_by_ex = {**inherited_reps(reps_by_ex), **reps_by_ex}

    selected = SESSIONS
    if args_only:
        selected = {n: ex for n, ex in SESSIONS.items() if n in args_only}
        missing = set(args_only) - set(selected)
        if missing:
            raise SystemExit(f"unknown workout name(s): {sorted(missing)}\n"
                             f"available: {sorted(SESSIONS)}")

    workouts = {name: build_session(name, ex, overrides, warmups, reps_by_ex, attempts)
                for name, ex in selected.items()}

    if dry_run:
        if not no_upload:
            for name, wk in workouts.items():
                d = wk.to_dict()
                print(f"{name}: {len(d['workoutSegments'][0]['workoutSteps'])} top-level steps, "
                      f"~{d['estimatedDurationInSecs'] // 60} min")
        if schedule:
            plan = schedule_plan(weeks)
            print(f"\nSchedule for the next {weeks} week(s):")
            if garmin is None:
                garmin = connect()   # reading the calendar needs a login even on a dry run
            apply_schedule(garmin, plan, dry_run=True)
        print("\nDry run OK — nothing uploaded, nothing scheduled.")
        return

    if garmin is None:
        garmin = connect()
    if not no_upload:
        replaced = replace_existing(garmin, set(workouts))
        for name, wk in workouts.items():
            for old in replaced.get(name, []):
                print(f"Replaced existing '{name}' (removed workoutId {old})")
            result = garmin.upload_workout(wk.to_dict())
            print(f"Uploaded {name} -> workoutId {result.get('workoutId')}")

    if schedule:
        plan = schedule_plan(weeks)
        print(f"\nScheduling the next {weeks} week(s):")
        apply_schedule(garmin, plan)


if __name__ == "__main__":
    main()
