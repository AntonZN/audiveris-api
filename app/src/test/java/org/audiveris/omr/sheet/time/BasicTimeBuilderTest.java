//------------------------------------------------------------------------------------------------//
//                                                                                                //
//                             B a s i c T i m e B u i l d e r T e s t                            //
//                                                                                                //
//------------------------------------------------------------------------------------------------//
// <editor-fold defaultstate="collapsed" desc="hdr">
//
//  Copyright © Audiveris 2026. All rights reserved.
//
//  This program is free software: you can redistribute it and/or modify it under the terms of the
//  GNU Affero General Public License as published by the Free Software Foundation, either version
//  3 of the License, or (at your option) any later version.
//
//  This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY;
//  without even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
//  See the GNU Affero General Public License for more details.
//
//  You should have received a copy of the GNU Affero General Public License along with this
//  program.  If not, see <http://www.gnu.org/licenses/>.
//------------------------------------------------------------------------------------------------//
// </editor-fold>
package org.audiveris.omr.sheet.time;

import org.audiveris.omr.score.TimeRational;
import static org.audiveris.omr.sheet.time.TimeBuilder.TimeKind.DEN;
import static org.audiveris.omr.sheet.time.TimeBuilder.TimeKind.NUM;
import org.audiveris.omr.sig.inter.AbstractTimeInter;

import static org.junit.Assert.*;
import org.junit.Test;

import java.awt.Rectangle;
import java.util.ArrayList;
import java.util.List;

/**
 * Tests for the lookup of time numbers within a combo time signature found outside the header.
 * <p>
 * In Stinkfist (PDMX), the tail of the 7 touches the 4 of a mid-staff 7/4: the symbols step sees
 * a single glyph and can only map it to a predefined combo shape, so 7/4 was read 2/4 and the
 * measure was exported with half of its beats.
 */
public class BasicTimeBuilderTest
{
    /** The 7/4 glyph of Stinkfist page 1, staff 5, and its staff (interline 21). */
    private static final Rectangle GLYPH_74 = new Rectangle(1186, 1617, 33, 86);

    private static final int STAFF_TOP = 1617;

    private static final int STAFF_HEIGHT = 86;

    /** 7/4 must be a supported pair, otherwise its numbers exclude each other. */
    @Test
    public void testSevenFourIsSupported ()
    {
        assertTrue(AbstractTimeInter.isSupported(new TimeRational(7, 4)));
        assertTrue(AbstractTimeInter.isSupported(new TimeRational(6, 4)));
        assertFalse(AbstractTimeInter.isSupported(new TimeRational(8, 4)));
    }

    /** Halves are split by staff middle line, within the glyph abscissa range. */
    @Test
    public void testHalfAreas ()
    {
        final Rectangle num = BasicTimeBuilder.halfArea(NUM, GLYPH_74, STAFF_TOP, STAFF_HEIGHT);
        final Rectangle den = BasicTimeBuilder.halfArea(DEN, GLYPH_74, STAFF_TOP, STAFF_HEIGHT);
        final int middle = STAFF_TOP + (STAFF_HEIGHT / 2);

        assertEquals(GLYPH_74.x, num.x);
        assertEquals(GLYPH_74.width, num.width);
        assertEquals(GLYPH_74.x, den.x);
        assertEquals(GLYPH_74.width, den.width);

        assertEquals(STAFF_TOP, num.y);
        assertEquals(middle, num.y + num.height);
        assertEquals(middle, den.y);
        assertEquals(STAFF_TOP + STAFF_HEIGHT, den.y + den.height);
        assertFalse(num.intersects(den));
    }

    /** With an odd height, the extra row goes to neither half, as in staff header. */
    @Test
    public void testHalfAreasOddHeight ()
    {
        final Rectangle num = BasicTimeBuilder.halfArea(NUM, GLYPH_74, STAFF_TOP, 85);
        final Rectangle den = BasicTimeBuilder.halfArea(DEN, GLYPH_74, STAFF_TOP, 85);

        assertEquals(42, num.height);
        assertEquals(42, den.height);
        assertEquals(STAFF_TOP + 43, den.y);
        assertFalse(num.intersects(den));
    }

    /** Several shapes assigned to the same glyph give one box, so halves are looked up once. */
    @Test
    public void testSameGlyphGivesOneBox ()
    {
        final List<Rectangle> boxes = new ArrayList<>();
        BasicTimeBuilder.addComboBox(boxes, GLYPH_74); // As 2/4
        BasicTimeBuilder.addComboBox(boxes, GLYPH_74); // As 4/4

        assertEquals(1, boxes.size());
        assertEquals(GLYPH_74, boxes.get(0));
    }

    /** Overlapping glyphs are merged, a time signature farther in the stack is kept apart. */
    @Test
    public void testOverlappingBoxesMerged ()
    {
        final List<Rectangle> boxes = new ArrayList<>();
        final Rectangle part = new Rectangle(1186, 1617, 30, 43);
        final Rectangle far = new Rectangle(1923, 1617, 36, 86);
        BasicTimeBuilder.addComboBox(boxes, part);
        BasicTimeBuilder.addComboBox(boxes, GLYPH_74);
        BasicTimeBuilder.addComboBox(boxes, far);

        assertEquals(2, boxes.size());
        assertEquals(GLYPH_74, boxes.get(0));
        assertEquals(far, boxes.get(1));
    }

    /** The caller's rectangle is not modified by a later merge. */
    @Test
    public void testInputBoxNotModified ()
    {
        final List<Rectangle> boxes = new ArrayList<>();
        final Rectangle part = new Rectangle(1186, 1617, 30, 43);
        BasicTimeBuilder.addComboBox(boxes, part);
        BasicTimeBuilder.addComboBox(boxes, GLYPH_74);

        assertEquals(new Rectangle(1186, 1617, 30, 43), part);
    }
}
