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


def test_deload_never_adds_weight():
    deload = Target(sets=3, reps_min=8, reps_max=12, rir=4.0, deload=True)
    r = recommend(deload, [perf(7, [12, 12, 12], 80, target=(8, 12))], TODAY, "barbell")
    assert r.weight < 80 and "разгрузка" in r.reason
    # a deload named only in the phase keeps RIR 2 and the same scheme: still no «+2,5»
    named = Target(sets=4, reps_min=6, reps_max=8, rir=3.0, deload=True)
    assert recommend(named, [perf(7, [8, 8, 8, 8], 85)], TODAY, "barbell").weight < 85
    # «~55-60% ПМ» in the header
    pct = Target(sets=2, reps_min=8, reps_max=8, rir=4.0, deload=True, pct=0.575)
    assert recommend(pct, [perf(7, [8, 8], 85)], TODAY, "barbell").weight <= 67.5


def test_progression_resumes_from_the_week_before_the_deload():
    hist = [perf(7, [8, 8], 50, rir=4.0, target=(8, 8)), perf(14, [8, 8, 8, 8], 80)]
    r = recommend(T, hist, TODAY, "barbell")
    assert r.weight == 82.5 and "до разгрузки" in r.reason


def test_target_hint_is_clamped():
    r = recommend(T, [perf(7, [6, 6, 6, 6], 60, hint="target", hint_kg=600)], TODAY, "barbell")
    assert r.weight == 65 and "не больше" in r.reason


def test_rounding_never_overshoots():
    # a new scheme from 20 kg on a machine with a 23 kg stack weight used before: the clamp is 22
    hist = [perf(7, [10, 10, 10], 20, rir=0.5, target=(8, 10)), perf(30, [8, 8, 8], 23, target=(8, 10))]
    assert recommend(T, hist, TODAY, "machine").weight <= 22
    # double progression from 50 when the stack has 54 next: within +10%, and the text says what it really is
    hist = [perf(7, [8, 8, 8, 8], 50), perf(60, [6, 6, 6, 6], 54)]
    r = recommend(T, hist, TODAY, "machine")
    assert r.weight == 54 and r.reason.endswith("+4")
    hist = [perf(7, [8, 8, 8, 8], 50), perf(60, [6, 6, 6, 6], 58)]
    assert recommend(T, hist, TODAY, "machine").weight == 52.5
    # hold at an odd 43,75: never rounds up
    assert recommend(T, [perf(7, [8, 7, 7, 6], 43.75, hint="hold")], TODAY, "machine").weight <= 43.75


def test_second_miss_goes_down_even_at_light_weights():
    assert recommend(T, [perf(7, [7, 6, 5, 5], 5), perf(14, [6, 6, 5, 4], 5)], TODAY, "dumbbell").weight == 4
    assert recommend(T, [perf(7, [7, 6, 5, 5], 20), perf(14, [6, 6, 5, 4], 20)], TODAY, "barbell").weight == 17.5


def test_heavy_and_custom_dumbbells():
    assert step_up(50, "dumbbell", "abs") == 52.5
    assert recommend(T, [perf(7, [8, 7, 7, 6], 55)], TODAY, "dumbbell").weight == 55
    # a 20-day pause on a sparse rack: 25 x 0.9 = 22.5 goes down to 20, not back up to 25
    r = recommend(T, [perf(20, [8, 7, 7, 6], 25)], TODAY, "dumbbell", dumbbells=[10, 15, 20, 25])
    assert r.weight == 20


def test_timed_hold_and_odd_rows():
    p = Perf(TODAY - timedelta(days=7), [None] * 3, [60, 60, 60], [None] * 3, "none", 3.0, hint="hold")
    assert recommend(Target(3, None, None, 3.0, seconds=True), [p], TODAY, "bodyweight").seconds == 60
    odd = Perf(TODAY - timedelta(days=7), [8, None], [None, None], [None, 60], "abs", 3.0, target=(10, 12))
    assert recommend(T, [odd], TODAY, "barbell").weight == 60  # reps and weights on different rows: hold
