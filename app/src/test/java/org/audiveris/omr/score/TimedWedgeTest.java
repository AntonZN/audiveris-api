//------------------------------------------------------------------------------------------------//
//                                                                                                //
//                                   T i m e d W e d g e T e s t                                  //
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
package org.audiveris.omr.score;

import org.audiveris.omr.math.Rational;
import org.audiveris.omr.score.PartwiseBuilder.TimedWedge;
import org.audiveris.omr.util.HorizontalSide;
import static org.audiveris.omr.util.HorizontalSide.LEFT;
import static org.audiveris.omr.util.HorizontalSide.RIGHT;

import static org.junit.Assert.*;
import org.junit.Test;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.List;

/**
 * Tests for the ordering of wedge events in {@link PartwiseBuilder}.
 * <p>
 * A wedge end may be linked to a chord off the time slots (a grace chord, or a false small head
 * read in a time signature), which has no time offset. The former comparator dereferenced that
 * null offset and the NullPointerException aborted the export of the whole measure.
 */
public class TimedWedgeTest
{
    private static TimedWedge event (HorizontalSide side,
                                     Rational timeOffset)
    {
        return new TimedWedge(null, side, null, timeOffset);
    }

    private static Rational r (int num,
                               int den)
    {
        return new Rational(num, den);
    }

    /** The comparator as it was, kept to show the crash. */
    @Test(expected = NullPointerException.class)
    public void formerComparatorCrashesOnUnknownTime ()
    {
        final List<TimedWedge> events = new ArrayList<>(
                Arrays.asList(event(RIGHT, r(1, 2)), event(RIGHT, null)));

        Collections.sort(events, (w1, w2) -> {
            int cmp = w1.timeOffset.compareTo(w2.timeOffset);
            if (cmp != 0) {
                return cmp;
            }
            if (w1.side == w2.side) {
                return 0;
            }
            return (w1.side == LEFT) ? -1 : 1;
        });
    }

    /** Unknown start goes first, unknown stop goes last, start before stop at same time. */
    @Test
    public void orderToleratesUnknownTime ()
    {
        final TimedWedge stopUnknown = event(RIGHT, null);
        final TimedWedge stopHalf = event(RIGHT, r(1, 2));
        final TimedWedge stopQuarter = event(RIGHT, r(1, 4));
        final TimedWedge startQuarter = event(LEFT, r(1, 4));
        final TimedWedge startUnknown = event(LEFT, null);
        final List<TimedWedge> expected = List.of(
                startUnknown,
                startQuarter,
                stopQuarter,
                stopHalf,
                stopUnknown);

        for (int shift = 0; shift < expected.size(); shift++) {
            final List<TimedWedge> events = new ArrayList<>(expected);
            Collections.rotate(events, shift);
            Collections.reverse(events);
            Collections.sort(events, TimedWedge.ORDER);
            assertEquals(expected, events);
        }
    }

    /** A start of unknown time is due at once, a stop of unknown time only at measure end. */
    @Test
    public void unknownTimeIsDueAtMeasureBounds ()
    {
        assertTrue(event(LEFT, null).isDueAt(Rational.ZERO));
        assertFalse(event(RIGHT, null).isDueAt(r(100, 1)));
        assertTrue(event(RIGHT, r(1, 4)).isDueAt(r(1, 4)));
        assertFalse(event(RIGHT, r(1, 2)).isDueAt(r(1, 4)));
    }
}
