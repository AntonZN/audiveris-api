"""Мелкая геометрия четырёхугольников и прямых. Без зависимостей кроме numpy."""

from __future__ import annotations

import numpy as np

Quad = np.ndarray  # (4, 2) float32: tl, tr, br, bl


def order_quad(points: np.ndarray) -> Quad:
    """Упорядочить 4 точки как tl, tr, br, bl.

    Сумма координат минимальна в левом-верхнем и максимальна в правом-нижнем;
    разность (y - x) разводит оставшиеся два угла. Устойчиво к любому исходному
    порядку обхода контура и к умеренному повороту.
    """
    pts = np.asarray(points, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()  # y - x
    return np.array(
        [pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]],
        dtype=np.float32,
    )


def quad_size(quad: Quad) -> tuple[int, int]:
    """Размер прямоугольника-приёмника: среднее длин противоположных сторон.

    Настоящее соотношение сторон листа из одной гомографии восстанавливается
    только с известным фокусным расстоянием. Для OMR это не нужно: важно, чтобы
    линейки стали горизонтальными и равноудалёнными, а лёгкая ошибка масштаба по
    X одинаково растягивает всю страницу и распознаванию не мешает.
    """
    tl, tr, br, bl = quad
    width = (np.linalg.norm(tr - tl) + np.linalg.norm(br - bl)) / 2
    height = (np.linalg.norm(bl - tl) + np.linalg.norm(br - tr)) / 2
    return max(int(round(width)), 1), max(int(round(height)), 1)


def quad_area(quad: Quad) -> float:
    """Площадь по формуле шнурков."""
    x, y = quad[:, 0], quad[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def is_convex(quad: Quad) -> bool:
    """True, если обход четырёхугольника не меняет знак векторного произведения."""
    signs = []
    for i in range(4):
        a, b, c = quad[i], quad[(i + 1) % 4], quad[(i + 2) % 4]
        # Векторное произведение на плоскости — скаляр; np.cross для 2D устарел.
        first, second = b - a, c - b
        signs.append(np.sign(first[0] * second[1] - first[1] * second[0]))
    return len(set(s for s in signs if s != 0)) == 1


def fit_line(points: np.ndarray) -> np.ndarray:
    """Прямая ax + by + c = 0 через облако точек (ортогональная регрессия).

    Именно ортогональная, а не «y по x»: края блока нот почти вертикальные, и
    обычный МНК по x на них разваливается.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    centroid = pts.mean(axis=0)
    _, _, vh = np.linalg.svd(pts - centroid)
    direction = vh[0]                      # главная ось облака
    normal = np.array([-direction[1], direction[0]])
    return np.array([normal[0], normal[1], -normal @ centroid])


def intersect(line1: np.ndarray, line2: np.ndarray) -> np.ndarray:
    """Точка пересечения двух прямых в однородных координатах."""
    p = np.cross(line1, line2)
    if abs(p[2]) < 1e-9:
        raise ValueError("прямые параллельны — пересечения нет")
    return np.array([p[0] / p[2], p[1] / p[2]], dtype=np.float32)


def offset_away_from(line: np.ndarray, point: np.ndarray, distance: float) -> np.ndarray:
    """Отодвинуть прямую на `distance` от `point`.

    Знак нормали у прямой произволен (зависит от порядка точек в регрессии),
    поэтому «наружу» определяем не по знаку коэффициентов, а по тому, с какой
    стороны лежит центр блока нот — так расширение рамки всегда идёт в нужную
    сторону, как бы ни легла регрессия.
    """
    a, b, c = line
    norm = float(np.hypot(a, b)) or 1.0
    signed = (a * point[0] + b * point[1] + c) / norm
    direction = 1.0 if signed > 0 else -1.0
    return np.array([a, b, c + direction * distance * norm])


def scale_quad(quad: Quad, factor: float) -> Quad:
    """Перевести четырёхугольник между рабочим и полным разрешением."""
    return (np.asarray(quad, dtype=np.float32) * float(factor)).astype(np.float32)


def fit_line_robust(points: np.ndarray, iterations: int = 3) -> np.ndarray:
    """Прямая через облако точек, устойчивая к выбросам.

    Нужна именно устойчивая: у первой нотной системы на странице левый край
    ОТСТУПЛЕН (там стоит название инструмента), и её концы линеек лежат на
    десятки пикселей правее остальных. Обычная регрессия по всем точкам из-за
    пяти таких выбросов уводит весь левый край страницы вбок.

    Схема простая и предсказуемая: фитим, считаем расстояния, выбрасываем всё
    дальше 2.5 MAD, повторяем. Обычно хватает двух итераций.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    line = fit_line(pts)
    for _ in range(iterations):
        if len(pts) <= 4:
            break
        a, b, c = line
        norm = float(np.hypot(a, b)) or 1.0
        distances = np.abs(pts @ np.array([a, b]) + c) / norm
        mad = float(np.median(np.abs(distances - np.median(distances)))) or 1e-6
        keep = distances <= np.median(distances) + 2.5 * mad
        if keep.sum() < 4 or keep.all():
            break
        pts = pts[keep]
        line = fit_line(pts)
    return line


def fit_edge_line(
    points: np.ndarray, centre: np.ndarray, keep_fraction: float = 0.5
) -> np.ndarray:
    """Прямая по ВНЕШНЕЙ границе облака точек (левый/правый край блока нот).

    Левый и правый края блока нужно проводить не «в среднем по концам линеек», а
    по самым дальним из них, и вот почему. Концы линеек систематически смещены
    ВНУТРЬ: у первой системы левый край отступлен, последняя система обрывается
    на середине строки, а на изогнутой странице детектор теряет хвост линейки у
    сгиба. Все эти ошибки — односторонние, и любая усредняющая оценка (включая
    робастную) уводит край внутрь и срезает музыку.

    Поэтому: грубо фитим прямую, оставляем половину точек, которые дальше всего
    от центра блока, и фитим окончательно уже по ним.
    """
    pts = np.asarray(points, dtype=np.float64).reshape(-1, 2)
    if len(pts) < 4:
        return fit_line(pts)
    line = fit_line(pts)
    a, b, c = line
    norm = float(np.hypot(a, b)) or 1.0
    signed = (pts @ np.array([a, b]) + c) / norm
    centre_signed = (np.asarray(centre, dtype=np.float64) @ np.array([a, b]) + c) / norm
    outward = -1.0 if centre_signed > 0 else 1.0
    score = signed * outward
    threshold = float(np.quantile(score, 1.0 - keep_fraction))
    keep = score >= threshold
    return fit_line(pts[keep]) if keep.sum() >= 2 else line
