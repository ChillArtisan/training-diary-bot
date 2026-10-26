from datetime import date, timedelta

from diary.recommend import Perf, Target, recommend, step_up

TODAY = date(2026, 10, 27)


def perf(days_ago, reps, w, rir=3.0, target=(6, 8), kind="abs", hint=None, hint_kg=None):
    return Perf(TODAY - timedelta(days=days_ago), list(reps), [None] * len(reps), [w] * len(reps), kind, rir,
                target, hint, hint_kg)


T = Target(sets=4, reps_min=6, reps_max=8, rir=3.0)


def test_all_sets_at_top_adds_increment():
    r = recommend(T, [perf(7, [8, 8, 8, 8], 60)], TODAY, "barbell")
    assert r.weight == 62.5 and "по верху" in r.reason


def test_within_range_holds():
    assert recommend(T, [perf(7, [8, 7, 7, 6], 60)], TODAY, "barbell").weight == 60


def test_miss_holds_then_cuts():
    first = recommend(T, [perf(7, [7, 6, 5, 5], 60)], TODAY, "barbell")
    assert first.weight == 60 and "недобор" in first.reason
    second = recommend(T, [perf(7, [7, 6, 5, 5], 60), perf(35, [6, 6, 5, 4], 60)], TODAY, "barbell")
    assert second.weight == 57.5


def test_owner_hint_hold_wins():
    r = recommend(T, [perf(7, [8, 8, 8, 8], 60, hint="hold")], TODAY, "barbell")
    assert r.weight == 60 and "не добавлять" in r.reason


def test_owner_hint_target():
    r = recommend(T, [perf(7, [6, 6, 6, 6], 62.5, hint="target", hint_kg=65)], TODAY, "barbell")
    assert r.weight == 65


def test_scheme_change_uses_strength_estimate():
    # peak week 4x6 85 at RIR 0.5 -> intro week 4x6-8 at RIR 3
    r = recommend(T, [perf(7, [6, 6, 6, 6], 85, rir=0.5)], TODAY, "barbell")
    assert 77.5 <= r.weight <= 80


def test_deload_sessions_do_not_inflate_the_estimate():
    # owner did the deload with the peak weight; the estimate must come from the peak week
    hist = [perf(7, [8, 8], 85, rir=4.0, target=(8, 8)), perf(14, [6, 6, 6, 6], 85, rir=0.5)]
    assert recommend(T, hist, TODAY, "barbell").weight <= 80


def test_deload_target_goes_down():
    deload = Target(sets=2, reps_min=8, reps_max=8, rir=4.0, deload=True)
    r = recommend(deload, [perf(7, [6, 6, 6, 6], 85, rir=0.5)], TODAY, "barbell")
    assert r.weight < 85


def test_long_break_cuts_weight():
    # three months off: the working weight drops by 30%
    r = recommend(T, [perf(95, [8, 8, 8, 8], 70)], TODAY, "barbell")
    assert r.weight == 50 and "пауза 95" in r.reason


def test_dumbbell_steps():
    assert step_up(17.5, "dumbbell", "abs") == 18
    r = recommend(Target(3, 10, 12, 3.0), [perf(7, [12, 12, 12], 17.5, target=(10, 12))], TODAY, "dumbbell")
    assert r.weight == 18


def test_machine_snaps_to_used_stack_weight():
    hist = [perf(7, [12, 12, 12], 53.25, target=(10, 12)), perf(14, [10, 10, 10], 55.6, target=(10, 12))]
    r = recommend(Target(3, 10, 12, 3.0), hist, TODAY, "cable")
    assert r.weight == 55.6


def test_seconds_goal():
    p = Perf(TODAY - timedelta(days=7), [None] * 3, [70, 60, 67], [None] * 3, "none", 3.0)
    r = recommend(Target(3, None, None, 3.0, seconds=True), [p], TODAY, "bodyweight")
    assert r.seconds == 75


def test_added_weight_progression():
    r = recommend(T, [perf(7, [8, 8, 8, 8], 10, kind="added")], TODAY, "bodyweight", bodyweight=80)
    assert r.kind == "added" and r.weight == 12.5


def test_no_history():
    r = recommend(T, [], TODAY, "barbell")
    assert r.weight is None and "подбери" in r.reason
