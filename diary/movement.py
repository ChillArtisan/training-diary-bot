"""Movement groups: what can replace an exercise when the machine is busy or it hurts."""
from __future__ import annotations

import re

from . import aliases
from .db import Db

# first match wins, so specific patterns go before general ones
GROUPS: list[tuple[str, str]] = [
    ("жим ногами", r"жим ногами|гакк"),
    ("выпады", r"выпад|сплит|болгар"),
    ("присед", r"присед"),
    ("разгибание ног", r"разгибания ног"),
    ("сгибание ног", r"сгибания ног"),
    ("икры", r"носки|икры"),
    ("ягодичный мост", r"мост|отведени"),
    ("тяга с пола", r"румынск|становая|тяга с плинтов|трэп|гиперэкст"),
    ("задняя дельта", r"тяга лица|тяга к лицу|face|задн|обратные разведения|разводка .*наклон|в наклоне.*(?:разведени|махи|подъем)|махи в наклоне"),
    ("жим на наклонной", r"жим.*наклон"),
    ("жим лёжа", r"жим (?:штанги |гантелей )?лежа|хаммер|жим в тренажере(?! смита на)|отжимания от пола|отжимания с|отжимания на брусьях"),
    ("жим вверх", r"армейск|жим .*(?:сидя|стоя)|жим арнольда|pike|уголком"),
    ("сведения", r"разведения гантелей лежа|сведени|бабочк|peck|кроссовер"),
    ("средняя дельта", r"махи|в стороны|дельта машин"),
    ("вертикальная тяга", r"подтяг|тяга верхнего блока|тяга вертикального|пуловер"),
    ("горизонтальная тяга", r"тяга (?:штанги|гантели|т ?грифа|горизонтального|блока|нижнего|тренажера)|тяга в наклоне|к поясу"),
    ("бицепс", r"бицепс|молот|сгибания рук|концентрир"),
    ("трицепс", r"трицепс|французск|разгибания .*(?:блок|канат|голов)|отжимания (?:от скамьи|обратные|узк)"),
    ("шраги", r"шраги"),
    ("пресс", r"скруч|планк|пресс|ролик|паллоф|подъем ног|книжка|hollow|\bвис\b|уголок|велосипед"),
]


def group_of(name: str) -> str | None:
    n = aliases.norm(name)
    return next((g for g, pattern in GROUPS if re.search(pattern, n)), None)


def alternatives(db: Db, eid: int, exclude: set[int] | None = None, limit: int = 4) -> list[tuple[int, str]]:
    """Exercises of the same movement group that the owner has actually done, most recent first."""
    ex = db.exercise(eid)
    group = group_of(ex["name"]) if ex else None
    if group is None:
        return []
    exclude = (exclude or set()) | {eid}
    out = [(x["id"], x["name"]) for x in db.exercises_used() if x["id"] not in exclude and group_of(x["name"]) == group]
    return out[:limit]
