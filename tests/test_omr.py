"""Тесты пайплайна `omr/`.

Проверяются инварианты, на которых он держится, а не «картинка стала красивее».
Каждый тест сторожит конкретную ошибку, которая уже была допущена при разработке
— см. комментарии.
"""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

try:
    import cv2

    from omr import geometry, score, staff
    from omr.config import DEFAULT
    from omr.pipeline import prepare
    from omr.merge import merge
    from omr.recognize import PageResult, _discard, _page_stem
    from omr.stages import dewarp as dewarp_stage
    from omr.stages import load, normalize, orient, rectify
    from omr.stages import pdf as pdf_stage
    from omr.stages import spread as spread_stage
except ImportError as exc:  # pragma: no cover — без OpenCV пайплайну делать нечего
    cv2 = None
    _IMPORT_ERROR = str(exc)


# ----------------------------------------------------------------------------------
# Синтетическая страница: единственный источник «истины» для тестов
# ----------------------------------------------------------------------------------

def make_page(
    width: int = 1000,
    height: int = 1400,
    staves: int = 6,
    interline: int = 12,
    thickness: int = 2,
    curve: float = 0.0,
) -> np.ndarray:
    """Белый лист с нарисованными станами по пять линеек."""
    page = np.full((height, width), 255, np.uint8)
    margin_x = int(width * 0.08)
    step = height // (staves + 1)
    for index in range(staves):
        base = step * (index + 1)
        for line in range(5):
            y = base + line * interline
            for x in range(margin_x, width - margin_x):
                offset = int(curve * np.sin(np.pi * (x - margin_x) / (width - 2 * margin_x)))
                page[y + offset : y + offset + thickness, x] = 0
    return cv2.cvtColor(page, cv2.COLOR_GRAY2BGR)


