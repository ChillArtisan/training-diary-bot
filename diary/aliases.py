"""Exercise names: normalization, seed synonyms, equipment guess, fuzzy resolve."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

AUTO_SCORE = 0.92       # this similar and only a typo inside one word -> same exercise, silently
ASK_SCORE = 0.75        # same first word and this similar -> new exercise plus a merge suggestion
ASK_SCORE_OTHER = 0.85  # different first words need more similarity before asking

# canonical name -> spellings seen in the diary
SEED: dict[str, list[str]] = {
    "Жим штанги лёжа": ["жим лежа", "жим штанги лежа"],
    "Жим штанги на наклонной": ["жим штанги на наклонной"],
    "Тяга штанги в наклоне": ["тяга в наклоне"],
    "Армейский жим": ["армейский жим стоя", "армейский жим штанги", "жим штанги стоя"],
    "Приседания со штангой": ["приседания", "присед"],
    "Румынская тяга": ["румынская тяга"],
    "Становая тяга": ["становая тяга"],
    "Трэп-гриф тяга": ["трэп гриф тяга"],
    "Болгарские сплит-приседания": ["болгарские сплиты", "болгарские сплит приседания"],
    "Разгибания ног сидя": ["разнибания ног сидя", "разгибания ног в тренажере", "разгибания ног"],
    "Сгибания ног лёжа": ["сгибания ног лежа", "сгибания ног лежа в тренажере"],
    "Подъём на носки стоя": ["подъемы на носки стоя", "подъем на носки стоя", "икры стоя"],
    "Подъём на носки сидя": ["подъемы на носки сидя", "подъем на носки сидя", "икры сидя"],
    "Гиперэкстензия": ["гиперэкстензция", "гиперэкстенция"],
    "Тяга лица": ["тяга к лицу", "лицевая тяга", "тяга к лицу в кроссовере"],
    "Тяга верхнего блока": ["тяга верх блока", "тяга вертикального блока"],
    "Тяга верхнего блока U-хватом": ["тяга верх блока u хватом", "тяга верхнего блока u хватом"],
    "Тяга горизонтального блока": ["тяга блока к поясу", "тяга нижнего блока"],
    "Разгибания на трицепс в блоке": ["трицепс на блоке", "трицепс не блоке", "трицепс блок", "разгибания на трицепс"],
    "Жим в блоке сидя": ["жим в блокн сидя"],
    "Махи гантелями в стороны": [
        "махи в стороны", "подъемы гантелей в стороны", "разведения гантелей в стороны", "махи гантелями через стороны",
    ],
    "Молотки": ["молотковые сгибания", "молотки на бицепс", "сгибания с гантелями молот"],
    "Подъём штанги на бицепс": ["подъемы штанги на бицепс", "сгибания штанги на бицепс", "бицепс со штангой"],
    "Русские скручивания": ["русские скручавания", "русские скручивания"],
    "Французский жим лёжа": ["французский жим лежа", "французский жим"],
}

_EQUIPMENT = [
    ("dumbbell", r"гантел|молот"),
    ("barbell", r"смит"),
    ("cable", r"блок|кроссовер|канат|тяга лица|тяга к лицу|лицев|face|паллоф"),
    ("machine", r"тренаж|машин|жим ногами|хаммер|peck|бабочк|гакк|сгибания ног|разгибания ног|носки|икры|сведения"),
    ("bodyweight", r"подтяг|брусь|отжим|\bвис|планк|скручив|подъем ног|подъемы ног|гиперэкст|книжка|уголок|hollow|стойка|выпрыг|напрыг"),
    ("barbell", r"штанг|гриф|присед|тяга|жим|шраги|выпад|мост|франц"),
]


# word forms that mean the same thing in an exercise name
LEMMA = {
    "подъемы": "подъем", "разгибание": "разгибания", "сгибание": "сгибания", "разводка": "разведения",
    "приседы": "приседания", "отжимание": "отжимания", "подтягивание": "подтягивания",
    "скручивание": "скручивания", "накл": "наклонной", "верх": "верхнего",
}


# words that make two otherwise similar names different exercises
VARIANT_RE = re.compile(
    r"^(?:стоя|сидя|лежа|узк|широк|обратн|прям|нейтрал|хват|акцент|трицепс|бицепс|груд|наклон|горизонт|вертикал"
    r"|верхн|нижн|гантел|штанг|блок|тренаж|смит|гриф|кроссовер|канат|одн|поочер|фронт|сумо|румын|голов|колен)"
)
# equipment named in the text itself; a name saying «гантели» is never the barbell lift
EQUIPMENT_WORDS = {"dumbbell": re.compile(r"гантел"), "barbell": re.compile(r"штанг|\bгриф\b|смит")}
QUAL_NOISE_RE = re.compile(r"\b(?:см|кг|раз|сек|мин|подход\w*|перв\w*|последн\w*|остальн\w*)\b")


def norm(name: str) -> str:
    s = name.lower().replace("ё", "е")
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return " ".join(LEMMA.get(w, w) for w in s.split())


def qualifier(name: str) -> str:
    """Words in brackets that name a variant: «(акцент на трицепс)»; «(30-40 см)» and «(или …)» are not."""
    parts = []
    for q in re.findall(r"\(([^)]*)\)", name.lower().replace("ё", "е")):
        q = " ".join(QUAL_NOISE_RE.sub(" ", re.sub(r"[^a-zа-я ]+", " ", q)).split())
        if q and not q.startswith("или "):
            parts.append(q)
    return " ".join(parts)


def norm_full(name: str) -> str:
    """norm() plus the bracket qualifier: the alias key of a variant."""
    return " ".join(filter(None, [norm(name), norm(qualifier(name))]))


def conflicting(raw: str, target_name: str) -> bool:
    """The bracket words of a name contradict the exercise its plain alias points to."""
    qa, qb = qualifier(raw), qualifier(target_name)
    if not qa or qa == qb:
        return False
    if qb:
        return True
    plain = set(norm(target_name).split())
    return any(VARIANT_RE.match(w) and w not in plain for w in qa.split())


def equipment_named(n: str) -> set[str]:
    return {eq for eq, rx in EQUIPMENT_WORDS.items() if rx.search(n)}


def equipment_clash(a: str, b: str) -> bool:
    """One name says dumbbells, the other a barbell."""
    ea, eb = equipment_named(a), equipment_named(b)
    return bool(ea and eb and not ea & eb)


def safe_auto(a: str, b: str) -> bool:
    """A silent merge only for a typo inside one word; стоя/сидя, digits or an extra word need the owner."""
    ta, tb = a.split(), b.split()
    if len(ta) != len(tb):
        return False
    diff = [(x, y) for x, y in zip(ta, tb) if x != y]
    if not diff:
        return True
    if len(diff) > 1:
        return False
    x, y = diff[0]
    if x.isdigit() or y.isdigit() or VARIANT_RE.match(x) or VARIANT_RE.match(y):
        return False
    return score(x, y) >= 0.75


def display(name: str) -> str:
    s = re.sub(r"\s+", " ", name).strip(" –—-:,.")
    return s[:1].upper() + s[1:] if s else s


def guess_equipment(name: str) -> str:
    n = norm(name)
    for eq, pattern in _EQUIPMENT:
        if re.search(pattern, n):
            return eq
    return "machine"


def score(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def best_match(n: str, known: dict[str, int]) -> tuple[int | None, float, str | None]:
    """Best fuzzy candidate among normalized aliases -> (exercise_id, score, alias)."""
    best = (None, 0.0, None)
    for alias, eid in known.items():
        sc = score(n, alias)
        if sc > best[1]:
            best = (eid, sc, alias)
    return best


def same_first_word(a: str, b: str) -> bool:
    return a.split(" ", 1)[0] == b.split(" ", 1)[0]
