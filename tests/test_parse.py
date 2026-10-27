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


def test_ambiguous_numbers_are_not_sets():
    assert parse_exercise("Жим лежа 80x5") is None  # weight x reps: unparsed, never 80 sets
    assert parse_exercise("Жим 4x60кг") is None
    assert sets("Присед 100x5x3") == [(5, None, 100)] * 3
    assert sets("Присед 3x5x100") == [(5, None, 100)] * 3
    assert sets("Жим гантелей 2x24кг 3x10") == [(10, None, 24)] * 3


def test_reps_lists_and_weight_lists():
    assert reps("Подтягивания 3xмакс 12,10,8") == [12, 10, 8]
    assert weights("Подтягивания 3xмакс 12,10,8") == [None] * 3
    assert reps("Подтягивания 3xмакс (12, 10, 8)") == [12, 10, 8]
    assert weights("Жим 3x10 12,5") == [12.5] * 3
    assert weights("Жим 3x8 60/62,5/65кг") == [60, 62.5, 65]
    assert weights("Жим 3x8 60кг, 62,5кг, 65кг") == [60, 62.5, 65]
    assert weights("Жим 3x8 60-65кг") == [60] * 3  # which set had what is unknown: the lower weight
    assert reps("Жим 3x8 60кг 8,8,7") == [8, 8, 7]


def test_ordinals_are_whole_words():
    assert sets("Присед 3x5 100кг на пятках 2 сек пауза") == [(5, None, 100)] * 3
    assert len(sets("Жим 3x8 60кг опять 6 в последнем")) == 3
    assert reps("Жим 3x8 60кг повторил 2 раза") == [8, 8, 8]
    assert len(sets("Тяга 3x10 50кг четвертый 8")) == 3
    line = "Сведения в кроссовере 3 x 15 36кг - нужно поймать технику (первые два, 30кг третий)"
    assert weights(line) == [36, 36, 30]


def test_units_and_hints_that_are_not_kilograms():
    assert parse_exercise("Гантели 3x12 10кг, 20 максимум повторов").hint is None
    assert sets("Жим 3x10 минимум 60кг") == [(10, None, 60)] * 3
    assert [s for _, s, _ in parse_exercise("Планка 3x1.5 мин").expand()] == [90, 90, 90]


def test_superset_part_with_own_scheme_keeps_no_weight():
    p = parse_post("Суперсет: жим гантелей 3x10 20кг + отжимания 3xмакс")
    assert [(e.name, e.weight) for e in p.ex_lines] == [("жим гантелей", 20), ("отжимания", None)]


def test_bot_plan_posts_are_not_logs():
    plan = ("НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ А\n\nЖим штанги лежа – 4x6-8 62,5кг\nРазминка (Жим): гриф×10 · 25×5\n"
            "• Жим штанги лежа: было 4x6 60кг · все по верху → +2,5\n• Тяга: новое — подбери вес\n#план")
    assert parse_post(plan).kind == "bot"
    assert parse_post(plan.replace("\n#план", "")).kind == "bot"


def test_post_kinds_edge_cases():
    assert parse_post("Ср не приду, работа").kind == "note"
    p = parse_post("вчера\n70,6кг")
    assert (p.kind, p.bodyweight, p.date.days_back) == ("body", 70.6, 1)
    p = parse_post("Вес 70,8\nЖим лежа 3x8 60кг")
    assert (p.kind, p.bodyweight) == ("log", 70.8)
    assert sets("Жим ногами – 3x10 - 150кг") == [(10, None, 150)] * 3
    assert sets("пресс машина – 3x8-41кг") == [(8, None, 41)] * 3
    assert sets("Подтягивания узким хватом 3-4 х 6-10.") == [(6, None, None)] * 3
    assert reps("Присед 5 x 6-8 82,5кг (1-8; 2-8; 3-8; 4-8; 5-") == [6] * 5  # a list cut short is not «8; 2»
    program = ("НЕДЕЛЯ 1\nПОНЕДЕЛЬНИК: ВЕРХ\nЖим штанги лежа – 4x6-8 60кг\nТяга – 4x8-10 50кг\n\n"
               "ВТОРНИК: НИЗ\nПрисед – 4x6-8 80кг\nЖим ногами – 3x10-12 100кг")
    assert parse_post(program).kind == "template"
