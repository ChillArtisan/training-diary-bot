"""Lines are real ones from the diary export."""
from diary.parse import parse_day, parse_exercise, parse_post, parse_week


def sets(line):
    return parse_exercise(line).expand()


def weights(line):
    return [w for _, _, w in sets(line)]


def reps(line):
    return [r for r, _, _ in sets(line)]


def test_indexed_reps():
    ex = parse_exercise("Жим штанги лёжа  5 x 6-8 70кг (1-8; 2-8; 3-8; 4-7; 5-7)")
    assert (ex.name, ex.sets, ex.reps_min, ex.reps_max, ex.weight) == ("Жим штанги лёжа", 5, 6, 8, 70)
    assert reps(ex.raw) == [8, 8, 8, 7, 7]


def test_first_and_rest_weights():
    assert weights("Армейский жим стоя – 4x6 35кг(первый) 32,5кг(остальные три)") == [35, 32.5, 32.5, 32.5]
    assert weights("Присед 4 x 10 (1-80кг; остальные 75кг)") == [80, 75, 75, 75]


def test_last_set_weight():
    assert weights("Тяга верхнего блока широким хватом: 4x15 (54кг) (последний 47кг)") == [54, 54, 54, 47]
    assert weights("Разгибания у блока с канатом 3 x 15 36кг(последний 33кг)") == [36, 36, 33]


def test_ordinal_weights_without_unit():
    assert weights("Тяга вертикального блока 3 x 12 61кг(первые два - 61, третий - 54)") == [61, 61, 54]


def test_added_weight_and_partial_sets():
    ex = parse_exercise("Подтягивания (с доп. весом) 5 x 6-8 +5кг (1-8; 2-8; 3-4; закочнил)")
    assert (ex.kind, ex.weight) == ("added", 5)
    assert reps(ex.raw)[:3] == [8, 8, 4]


def test_plank_seconds():
    ex = parse_exercise("Планка 3хмакс(71с; 70с; 70с)")
    assert ex.seconds and [s for _, s, _ in ex.expand()] == [71, 70, 70]
    ex = parse_exercise("Планка: 3x120 сек (1-90с; 2-60с; 3-60с)")
    assert [s for _, s, _ in ex.expand()] == [90, 60, 60]
    assert [s for _, s, _ in parse_exercise("Вис на турнике с лямками 2х1мин").expand()] == [60, 60]


def test_hints():
    ex = parse_exercise("Тяга штанги в наклоне 3 x 12 62,5кг- потом делай 65кг")
    assert (ex.weight, ex.hint, ex.hint_kg) == (62.5, "target", 65)
    ex = parse_exercise("Жим гантелей на наклонной(45*) 3 x 12 17,5кг - не добавлять! (последний на 10)")
    assert (ex.weight, ex.hint) == (17.5, "hold")
    assert reps(ex.raw) == [12, 12, 10]
    ex = parse_exercise("Тяга верхнего блока широким хватом 4 x 12 54кг - можно в следующий раз +2,5кг")
    assert (ex.weight, ex.kind, ex.hint) == (54, "abs", "bump")
    assert parse_exercise("Жим лежа 3х10 65кг не стоит добавлять").hint == "hold"
    assert parse_exercise("Тяга в наклоне 4х7 55кг(добавить.)").hint == "bump"


def test_weight_kinds():
    assert parse_exercise("Сгибания ног лежа – 3x12 41").weight == 41
    ex = parse_exercise("Боковые скручивания в блоке 2х12 6плитка")
    assert (ex.kind, ex.weight) == ("units", 6)
    ex = parse_exercise("Ягодичный мостик со штангой: 3x15 (стим + 30кг)")
    assert (ex.kind, ex.weight) == ("smith", 30)
    assert parse_exercise("Болгарские сплиты 3х10 17,5kg").weight == 17.5
    assert parse_exercise("Подъёмы ног в висе 3х12").kind == "none"


def test_rir_mark():
    assert parse_exercise("Жим лежа 4x6 60кг @2").rir == 2
    assert parse_exercise("Жим лежа 4x6 60кг @1-2").rir == 1.5


def test_dash_ranges():
    ex = parse_exercise("Отжимания с ногами на возвышении / обычные отжимания 2–3×12–20")
    assert (ex.sets, ex.reps_min, ex.reps_max) == (2, 12, 20)


def test_superset():
    p = parse_post("Суперсет подъем штанги на бицепс 19кг  + французский жим 3х12 24кг")
    lines = p.ex_lines
    assert [(e.name, e.sets, e.reps_min, e.weight) for e in lines] == [
        ("подъем штанги на бицепс", 3, 12, 19), ("французский жим", 3, 12, 24)]
    p = parse_post("Суперсет: махи в стороны + махи в наклоне – 3x15 5кг")
    assert [e.weight for e in p.ex_lines] == [5, 5]


def test_headers():
    assert parse_day("ПОНЕДЕЛЬНИК: ВЕРХ А")[:2] == (0, "ВЕРХ А")
    assert parse_day("# ПН: UPPER (Объем - Жимовой акцент)")[0] == 0
    assert parse_day("Чт. Небольшое фулбади, чтобы восстановиться")[0] == 3
    assert parse_day("Сб, Пн, Вт - пропущены, работа.") is None
    assert parse_day("Тренировка А:")[1] == "А"
    assert parse_week("НЕДЕЛЯ 3: ПИК · RIR 0-1 · ~80-85% ПМ")[::3] == (3, 0.5)
    assert parse_week("#НЕДЕЛЯ1 (База) 2-3 повтора в запасе")[::3] == (1, 2.5)
    assert parse_week("НЕДЕЛЯ 4 (РАЗГРУЗОЧНАЯ)")[3] == 4
    assert parse_week("Через две недели отдыха снова в зал") is None


def test_post_kinds():
    assert parse_post("70,2кг").kind == "body"
    assert parse_post("Замеры на 01.03.2026\nВес: 71,3\nРост 181").bodyweight == 71.3
    assert parse_post("Сегодня просто хорошее настроение").kind == "note"
    template = parse_post("НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ\nЖим штанги лежа – 4x8\n\nВТОРНИК: НИЗ\nЖим ногами – 3x8")
    assert template.kind == "template" and len(template.blocks) == 2
    log = parse_post("НЕДЕЛЯ 1: ВВОД · RIR 3\nВТОРНИК: НИЗ А\n\nПриседания со штангой – 4x8 70кг\nкак-то тяжело")
    assert log.kind == "log"
    b = log.blocks[0]
    assert (b.week, b.weekday, b.label, b.rir, b.notes) == (1, 1, "НИЗ А", 3, ["как-то тяжело"])


def test_unparsed_data_lines():
    p = parse_post("Понедельник. проверка максимума.\n90кг - тяжело.")
    assert p.unparsed == ["90кг - тяжело."]