def photograph(page: np.ndarray, *, fill: float = 0.6, tilt: float = 0.15,
               rotation: float = 3.0) -> np.ndarray:
    """Снять «лист» под углом на сером столе — минимальная модель телефонного фото."""
    page_h, page_w = page.shape[:2]
    frame_w = int(page_w / fill)
    frame_h = int(page_h / fill)
    frame = np.full((frame_h, frame_w, 3), 120, np.uint8)

    dx = tilt * page_w / 2
    cx, cy = (frame_w - page_w) / 2, (frame_h - page_h) / 2
    source = np.float32([[0, 0], [page_w, 0], [page_w, page_h], [0, page_h]])
    target = np.float32([[cx + dx, cy], [cx + page_w - dx, cy],
                         [cx + page_w, cy + page_h], [cx, cy + page_h]])
    angle = np.radians(rotation)
    rot = np.float32([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
    centre = np.float32([frame_w / 2, frame_h / 2])
    target = (target - centre) @ rot.T + centre

    matrix = cv2.getPerspectiveTransform(source, target)
    warped = cv2.warpPerspective(page, matrix, (frame_w, frame_h))
    mask = cv2.warpPerspective(np.full((page_h, page_w), 255, np.uint8), matrix,
                               (frame_w, frame_h))
    return np.where(mask[..., None] > 0, warped, frame)


@unittest.skipIf(cv2 is None, "нужен opencv-python-headless")
class OmrPipelineTest(unittest.TestCase):
    """Инварианты пайплайна `omr/`, каждый сторожит конкретную ошибку."""

    # ----------------------------------------------------------------------------------
    # Геометрия
    # ----------------------------------------------------------------------------------

    def test_order_quad_is_order_independent(self) -> None:
        corners = np.array([[10, 0], [110, 20], [100, 120], [0, 100]], np.float32)
        for shift in range(4):
            rolled = np.roll(corners, shift, axis=0)
            self.assertTrue(np.allclose(geometry.order_quad(rolled), geometry.order_quad(corners)))


    def test_fit_edge_line_follows_outer_points(self) -> None:
        """Край должен идти по ВНЕШНИМ точкам.

        Стерегёт исходную ошибку: усреднение по всем концам линеек уводило правый
        край блока нот внутрь и срезало четверть музыки.
        """
        outer = np.array([[100.0, y] for y in range(0, 500, 50)])
        inner = np.array([[60.0, y] for y in range(0, 200, 50)])   # «недотянутые» концы
        points = np.vstack([outer, inner])
        centre = np.array([0.0, 250.0])
        line = geometry.fit_edge_line(points, centre)
        x_at_centre = -(line[1] * 250.0 + line[2]) / line[0]
        self.assertAlmostEqual(x_at_centre, 100.0, delta=3.0)


    def test_offset_away_from_ignores_normal_sign(self) -> None:
        """Смещение наружу не должно зависеть от того, как легла регрессия."""
        centre = np.array([0.0, 0.0])
        for line in (np.array([1.0, 0.0, -10.0]), np.array([-1.0, 0.0, 10.0])):
            moved = geometry.offset_away_from(line, centre, 5.0)
            x = -moved[2] / moved[0]
            self.assertAlmostEqual(x, 15.0, delta=1e-6)


    # ----------------------------------------------------------------------------------
    # Детекция линеек
    # ----------------------------------------------------------------------------------

    def test_finds_every_staff_on_a_clean_page(self) -> None:
        page = make_page(staves=6)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        lines, staves, interline = staff.analyse(gray)
        self.assertTrue(len(staves) == 6)
        self.assertAlmostEqual(interline, 12, delta=1.0)


    def test_thin_lines_survive_detection(self) -> None:
        """Сторожит удалённое «открытие 2x2»: оно стирало по одной линейке на стан."""
        page = make_page(staves=4, thickness=1, interline=14)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        _, staves, _ = staff.analyse(gray)
        self.assertTrue(len(staves) == 4)


    def test_detects_staves_on_a_rotated_page(self) -> None:
        page = make_page(staves=5)
        rotated = photograph(page, fill=0.9, tilt=0.0, rotation=6.0)
        gray = cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY)
        lines, staves, _ = staff.analyse(gray)
        self.assertTrue(len(staves) >= 4)
        self.assertAlmostEqual(staff.median_angle(lines), 6.0, delta=0.6)


    def test_detects_staves_on_a_curved_page(self) -> None:
        """Сторожит переход на разбор по полосам: одно глобальное ядро рвало дугу."""
        page = make_page(staves=5, curve=25.0)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        _, staves, _ = staff.analyse(gray)
        self.assertTrue(len(staves) >= 4)


    def test_extend_lines_recovers_a_truncated_tail(self) -> None:
        """Сторожит дотягивание хвостов: без него правый край страницы уезжал внутрь.

        Морфология на изогнутой странице теряет хвост линейки — а именно по концам
        линеек строится боковой край рамки.
        """
        page = make_page(staves=1, width=800, height=300)
        gray = cv2.cvtColor(page, cv2.COLOR_BGR2GRAY)
        mask = staff.ink_mask(gray)
        full = staff.detect_lines(gray, mask=mask)
        full = staff.merge_fragments(full, tolerance=5.0)
        self.assertTrue(full, "на синтетической странице должны найтись линейки")
        # Искусственно отрезаем правую треть каждой линейки.
        cut = [staff.StaffLine(line.xs[: len(line.xs) * 2 // 3],
                               line.ys[: len(line.ys) * 2 // 3], line.thickness)
               for line in full]
        restored = staff.extend_lines(cut, mask, max_drift=20.0)
        self.assertTrue(np.median([line.x1 for line in restored]) > np.median([line.x1 for line in cut]) + 50)
        self.assertAlmostEqual(
            float(np.median([line.x1 for line in restored])),
            float(np.median([line.x1 for line in full])),
            delta=15,
        )


    def test_interline_survives_lines_of_different_extent(self) -> None:
        """Интервал меряется в общем X — иначе разная длина линеек его перекашивает."""
        lines = [
            staff.StaffLine(np.arange(0, 500, dtype=np.float32),
                            np.full(500, 100 + 12 * i, np.float32), 2.0)
            for i in range(5)
        ]
        lines[0] = staff.StaffLine(np.arange(0, 120, dtype=np.float32),
                                   np.full(120, 100.0, np.float32), 2.0)
        self.assertAlmostEqual(staff.estimate_interline(lines), 12.0, delta=0.5)


    def test_interline_is_chosen_by_whether_staves_assemble(self) -> None:
        """Мода гистограммы зазоров бывает не тем пиком: если линейки местами
        разорваны, самым населённым оказывается ПОЛОВИННЫЙ зазор, окно сборки
        [0.65x, 1.45x] промахивается мимо настоящего — и станов ноль при шести
        десятках найденных линеек.

        Боевой случай (phone-07): 3.5px вместо 6.9, ноль станов вместо шести, и
        хватало разницы в один пиксель по высоте уменьшенной копии, чтобы
        результат перевернулся между машинами. Поэтому кандидат выбирается по
        тому, собираются ли на нём станы.
        """
        def wide(y: float) -> staff.StaffLine:
            xs = np.linspace(0, 400, 20)
            return staff.StaffLine(xs, np.full_like(xs, float(y)), thickness=1.0)

        def short(y: float, x0: float) -> staff.StaffLine:
            xs = np.linspace(x0, x0 + 10, 5)
            return staff.StaffLine(xs, np.full_like(xs, float(y)), thickness=1.0)

        lines = []
        for top in (0, 100, 200):                       # три стана с интервалом 12
            lines += [wide(top + i * 12) for i in range(5)]
        # Шум: тридцать коротких обрывков с зазором 5. Таких зазоров больше, чем
        # настоящих по 12, поэтому гистограмма показывает именно на них. В стан
        # они не собираются — по горизонтали не перекрываются.
        lines += [short(400 + i * 5, i * 13) for i in range(30)]

        candidates = staff.interline_candidates(lines)
        best = max(candidates, key=lambda value: len(staff.find_staves(lines, value)))

        self.assertGreater(len(candidates), 1, "одного кандидата мало, чтобы ошибиться")
        self.assertAlmostEqual(best, 12.0, delta=1.0)
        self.assertEqual(len(staff.find_staves(lines, best)), 3)
        # Самый населённый зазор здесь именно шумовой — на нём не собирается ничего.
        self.assertAlmostEqual(candidates[0], 5.0, delta=1.0)
        self.assertEqual(staff.find_staves(lines, candidates[0]), [])

    def test_line_y_at_interpolates_along_the_curve(self) -> None:
        line = staff.StaffLine(np.array([0.0, 10.0, 20.0]), np.array([0.0, 5.0, 0.0]), 2.0)
        self.assertAlmostEqual(line.y_at(5.0), 2.5, places=5)
        self.assertAlmostEqual(line.y_at(-100.0), 0.0, places=5)  # зажим на концах

    def test_page_frame_keeps_lines_that_did_not_form_a_staff(self) -> None:
        """Рамка страницы не должна отрезать систему, стан которой не собрался.

        Боевой случай — фото листа под углом: у нижнего стана перспектива
        развела интервал, пять линеек не собрались в цепочку, и рамка по
        последнему СОБРАННОМУ стану отрезала 9 тактов из 71. Толстый край листа
        при этом за линейку приниматься не должен.
        """
        from omr.stages import page as page_stage

        def line(y: float, thickness: float = 1.5, x0: float = 0.0) -> staff.StaffLine:
            xs = np.linspace(x0, 1000, 40)
            return staff.StaffLine(xs, np.full_like(xs, float(y)), thickness=thickness)

        staves = [staff.Staff([line(top + i * 9) for i in range(5)]) for top in (100, 270, 440)]
        stray = [line(620), line(633), line(647), line(656)]     # несобравшийся стан
        edge = line(760, thickness=7.0)                           # тень края листа
        title = line(20, thickness=7.0)
        lines = [l for s in staves for l in s.lines] + stray + [edge, title]

        upper, lower = page_stage.stray_extent(staves, lines, block_width=1000.0)
        self.assertIsNone(upper)
        self.assertIs(lower, stray[-1])

        # Короткие обрывки (балка, лига) рамку не раздвигают.
        beams = [line(640, x0=700)]
        self.assertEqual(
            page_stage.stray_extent(staves, [l for s in staves for l in s.lines] + beams, 1000.0),
            (None, None),
        )


    # ----------------------------------------------------------------------------------
    # Пайплайн целиком
    # ----------------------------------------------------------------------------------

    def test_orientation_fixes_a_page_scanned_sideways(self) -> None:
        """Половина реальных провалов прода — страница, положенная в сканер боком."""
        page = make_page(staves=6)
        sideways = cv2.rotate(page, cv2.ROTATE_90_COUNTERCLOCKWISE)
        gray = load.to_gray(sideways)
        _, staves, _ = staff.analyse(gray)
        self.assertTrue(len(staves) == 0, "лежащая на боку страница не должна давать станов")
        fixed, info = orient.fix_orientation(sideways, gray, len(staves), DEFAULT)
        self.assertTrue(info.rotated)
        _, staves_after, _ = staff.analyse(load.to_gray(fixed))
        self.assertTrue(len(staves_after) == 6)


    def test_orientation_picks_the_direction_by_clef_side(self) -> None:
        """Обе стороны дают горизонтальные линейки — но одна кладёт страницу вверх ногами.

        Сторону выбираем по началу стана: там ключ, и он закрывает почти всю высоту.
        Рисуем «ключ» жирным штрихом слева и проверяем, что после разворота он
        остался слева.
        """
        page = make_page(staves=6)
        height, width = page.shape[:2]
        step = height // 7
        for index in range(6):
            base = step * (index + 1)
            # Столбик во всю высоту стана в его начале — заменитель ключа.
            page[base - 12 : base + 4 * 12 + 12, int(width * 0.08) : int(width * 0.11)] = 0

        for rotation in (cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_90_COUNTERCLOCKWISE):
            sideways = cv2.rotate(page, rotation)
            gray = load.to_gray(sideways)
            _, staves, _ = staff.analyse(gray)
            fixed, info = orient.fix_orientation(sideways, gray, len(staves), DEFAULT)
            self.assertTrue(info.rotated)
            restored = load.to_gray(fixed)
            _, staves_after, _ = staff.analyse(restored)
            self.assertTrue(len(staves_after) >= 5)
            # «Ключ» обязан оказаться в левой половине, а не в правой.
            mask = staff.ink_mask(restored)
            half = mask.shape[1] // 2
            self.assertTrue(mask[:, :half].sum() > mask[:, half:].sum())


    def test_orientation_says_why_it_left_the_page_alone(self) -> None:
        """Две причины не поворачивать — «станов и так хватает» и «у повёрнутой
        не лучше» — должны различаться в логе. На проде они выглядели одинаково
        («портретная»), и разобрать по логу расхождение с локальным прогоном было
        нечем."""
        from omr.stages.orient import OrientationInfo

        skipped = OrientationInfo(False, 9, 0, tested=False).reason
        checked = OrientationInfo(False, 0, 2).reason
        turned = OrientationInfo(True, 0, 6, clockwise=False, clef_score=-0.01).reason

        self.assertIn("поворот не проверяли", skipped)
        self.assertIn("станов 9", skipped)
        self.assertIn("у повёрнутой 2", checked)
        self.assertIn("против часовой", turned)
        self.assertNotEqual(skipped, checked)

    def test_orientation_leaves_an_upright_page_alone(self) -> None:
        page = make_page(staves=6)
        gray = load.to_gray(page)
        _, staves, _ = staff.analyse(gray)
        fixed, info = orient.fix_orientation(page, gray, len(staves), DEFAULT)
        self.assertTrue(not info.rotated)
        self.assertTrue(fixed.shape == page.shape)


    def test_pipeline_straightens_a_photographed_page(self) -> None:
        photo = photograph(make_page(staves=6), fill=0.55, tilt=0.18, rotation=4.0)
        result = prepare(photo)
        self.assertTrue(not result.reverted)
        self.assertTrue(result.staves_after >= 5)
        lines, _, _ = staff.analyse(load.to_gray(result.image))
        self.assertTrue(abs(staff.median_angle(lines)) < 0.4)
        # Ради этого всё и затевалось: на снимке лист занимал 55% кадра, в выходе
        # музыка должна идти от края до края — движок ужмёт кадр до 1920 px, и всё,
        # что не музыка, — это выброшенное разрешение.
        span = np.median([line.length for line in lines]) / result.image.shape[1]
        self.assertTrue(span > 0.8)


    def test_prepared_image_is_grayscale(self) -> None:
        """Контракт: на выходе один канал — освещение выравнивается по яркости."""
        result = prepare(make_page(staves=4))
        self.assertTrue(result.image.ndim == 2)


    def test_pipeline_leaves_a_clean_scan_alone(self) -> None:
        """На ровном скане геометрия обязана промолчать: лишний ремап только мылит."""
        result = prepare(make_page(staves=6))
        applied = {report.name for report in result.stages if report.applied}
        self.assertTrue("dewarp" not in applied)
        self.assertTrue("deskew" not in applied)


    def test_pipeline_reports_every_stage(self) -> None:
        result = prepare(photograph(make_page(), fill=0.7))
        names = [report.name for report in result.stages]
        self.assertTrue(names[0] == "analyse")
        self.assertTrue({"dewarp", "deskew", "normalize"} <= set(names))
        self.assertTrue(result.report())


    def test_pipeline_survives_a_photo_without_any_music(self) -> None:
        """Мусор на входе не должен ронять пайплайн — только не сработают стадии."""
        noise = np.random.default_rng(0).integers(0, 255, (600, 800, 3), dtype=np.uint8)
        result = prepare(noise)
        self.assertTrue(result.image.size > 0)


    # ----------------------------------------------------------------------------------
    # Отдельные стадии
    # ----------------------------------------------------------------------------------

    def test_rectify_fills_the_target_rectangle(self) -> None:
        page = make_page(staves=3)
        photo = photograph(page, fill=0.5, tilt=0.2, rotation=0.0)
        quad = geometry.order_quad(np.array(
            [[200, 200], [800, 180], [820, 900], [180, 920]], np.float32))
        warped, matrix = rectify.rectify(photo, quad)
        width, height = geometry.quad_size(quad)
        self.assertTrue(warped.shape[:2] == (height, width))
        self.assertTrue(matrix.shape == (3, 3))


    def test_dewarp_flattens_a_curved_page(self) -> None:
        curved = make_page(staves=6, curve=30.0)
        flat, info = dewarp_stage.dewarp(curved, DEFAULT)
        self.assertTrue(info.applied, info.reason)
        lines, _, _ = staff.analyse(load.to_gray(flat))
        self.assertTrue(lines)
        # Кривизна должна упасть до долей интервала — иначе стан для движка всё ещё дуга.
        self.assertTrue(np.median([line.curvature_px for line in lines]) < 4.0)


    def test_dewarp_declines_on_a_flat_page(self) -> None:
        """Ремап на ровной странице — чистая потеря резкости, стадия обязана промолчать."""
        _, info = dewarp_stage.dewarp(make_page(staves=6), DEFAULT)
        self.assertTrue(not info.applied)


    def test_dewarp_needs_a_common_span(self) -> None:
        """Сторожит общую опору: без неё поле смещений разъезжалось и мазало правый край."""
        page = make_page(staves=6, curve=30.0)
        # Замазываем правую половину белым — линейки станут разной длины.
        page[:, page.shape[1] // 2 :] = 255
        flat, info = dewarp_stage.dewarp(page, DEFAULT)
        if info.applied:
            self.assertTrue(flat.shape == page.shape)


    def test_scale_for_engine_hits_the_engine_width(self) -> None:
        """Ширина — единственный ориентир: homr всё равно приведёт вход к своим 1920."""
        image = np.full((400, 8000, 3), 255, np.uint8)
        scaled, factor, note = normalize.scale_for_engine(image, interline=30.0, config=DEFAULT)
        self.assertAlmostEqual(scaled.shape[1], DEFAULT.target_width, delta=2)
        self.assertAlmostEqual(factor, DEFAULT.target_width / 8000, places=5)


    def test_scale_for_engine_flags_an_unreadably_small_staff(self) -> None:
        image = np.full((2000, 4000, 3), 255, np.uint8)
        _, _, note = normalize.scale_for_engine(image, interline=6.0, config=DEFAULT)
        self.assertTrue("мелкий" in note)


    def test_flatten_illumination_removes_a_gradient(self) -> None:
        page = cv2.cvtColor(make_page(staves=4), cv2.COLOR_BGR2GRAY)
        gradient = np.linspace(0.45, 1.0, page.shape[1], dtype=np.float32)
        shaded = np.clip(page * gradient[None, :], 0, 255).astype(np.uint8)
        flat = normalize.flatten_illumination(shaded, DEFAULT)
        paper = flat[flat > 128]
        self.assertTrue(paper.std() < 12)  # бумага стала равномерной
        self.assertTrue((flat < 100).sum() > 0)  # штрихи не съедены


    # ----------------------------------------------------------------------------------
    # Метрика
    # ----------------------------------------------------------------------------------

    def test_accuracy_is_one_for_identical_sequences(self) -> None:
        sequence = score.Sequence([("C", "4", "0", "quarter")] * 5, 2, 1)
        self.assertAlmostEqual(score.accuracy(sequence, sequence), 1.0, places=5)


    def test_accuracy_penalises_wrong_pitches(self) -> None:
        reference = score.Sequence([("C", "4", "0", "quarter")] * 4, 1, 1)
        wrong = score.Sequence([("D", "4", "0", "quarter")] * 4, 1, 1)
        self.assertAlmostEqual(score.accuracy(wrong, reference), 0.0, places=5)
        half = score.Sequence([("C", "4", "0", "quarter")] * 2
                              + [("D", "4", "0", "quarter")] * 2, 1, 1)
        self.assertAlmostEqual(score.accuracy(half, reference), 0.5, places=5)

@unittest.skipIf(cv2 is None, "нужен opencv-python-headless")
class EngineRetryTest(unittest.TestCase):
    """Второй заход движком по странице КАК ЕСТЬ, когда подготовка не помогла.

    Для снимка второй заход есть и снаружи (homr на сыром файле), а для листа PDF
    снаружи его быть не может — homr не читает PDF. Поэтому он сделан внутри
    пайплайна, и эти тесты сторожат, что он делается ровно когда надо.
    """

    def setUp(self) -> None:
        if cv2 is None:
            self.skipTest(_IMPORT_ERROR)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.page = self.out / "page.png"
        cv2.imwrite(str(self.page), make_page())

    def _engine_result(self, stem: str):
        from omr.engines.homr_engine import EngineResult

        musicxml = self.out / f"{stem}.musicxml"
        musicxml.write_text("<score-partwise><part id='P1'/></score-partwise>")
        log = self.out / f"{stem}.engine.log"
        log.write_text("ok")
        return EngineResult(musicxml, log, 0.1, [])

    def test_retries_the_page_as_shot_when_the_engine_crashed(self) -> None:
        """Отличаем заходы по типу входа: подготовленная страница приезжает
        массивом, а страница «как есть» — путём к файлу."""
        from omr.engines import homr_engine
        from omr.recognize import recognize

        def engine(image, output_dir, *, timeout, stem):
            if isinstance(image, np.ndarray):
                raise homr_engine.EngineError("движок упал на подготовленной")
            return self._engine_result(stem)

        with unittest.mock.patch.object(homr_engine, "run", side_effect=engine):
            result = recognize(self.page, self.out, DEFAULT, timeout=5)

        self.assertIsNotNone(result.musicxml)
        self.assertIn("без подготовки", result.pages[0].origin)

    def test_does_not_retry_a_page_without_music(self) -> None:
        """«Нот не найдено» — это не падение движка: повторять нечего и незачем,
        а второй заход стоит ещё одного прогона движка по каждой странице."""
        from omr.engines import homr_engine
        from omr.recognize import recognize

        blank = self.out / "blank.png"
        cv2.imwrite(str(blank), np.full((800, 600, 3), 255, np.uint8))

        with unittest.mock.patch.object(homr_engine, "run") as engine:
            result = recognize(blank, self.out, DEFAULT, timeout=5)

        engine.assert_not_called()
        self.assertIsNone(result.musicxml)

    def test_gives_up_after_the_second_attempt(self) -> None:
        """Движок падает всегда — заходов ровно два, рекурсии нет."""
        from omr.engines import homr_engine
        from omr.recognize import recognize

        with unittest.mock.patch.object(
            homr_engine, "run", side_effect=homr_engine.EngineError("всегда падает")
        ) as engine:
            result = recognize(self.page, self.out, DEFAULT, timeout=5)

        self.assertIsNone(result.musicxml)
        self.assertEqual(engine.call_count, 2)


@unittest.skipIf(cv2 is None, "нужен opencv-python-headless")
class LostStaffRetryTest(unittest.TestCase):
    """Повтор движка, когда homr нашёл станов меньше, чем наш детектор.

    Боевой случай — скриншот Шопена op.10 №3: homr увидел 9 станов из 10, одна
    рука системы пропала (91% -> 80%). На тех же пикселях сбой повторяется, на
    слегка размытых — нет.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name)
        self.page = self.out / "page.png"
        cv2.imwrite(str(self.page), make_page(staves=6))

    def _engine(self, staffs_by_attempt: dict[str, int]):
        """Фейковый движок: число станов зависит от того, какой это заход."""
        from omr.engines.homr_engine import EngineResult

        calls: list[str] = []

        def run(image, output_dir, *, timeout, stem):
            attempt = stem.split(".retry-")[1] if ".retry-" in stem else "first"
            calls.append(attempt)
            musicxml = Path(output_dir) / f"{stem}.musicxml"
            musicxml.write_text(f"<score-partwise><!-- {attempt} --></score-partwise>")
            log = Path(output_dir) / f"{stem}.engine.log"
            log.write_text(attempt)
            return EngineResult(musicxml, log, 1.0, [], staffs_by_attempt[attempt])

        return run, calls

    def _recognize(self, staffs_by_attempt):
        from omr.engines import homr_engine
        from omr.recognize import recognize

        run, calls = self._engine(staffs_by_attempt)
        with unittest.mock.patch.object(homr_engine, "run", side_effect=run):
            result = recognize(self.page, self.out, DEFAULT, timeout=5)
        return result, calls

    def test_takes_the_retry_that_found_the_lost_staff(self) -> None:
        result, calls = self._recognize({"first": 5, "blur": 6, "pad": 6})
        self.assertEqual(calls, ["first", "blur"])            # на первом удачном остановились
        self.assertIn("blur", result.musicxml.read_text())    # под обычным именем — повтор
        self.assertEqual(sorted(p.name for p in self.out.glob("*.musicxml")), ["page.musicxml"])
        self.assertIn("взят повтор", result.pages[0].engine_note)

    def test_no_retry_when_homr_saw_every_staff(self) -> None:
        result, calls = self._recognize({"first": 6})
        self.assertEqual(calls, ["first"])
        self.assertEqual(result.pages[0].engine_note, "")

    def test_keeps_the_first_attempt_when_retries_do_not_help(self) -> None:
        """Ложная тревога (наш детектор насчитал лишний стан): повторы стоят
        времени, но результат остаётся прежним, а их файлы не остаются в выходе."""
        result, calls = self._recognize({"first": 5, "blur": 5, "pad": 4})
        self.assertEqual(calls, ["first", "blur", "pad"])
        self.assertIn("first", result.musicxml.read_text())
        self.assertEqual(sorted(p.name for p in self.out.glob("*.musicxml")), ["page.musicxml"])
        self.assertIn("оставлен первый заход", result.pages[0].engine_note)


class MergeTest(unittest.TestCase):
    """Склейка постраничных MusicXML. Главная опасность — разное число партий."""

    @staticmethod
    def page(parts: int, measures: int, first_number: int = 1) -> str:
        blocks = []
        for index in range(1, parts + 1):
            body = "".join(
                f'<measure number="{first_number + m}">'
                '<attributes><divisions>4</divisions>'
                "<time><beats>4</beats><beat-type>4</beat-type></time></attributes>"
                "<note><pitch><step>C</step><octave>4</octave></pitch>"
                "<duration>16</duration><type>whole</type></note></measure>"
                for m in range(measures)
            )
            blocks.append(f'<part id="P{index}">{body}</part>')
        listing = "".join(
            f'<score-part id="P{i}"><part-name>P{i}</part-name></score-part>'
            for i in range(1, parts + 1)
        )
        return ('<?xml version="1.0" encoding="UTF-8"?><score-partwise version="4.0">'
                f"<part-list>{listing}</part-list>{''.join(blocks)}</score-partwise>")

    def write_pages(self, directory, specs) -> list:
        paths = []
        for index, (parts, measures) in enumerate(specs, start=1):
            path = Path(directory) / f"page{index}.musicxml"
            path.write_text(self.page(parts, measures))
            paths.append(path)
        return paths

    def test_appends_measures_across_pages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pages = self.write_pages(tmp, [(1, 4), (1, 3)])
            target = Path(tmp) / "merged.musicxml"
            report = merge(pages, target)
            self.assertEqual(report.measures, 7)
            root = ET.parse(target).getroot()
            numbers = [m.get("number") for m in root.find("part").findall("measure")]
            # Номера обязаны стать сквозными: у каждой страницы они начинались с 1.
            self.assertEqual(numbers, [str(i) for i in range(1, 8)])

    def test_pads_pages_that_have_fewer_parts(self) -> None:
        """Сторожит главную ловушку: страницы с разным числом партий.

        homr выводит партии из того, что видно на конкретном листе. Без добивки
        паузами партии разъедутся по длине, и партитура станет рваной.
        """
        with tempfile.TemporaryDirectory() as tmp:
            pages = self.write_pages(tmp, [(2, 4), (1, 3)])
            target = Path(tmp) / "merged.musicxml"
            report = merge(pages, target)
            self.assertEqual(report.parts, 2)
            self.assertEqual(report.padded_measures, 3)
            lengths = [
                len(part.findall("measure"))
                for part in ET.parse(target).getroot().findall("part")
            ]
            self.assertEqual(lengths, [7, 7])

    def test_drops_a_part_that_is_mostly_padding(self) -> None:
        """Партия, найденная на одной странице из четырёх, — артефакт детекции.

        Оставить её значит отдать партитуру с инструментом, который молчит почти
        всю пьесу. На реальном `pdf-01` так и выходило: 66 тактов пауз из 76.
        """
        with tempfile.TemporaryDirectory() as tmp:
            pages = self.write_pages(tmp, [(2, 5), (1, 5), (1, 5), (1, 5)])
            target = Path(tmp) / "merged.musicxml"
            report = merge(pages, target)
            self.assertEqual(report.dropped_parts, 1)
            self.assertEqual(report.parts, 1)
            self.assertEqual(report.padded_measures, 0)
            root = ET.parse(target).getroot()
            self.assertEqual(len(root.findall("part")), 1)
            self.assertEqual(len(root.findall("part-list/score-part")), 1)

    def test_keeps_a_part_that_is_only_briefly_absent(self) -> None:
        """Настоящий второй голос молчит лишь местами — его выбрасывать нельзя."""
        with tempfile.TemporaryDirectory() as tmp:
            pages = self.write_pages(tmp, [(2, 5), (2, 5), (1, 4), (2, 5)])
            target = Path(tmp) / "merged.musicxml"
            report = merge(pages, target)
            self.assertEqual(report.dropped_parts, 0)
            self.assertEqual(report.parts, 2)

    def test_never_drops_the_last_part(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pages = self.write_pages(tmp, [(1, 5)])
            target = Path(tmp) / "merged.musicxml"
            report = merge(pages, target, max_padding=0.0)
            self.assertEqual(report.parts, 1)

    def test_grows_to_the_widest_page(self) -> None:
        """Если на второй странице партий БОЛЬШЕ, партитура должна дорасти."""
        with tempfile.TemporaryDirectory() as tmp:
            pages = self.write_pages(tmp, [(1, 4), (2, 4)])
            target = Path(tmp) / "merged.musicxml"
            report = merge(pages, target)
            self.assertEqual(report.parts, 2)
            root = ET.parse(target).getroot()
            self.assertEqual(len(root.findall("part")), 2)
            self.assertEqual(len(root.findall("part-list/score-part")), 2)
            lengths = [len(p.findall("measure")) for p in root.findall("part")]
            self.assertEqual(lengths, [8, 8])


class PdfDetectionTest(unittest.TestCase):
    """Определение PDF по сигнатуре: расширение файла нам не подконтрольно."""

    def test_detects_pdf_by_signature_not_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "score.png"
            fake.write_bytes(b"%PDF-1.7\nrest")
            self.assertTrue(pdf_stage.is_pdf(fake))

    def test_rejects_non_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "score.pdf"
            image.write_bytes(b"\x89PNG\r\n\x1a\n")
            self.assertFalse(pdf_stage.is_pdf(image))

    def test_missing_file_is_not_a_pdf(self) -> None:
        self.assertFalse(pdf_stage.is_pdf(Path("/nonexistent/x.pdf")))


@unittest.skipIf(cv2 is None, "нужен opencv-python-headless")
class SpreadTest(unittest.TestCase):
    """Разворот книги: две страницы в кадре режем по корешку."""

    @staticmethod
    def spread_page(gap: int = 120) -> np.ndarray:
        """Два листа рядом с промежутком между ними."""
        left = make_page(width=700, height=1000, staves=5)
        right = make_page(width=700, height=1000, staves=5)
        gutter = np.full((1000, gap, 3), 255, np.uint8)
        return np.hstack([left, gutter, right])

    def test_finds_the_gutter_between_two_pages(self) -> None:
        image = self.spread_page()
        info = spread_stage.detect(image, DEFAULT)
        self.assertTrue(info.is_spread, info.reason)
        # Корешок посередине: 700 + 60.
        self.assertAlmostEqual(info.split_x, 760, delta=90)
        self.assertLess(info.valley_ratio, DEFAULT.spread_gutter_ratio)

    def test_leaves_a_single_page_alone(self) -> None:
        info = spread_stage.detect(make_page(staves=8), DEFAULT)
        self.assertFalse(info.is_spread, info.reason)

    def test_split_produces_two_halves_covering_the_frame(self) -> None:
        image = self.spread_page()
        info = spread_stage.detect(image, DEFAULT)
        halves = spread_stage.split(image, info.split_x)
        self.assertEqual(len(halves), 2)
        self.assertEqual(halves[0].shape[1] + halves[1].shape[1], image.shape[1])
        for half in halves:
            _, staves, _ = staff.analyse(load.to_gray(half))
            self.assertGreaterEqual(len(staves), 4)

    def test_each_half_gets_the_full_engine_width(self) -> None:
        """Ради этого всё и делалось: на развороте на стан приходилось вдвое меньше пикселей."""
        image = self.spread_page()
        whole = prepare(image)
        info = spread_stage.detect(image, DEFAULT)
        half = prepare(spread_stage.split(image, info.split_x)[0])
        lines_whole, _, interline_whole = staff.analyse(load.to_gray(whole.image))
        lines_half, _, interline_half = staff.analyse(load.to_gray(half.image))
        self.assertGreater(interline_half, interline_whole * 1.4)

    def test_ignores_a_frame_without_enough_lines(self) -> None:
        """Безопасный отказ: пустой кадр не должен объявляться разворотом."""
        blank = np.full((600, 900, 3), 255, np.uint8)
        info = spread_stage.detect(blank, DEFAULT)
        self.assertFalse(info.is_spread)


class PageNamingTest(unittest.TestCase):
    """Индекс в имени появляется, только когда страниц действительно несколько.

    Сторожит перекос, который был: `multipage` считался по ПЛАНУ разрезки, и
    после отката разворота единственная страница всё равно называлась `.p01`.
    """

    def test_single_page_keeps_the_plain_name(self) -> None:
        self.assertEqual(_page_stem(Path("score.png"), 1, indexed=False), "score")

    def test_multiple_pages_are_indexed(self) -> None:
        self.assertEqual(_page_stem(Path("score.pdf"), 3, indexed=True), "score.p03")

    def test_discard_removes_the_artefacts_of_a_cancelled_page(self) -> None:
        """Брошенный в выходе MusicXML отменённой половины — не мусор, а неверный файл."""
        with tempfile.TemporaryDirectory() as tmp:
            musicxml = Path(tmp) / "half.musicxml"
            clean = Path(tmp) / "half.clean.png"
            for path in (musicxml, clean):
                path.write_text("x")
            page = PageResult(number=1, source=Path("half.png"))
            page.musicxml, page.clean_image = musicxml, clean
            _discard([page])
            self.assertFalse(musicxml.exists())
            self.assertFalse(clean.exists())

    def test_discard_survives_already_missing_files(self) -> None:
        page = PageResult(number=1, source=Path("x.png"))
        page.musicxml = Path("/nonexistent/x.musicxml")
        _discard([page])   # не должно бросить


@unittest.skipIf(cv2 is None, "нужен opencv-python-headless")
class HomrEngineExitTest(unittest.TestCase):
    """Падение homr ПОСЛЕ записи результата — не провал распознавания.

    Под нагрузкой onnxruntime абортится на выходе из процесса (`recursive_mutex
    lock failed`, код -6), когда MusicXML уже записан. Выбрасывать готовую
    страницу из-за этого нельзя; а вот падение ДО записи — по-прежнему провал.
    """

    @staticmethod
    def _fake_homr(returncode: int, stderr: str, write: bool):
        import subprocess

        def run(command, timeout):
            if write:
                Path(command[2]).with_suffix(".musicxml").write_text("<score-partwise/>")
            return subprocess.CompletedProcess(command, returncode, "", stderr)

        return run

    def _run(self, fake):
        from omr.engines import homr_engine

        page = np.full((40, 40, 3), 255, np.uint8)
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(homr_engine, "_run", side_effect=fake):
            result = homr_engine.run(page, Path(tmp), stem="p")
            return result, result.log.read_text()

    def test_accepts_the_result_when_homr_dies_after_writing_it(self) -> None:
        result, log = self._run(self._fake_homr(
            -6, "Result was written to p.musicxml\nlibc++abi: terminating", write=True))
        self.assertEqual(result.musicxml.name, "p.musicxml")
        self.assertIn("результат принят", log)

    def test_crash_before_writing_is_still_a_failure(self) -> None:
        from omr.engines import homr_engine

        with self.assertRaises(homr_engine.EngineError):
            self._run(self._fake_homr(-6, "libc++abi: terminating", write=False))

    def test_reports_how_many_staves_homr_kept(self) -> None:
        """Найденные минус дубликаты — так у Брамса: 13 найдено, 1 дубль, станов 12."""
        result, _ = self._run(self._fake_homr(
            0, "Found 1285 staff line fragments\nFound 13 staffs\nRemoved 1 duplicate staffs\n"
               "Found 4 connected staffs\nResult was written to p.musicxml", write=True))
        self.assertEqual(result.staffs, 12)

    def test_file_without_homr_confirmation_is_not_trusted(self) -> None:
        """Файл есть, но homr не сказал, что дописал его, — мог оборваться посередине."""
        from omr.engines import homr_engine

        with self.assertRaises(homr_engine.EngineError):
            self._run(self._fake_homr(-11, "Segmentation fault", write=True))


if __name__ == "__main__":
    unittest.main()
