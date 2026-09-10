#!/usr/bin/env python3
"""Запуск homr отдельным процессом + защита от падения на вырожденном стане.

Отдельный процесс — потому что onnxruntime поднимает сотни мегабайт и держать
его в веб-воркере незачем; запускается и умирает вместе с задачей.

Защита. В `homr/staff_parsing.py` размеры кропа стана считаются целочисленным
умножением на масштабный коэффициент, и на очень узком или низком стане
результат округляется в НОЛЬ. Дальше OpenCV падает — то в `cv2.resize`
(`inv_scale_x > 0`), то в `cv2.threshold` на пустой матрице (та возвращает None,
и следующая строка ловит `TypeError`), — и вместе с ним падает весь процесс homr.
Итог: один кривой стан из двадцати убивает распознавание ВСЕЙ страницы.

Защищаемся на двух уровнях:

1. `cv2.resize` внутри модуля не может получить нулевой размер — почти
   вырожденный стан переживает обработку и остаётся в результате;
2. разбор ОДНОГО стана обёрнут в try/except. Что бы там ни сломалось, стан
   отдаётся пустым, а `parse_staffs` такие пропускает штатно
   («Skipping empty staff»). Страница теряет один стан вместо всего выхода.

Второй уровень тут главный: он ловит и те падения, которых мы ещё не видели.

Скрипт намеренно не импортирует ничего из `omr` — он должен запускаться тем
интерпретатором, где стоит homr, каким бы тот ни был.
"""

import sys
import threading
from pathlib import Path

import numpy as np


