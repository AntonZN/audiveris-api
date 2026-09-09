"""Группировка станов в системы и нарезка кропов — для движков «по одному стану».

Живёт здесь, а не в `omr/`, по простой причине: единственный потребитель — Zeus.
homr гранд-станы группирует сам (в его выходе одна партия с `<staves>2</staves>`)
и станы режет тоже сам, так что продовому пакету этот код не нужен. Держать его
в `omr/` значило бы завести там мёртвый код — ровно тот случай, который в этом
проекте уже дважды находился (`api.analysis.postprocess`, закомментированный
`movement_args` в services.py).

Если появится ещё один движок, работающий по системам, — код самодостаточный и
переезжает обратно механически.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from omr.staff import Staff


# ----------------------------------------------------------------------------------
# Системы: объединение станов в гранд-станы
# ----------------------------------------------------------------------------------

def bridge_ratio(mask: np.ndarray, upper: Staff, lower: Staff) -> float:
    """Насколько промежуток между станами «прошит» вертикальными штрихами.

    Признак гранд-стана — не расстояние, а СВЯЗНОСТЬ. У пары нотоносцев одного
    инструмента тактовые черты и акколада идут насквозь через промежуток, а между
    разными системами он пуст. Считаем долю колонок промежутка, пробитых чернилами
    сверху донизу.

    Расстояние для этого не годится, хотя и кажется очевидным: на реальной
    странице зазор внутри пары и зазор между системами перекрываются
    (замерено на californication: 6.5 и 9.1 внутри против 12.2 и 13.4 между —
    порядок нарушен). Связность же даёт идеальное чередование 0.011 / 0.0, а на
    однолинейной партии — ровно нули во всех промежутках.
    """
    y0 = int(upper.bottom.y_mid) + 2
    y1 = int(lower.top.y_mid) - 2
    x0 = int(max(upper.x0, lower.x0))
    x1 = int(min(upper.x1, lower.x1))
    if y1 - y0 < 4 or x1 - x0 < 20:
        return 0.0
    band = mask[y0:y1, x0:x1] > 0
    return float(band.all(axis=0).mean())


def group_into_systems(
    staves: list[Staff],
    mask: np.ndarray,
    *,
    min_bridge: float = 0.0025,
    max_gap_interlines: float = 25.0,
) -> list[list[Staff]]:
    """Собрать станы в системы: гранд-стан — парой, остальные — поодиночке.

    Жадное паросочетание: перебираем соседние пары по убыванию силы перемычки и
    берём те, где оба стана ещё свободны. Так пара не может вырасти до трёх
    нотоносцев (для фортепиано это и не нужно), а самые уверенные связи
    разбираются первыми.

    Порог `min_bridge` отделяет реальную перемычку от шума: на однолинейных
    партиях доля пробитых колонок равна строго нулю, так что запас огромный.
    """
    ordered = sorted(staves, key=lambda item: item.top.y_mid)
    if len(ordered) < 2:
        return [[item] for item in ordered]

    candidates = []
    for index, (upper, lower) in enumerate(zip(ordered, ordered[1:])):
        interline = upper.interline or 10.0
        gap = (lower.top.y_mid - upper.bottom.y_mid) / interline
        if gap > max_gap_interlines:
            continue
        ratio = bridge_ratio(mask, upper, lower)
        if ratio >= min_bridge:
            candidates.append((ratio, index))

    taken: set[int] = set()
    paired: dict[int, int] = {}
    for _, index in sorted(candidates, reverse=True):
        if index in taken or index + 1 in taken:
            continue
        taken.update({index, index + 1})
        paired[index] = index + 1

    systems: list[list[Staff]] = []
    index = 0
    while index < len(ordered):
        if index in paired:
            systems.append([ordered[index], ordered[index + 1]])
            index += 2
        else:
            systems.append([ordered[index]])
            index += 1
    return systems

@dataclass
class StaffCrop:
    index: int
    image: np.ndarray
    box: tuple[int, int, int, int]   # x0, y0, x1, y1 в координатах страницы
    staves: int = 1                  # сколько нотоносцев попало в кроп


def crop_staves(
    image: np.ndarray,
    staves: list[Staff],
    *,
    margin_x: float = 2.0,
    margin_y: float = 4.0,
) -> list[StaffCrop]:
    """Вырезать каждый стан отдельной картинкой, сверху вниз."""
    return crop_systems(image, [[item] for item in sorted(
        staves, key=lambda item: item.top.y_mid)],
        margin_x=margin_x, margin_y=margin_y)


def crop_systems(
    image: np.ndarray,
    systems: list[list[Staff]],
    *,
    margin_x: float = 2.0,
    margin_y: float = 4.0,
) -> list[StaffCrop]:
    """Вырезать каждую СИСТЕМУ целиком: гранд-стан — вместе с обоими нотоносцами.

    Резать гранд-стан пополам нельзя: правая и левая рука звучат одновременно, и
    модель, получившая их по отдельности, выстроит их последовательно — музыка
    поедет. Поэтому единица кропа — система (см. `staff.group_into_systems`), а
    не отдельный нотоносец.
    """
    if not systems:
        return []

    height, width = image.shape[:2]
    ordered = sorted(systems, key=lambda group: min(s.top.y_mid for s in group))
    crops: list[StaffCrop] = []

    for index, group in enumerate(ordered):
        if not group:
            continue
        top = min(s.top.y_mid for s in group)
        bottom = max(s.bottom.y_mid for s in group)
        unit = group[0].interline or 10.0
        x0 = int(max(0, min(s.x0 for s in group) - margin_x * unit))
        x1 = int(min(width, max(s.x1 for s in group) + margin_x * unit))

        # По вертикали ограничиваемся серединой промежутка до соседних систем,
        # иначе в кроп заедет чужая музыка.
        ceiling = 0.0 if index == 0 else (
            max(s.bottom.y_mid for s in ordered[index - 1]) + top
        ) / 2
        floor = float(height) if index == len(ordered) - 1 else (
            bottom + min(s.top.y_mid for s in ordered[index + 1])
        ) / 2
        y0 = int(max(ceiling, top - margin_y * unit))
        y1 = int(min(floor, bottom + margin_y * unit))

        if x1 - x0 < 8 or y1 - y0 < 8:
            continue
        crops.append(
            StaffCrop(index=len(crops), image=image[y0:y1, x0:x1].copy(),
                      box=(x0, y0, x1, y1), staves=len(group))
        )
    return crops