class _SafeCv2:
    """Прокси над cv2, у которого resize не умеет получить нулевой размер."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def resize(self, src, dsize, *args, **kwargs):
        if dsize is not None:
            width, height = dsize
            dsize = (max(int(width), 1), max(int(height), 1))
        return self._real.resize(src, dsize, *args, **kwargs)


def _install_canvas_guard() -> None:
    import cv2

    from homr import staff_parsing

    # Главная защита: любой resize внутри staff_parsing получает размер >= 1.
    staff_parsing.cv2 = _SafeCv2(cv2)

    def clamp(shape):
        return np.maximum(np.asarray(shape), 1)

    # Плюс точечные патчи на исходные места — они дешёвые и оставляют размеры
    # согласованными между собой, а не только неотрицательными. Каждый под
    # hasattr: если в новой версии homr функцию переименуют, мы просто
    # запустимся без этой части защиты, а не упадём на импорте раннера.
    if hasattr(staff_parsing, "get_tr_omr_canvas_size"):
        original = staff_parsing.get_tr_omr_canvas_size
        staff_parsing.get_tr_omr_canvas_size = lambda *a, **k: clamp(original(*a, **k))

    if hasattr(staff_parsing, "center_image_on_canvas"):
        original_center = staff_parsing.center_image_on_canvas
        staff_parsing.center_image_on_canvas = (
            lambda image, canvas, *a, **k: original_center(image, clamp(canvas), *a, **k)
        )

    # Ловушка последней надежды: сбой на одном стане не должен ронять страницу.
    if hasattr(staff_parsing, "parse_staff_image"):
        original_parse = staff_parsing.parse_staff_image

        def safe_parse(debug, index, *args, **kwargs):
            try:
                return original_parse(debug, index, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — ради этого всё и затевалось
                print(f"staff {index} skipped: {type(exc).__name__}: {exc}", file=sys.stderr)
                return []

        staff_parsing.parse_staff_image = safe_parse


# Что homr увидел на странице: «высокие» элементы у левого края (акколады,
# скобки, начальные черты систем) и сами станы. Заполняется при подборе
# гранд-станов, читается при склейке систем — один процесс раннера = одна страница.
_PAGE: dict = {}


def _install_brace_pairing() -> None:
    """Собирать гранд-станы по акколаде, а не вслепую парами.

    homr 0.6.2 склеивает станы системы в гранд-станы ВСЛЕПУЮ: 0+1, 2+3, … (при
    нечётном числе первый остаётся одиночным). Для фортепиано и «голос +
    фортепиано» это случайно верно, для ансамбля — нет: в концерте Корелли семь
    струнных стали четырьмя «фортепиано», скрипка читалась в паре с
    виолончелью как правая и левая рука. В 0.7.0 пары подбираются по акколаде,
    но там длинная скобка всей системы перебивает акколаду, и виолончель
    Брамса уезжает в правую руку фортепиано — хуже, чем вслепую.

    Признак, который различает всё это на наших эталонах (замер по выходу
    сегментации самого homr, «высокие» элементы у левого края системы):

    * акколада фортепиано — ТОЛСТЫЙ элемент (от 0.9 межлинейного интервала;
      на эталонах 1.1-1.3) и накрывает РОВНО два стана;
    * скобка группы (струнные, хор) тоньше (0.7) и накрывает три стана и
      больше; начальная тактовая черта системы — ещё тоньше.

    Поэтому: есть акколада ровно на пару — эта пара гранд-стан; есть элемент
    на три стана и больше, а акколады нет — это ансамбль, каждый стан отдельно;
    нет ни того, ни другого — прежнее поведение 0.6.2. Для систем страницы без
    собственных признаков берём решение соседней системы с тем же числом
    станов: разное решение внутри страницы даёт разное число голосов, а на
    этом homr раскладывает страницу в одну партию.

    В версиях, где `_create_grandstaffs` уже принимает акколады (0.7+), не
    вмешиваемся.
    """
    import inspect

    import homr.main as homr_main
    from homr import brace_dot_detection
    from homr.model import MultiStaff

    if not (
        hasattr(brace_dot_detection, "_create_grandstaffs")
        and hasattr(brace_dot_detection, "_filter_for_tall_elements")
        and hasattr(brace_dot_detection, "find_braces_brackets_and_grand_staff_lines")
    ):
        return
    if len(inspect.signature(brace_dot_detection._create_grandstaffs).parameters) != 1:
        return

    blind = brace_dot_detection._create_grandstaffs
    original_find = brace_dot_detection.find_braces_brackets_and_grand_staff_lines

    def find(debug, staffs, brace_dot):
        try:
            _PAGE["tall"] = brace_dot_detection._filter_for_tall_elements(brace_dot, staffs)
        except Exception:  # noqa: BLE001 — без признаков просто работаем вслепую
            _PAGE["tall"] = []
        _PAGE["staffs"] = sorted(staffs, key=lambda staff: staff.min_y)
        return original_find(debug, staffs, brace_dot)

    def create(multi_staffs):
        decisions = [
            _brace_decision(multi, _PAGE.get("tall", []), _PAGE.get("staffs", []))
            for multi in multi_staffs
        ]
        by_size = {len(m.staffs): d for m, d in zip(multi_staffs, decisions) if d is not None}
        result = []
        for multi, decision in zip(multi_staffs, decisions):
            if decision is None:
                decision = by_size.get(len(multi.staffs))
            if decision is None:
                result.extend(blind([multi]))
                continue
            result.append(_pair_staffs(multi, decision, MultiStaff))
        print(
            "grand staffs by brace: "
            + " ".join(
                "?" if d is None else ("|".join(map(str, sorted(d))) or "-")
                for d in decisions
            ),
            file=sys.stderr,
        )
        return result

    brace_dot_detection._create_grandstaffs = create
    brace_dot_detection.find_braces_brackets_and_grand_staff_lines = find
    homr_main.find_braces_brackets_and_grand_staff_lines = find


def _brace_decision(multi, tall, page_staffs):
    """Индексы станов, начинающих гранд-стан; пустое множество — все отдельно;
    None — признаков нет."""
    staffs = multi.staffs
    if len(staffs) < 2 or not tall or not page_staffs:
        return None
    unit = float(np.median([staff.average_unit_size for staff in staffs]))
    left = min(staff.min_x for staff in staffs)
    members = {id(staff): index for index, staff in enumerate(staffs)}
    pairs: set[int] = set()
    grouped = False
    for element in tall:
        width, height = element.size
        if abs(element.center[0] - left) > 4 * unit:
            continue  # не у левого края: тактовая черта внутри системы
        top, bottom = element.center[1] - height / 2, element.center[1] + height / 2
        covered, partial = [], False
        for staff in page_staffs:
            share = _share(staff, top, bottom)
            if share >= 0.8:
                covered.append(staff)
            elif share > 0.3:
                partial = True
        inside = [members[id(staff)] for staff in covered if id(staff) in members]
        if not inside:
            continue
        if len(covered) == 2 and not partial and width >= 0.9 * unit:
            first, second = sorted(inside) if len(inside) == 2 else (None, None)
            if first is not None and second == first + 1:
                pairs.add(first)
        elif len(covered) >= 3:
            grouped = True
    if pairs:
        return pairs
    if grouped:
        return set()
    return None


def _share(staff, top: float, bottom: float) -> float:
    """Какую долю высоты стана накрывает вертикальный отрезок [top, bottom]."""
    span = max(staff.max_y - staff.min_y, 1.0)
    return max(0.0, min(bottom, staff.max_y) - max(top, staff.min_y)) / span


def _pair_staffs(multi, pairs, multi_staff_cls):
    staffs = multi.staffs
    merged = []
    index = 0
    while index < len(staffs):
        if index in pairs and index + 1 < len(staffs):
            merged.append(staffs[index].merge(staffs[index + 1]))
            index += 2
        else:
            merged.append(staffs[index])
            index += 1
    return multi_staff_cls(merged, multi.connections)


def _install_system_rejoin() -> None:
    """Склеить систему, которую homr порвал на куски, до того как он сдастся.

    homr раскладывает станы страницы по голосам так: `staff.staffs[voice]` в
    каждой системе, то есть i-й стан каждой системы — это i-я партия. Работает,
    только если во ВСЕХ системах станов поровну. Если нет, `_ensure_same_number_of_staffs`
    разбивает на одиночные станы уже ВСЮ страницу, и страница уходит одной
    партией: системы идут подряд, виолончель и фортепиано, правая и левая рука
    перестают звучать одновременно.

    Разное число станов почти всегда значит одно: связь стана с остальной
    системой (акколада, общая тактовая черта) не нашлась, и система распалась
    на две — `[2, 1, 1, 2, 2]` вместо `[2, 2, 2, 2]` (Брамс, соч. 99, стр. 4:
    теряла 24 такта виолончели). Такие куски склеиваем обратно: подряд идущие
    системы, которые в сумме дают обычное число станов и стоят друг к другу
    ближе, чем соседние системы между собой, — или которые накрывает один и тот
    же элемент у левого края (общая черта системы, скобка группы).

    Всё, что так не чинится, отдаётся исходной функции — поведение прежнее.
    """
    from homr import staff_parsing
    from homr.model import MultiStaff

    if not hasattr(staff_parsing, "_ensure_same_number_of_staffs"):
        return
    original = staff_parsing._ensure_same_number_of_staffs

    def aligned(staffs, image):
        rejoined = _rejoin_split_systems(staffs, MultiStaff, _PAGE.get("tall", []))
        if rejoined is not None:
            print(
                "systems re-joined: "
                f"{[len(s.staffs) for s in staffs]} -> {[len(s.staffs) for s in rejoined]}",
                file=sys.stderr,
            )
            staffs = rejoined
        return original(staffs, image)

    staff_parsing._ensure_same_number_of_staffs = aligned


def _rejoin_split_systems(systems, multi_staff_cls, tall=()):
    """Вернуть системы с восстановленным числом станов или None, если чинить нечего."""
    counts = [len(system.staffs) for system in systems]
    if len(set(counts)) <= 1:
        return None
    # Обычное число станов — самое частое; при равенстве берём большее: распад
    # системы даёт куски МЕНЬШЕ целого, а не больше.
    target = max(set(counts), key=lambda count: (counts.count(count), count))

    def top(system):
        return min(staff.min_y for staff in system.staffs)

    def bottom(system):
        return max(staff.max_y for staff in system.staffs)

    def merged(group):
        return multi_staff_cls(
            [staff for piece in group for staff in piece.staffs],
            [link for piece in group for link in piece.connections],
        )

    # Масштаб «между системами» — по соседним ЦЕЛЫМ системам.
    gaps = [
        top(b) - bottom(a)
        for a, b in zip(systems, systems[1:])
        if len(a.staffs) == target and len(b.staffs) == target
    ]
    if not gaps:
        # Целых соседей нет — типично для страницы с ОДНОЙ системой (партитура
        # ансамбля: семь станов, распавшихся на 6 + 1). Мерить расстояние не по
        # чему, остаётся прямой признак: стык кусков накрыт общим элементом.
        groups = [[systems[0]]]
        for system in systems[1:]:
            if _spanned_together(groups[-1][-1], system, tall):
                groups[-1].append(system)
            else:
                groups.append([system])
        if len(groups) == len(systems):
            return None
        result = [merged(group) for group in groups]
        if len({len(system.staffs) for system in result}) > 1:
            return None
        return result
    limit = float(np.median(gaps))

    def joinable(upper, lower):
        # Расстояние — быстрый признак, но не железный: у Брамса промежуток
        # «виолончель — фортепиано» внутри системы бывает БОЛЬШЕ промежутка между
        # системами. Поэтому второй признак — общий элемент у левого края.
        return top(lower) - bottom(upper) < limit or _spanned_together(upper, lower, tall)

    result = []
    index = 0
    while index < len(systems):
        if counts[index] >= target:
            result.append(systems[index])
            index += 1
            continue
        group = [systems[index]]
        total = counts[index]
        following = index + 1
        while (
            total < target
            and following < len(systems)
            and joinable(group[-1], systems[following])
        ):
            group.append(systems[following])
            total += counts[following]
            following += 1
        if total != target:
            result.append(systems[index])
            index += 1
            continue
        result.append(merged(group))
        index = following

    if len(result) == len(systems):
        return None
    return result


def _spanned_together(upper, lower, tall) -> bool:
    """Накрывает ли стык двух кусков один элемент у левого края системы.

    Начальная черта системы и скобка группы проходят через ВСЕ станы системы, а
    между системами промежуток пуст. Поэтому элемент, накрывающий и последний
    стан верхнего куска, и первый стан нижнего, — прямое доказательство, что это
    одна система. (Тактовые черты для этого не годятся: на этом этапе homr ещё
    не разложил их по станам — замерено, список пуст.)

    Нужен там, где расстояние молчит: на странице с одной системой (Корелли,
    стр. 1 и 3: семь струнных распадались на 6 + 1 и 4 + 1 + 2) сравнить
    промежуток не с чем.
    """
    if not tall:
        return False
    last, first = upper.staffs[-1], lower.staffs[0]
    staffs = (*upper.staffs, *lower.staffs)
    unit = float(np.median([staff.average_unit_size for staff in staffs]))
    left = min(staff.min_x for staff in staffs)
    for element in tall:
        if abs(element.center[0] - left) > 4 * unit:
            continue
        top = element.center[1] - element.size[1] / 2
        bottom = element.center[1] + element.size[1] / 2
        if _share(last, top, bottom) >= 0.8 and _share(first, top, bottom) >= 0.8:
            return True
    return False


def _install_tempo_capture(image_path: str) -> None:
    """Сохранить СЫРЫЕ строки OCR, которые homr читает над верхним станом.

    `title_detection` кропает полосу над верхним станом, гоняет по ней OCR и ищет
    там заголовок. Метрономная отметка попадает в этот же кроп, но до нас не
    доезжает ни одним путём: строку без четырёх букв («♩=95») он отбрасывает как
    не-заголовок, а если отметка слиплась с текстом в одну строку OCR
    («♩ = 117 "Water"»), то её съедает `cleanup_text` — он оставляет только буквы
    и цифры, и «=» исчезает вместе с самим признаком темпа.

    Поэтому перехватываем ВСЕ строки до фильтра и до чистки. `is_tempo_marking`
    для этого удобна тем, что через неё проходит каждая строка ровно один раз;
    вердикт её мы не трогаем. OCR уже отработал — темп достаётся даром, а иначе
    за него приходится отдельно гонять Audiveris на весь лист.

    Пишем в файл рядом со входом (как homr пишет свой MusicXML) — построчно и
    сразу, потому что `detect_title` работает в отдельном потоке и процесс может
    закончиться раньше, чем мы соберём всё в память.
    """
    from homr import title_detection

    if not hasattr(title_detection, "is_tempo_marking"):
        return  # переименовали — просто останемся без бесплатного темпа

    original = title_detection.is_tempo_marking
    sidecar = Path(image_path).with_suffix(".ocr.txt")
    lock = threading.Lock()

    def capturing(text):
        if text and text.strip():
            try:
                with lock, sidecar.open("a", encoding="utf-8") as handle:
                    handle.write(" ".join(text.split()) + "\n")
            except OSError:
                pass  # не смогли записать — не повод ронять распознавание
        return original(text)

    title_detection.is_tempo_marking = capturing


if __name__ == "__main__":
    try:
        _install_canvas_guard()
    except Exception as exc:  # noqa: BLE001 — защита необязательна, запуск важнее
        print(f"canvas guard not installed: {exc}", file=sys.stderr)
    try:
        _install_brace_pairing()
    except Exception as exc:  # noqa: BLE001 — без подбора по акколаде работаем как 0.6.2
        print(f"brace pairing not installed: {exc}", file=sys.stderr)
    try:
        _install_system_rejoin()
    except Exception as exc:  # noqa: BLE001 — без склейки запуск прежний
        print(f"system rejoin not installed: {exc}", file=sys.stderr)
    try:
        _install_tempo_capture(sys.argv[1])
    except Exception as exc:  # noqa: BLE001 — то же: перехват темпа не обязателен
        print(f"tempo capture not installed: {exc}", file=sys.stderr)
    from homr.main import main

    main()
