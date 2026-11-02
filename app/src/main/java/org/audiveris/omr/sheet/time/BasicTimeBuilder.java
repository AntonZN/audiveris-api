//------------------------------------------------------------------------------------------------//
//                                                                                                //
//                                 B a s i c T i m e B u i l d e r                                //
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

import org.audiveris.omr.glyph.Glyph;
import org.audiveris.omr.glyph.ShapeSet;
import org.audiveris.omr.sheet.Staff;
import static org.audiveris.omr.sheet.time.TimeBuilder.TimeKind.DEN;
import static org.audiveris.omr.sheet.time.TimeBuilder.TimeKind.NUM;
import org.audiveris.omr.sig.inter.Inter;
import org.audiveris.omr.sig.inter.TimeNumberInter;
import org.audiveris.omr.sig.inter.TimeWholeInter;
import org.audiveris.omr.util.VerticalSide;

import java.awt.Rectangle;
import java.util.ArrayList;
import java.util.List;

/**
 * A subclass of TimeBuilder specifically meant for extraction outside system header,
 * further down in the system measures.
 * <p>
 * Symbol extraction has already been performed, so time-signature shaped symbols are now
 * checked for consistency across all staves of the containing system.
 * <p>
 * Within the area of a combo candidate (like 2/4), top and bottom numbers are also looked up
 * separately, as is done in staff header.
 *
 * @author Hervé Bitteur
 */
public class BasicTimeBuilder
        extends TimeBuilder
{
    //~ Instance fields ----------------------------------------------------------------------------

    /** Numbers looked up within combo candidates, these are not part of column time set. */
    private final List<Inter> halves = new ArrayList<>();

    //~ Constructors -------------------------------------------------------------------------------

    /**
     * Creates a new <code>BasicTimeBuilder</code> object.
     *
     * @param staff  underlying staff
     * @param column containing time column
     */
    public BasicTimeBuilder (Staff staff,
                             BasicTimeColumn column)
    {
        super(staff, column);
    }

    //~ Methods ------------------------------------------------------------------------------------

    @Override
    public void cleanup ()
    {
        throw new UnsupportedOperationException("Not supported yet.");
    }

    @Override
    protected void findCandidates ()
    {
        // For time symbols found (whole or half), pitch is correct, but abscissa is random
        // For now, at staff level, we can only check that nums & dens are x-compatible
        BasicTimeColumn basicColumn = (BasicTimeColumn) column;
        List<Rectangle> comboBoxes = new ArrayList<>();

        for (Inter inter : basicColumn.timeSet) {
            if (inter.getStaff() == staff) {
                if (inter instanceof TimeWholeInter) {
                    wholes.add(inter);

                    if (!ShapeSet.SingleWholeTimes.contains(inter.getShape())) {
                        final Glyph glyph = inter.getGlyph();
                        final Rectangle box = (glyph != null) ? glyph.getBounds()
                                : inter.getBounds();
                        addComboBox(comboBoxes, box);
                    }
                } else if (inter instanceof TimeNumberInter) {
                    TimeNumberInter number = (TimeNumberInter) inter;
                    VerticalSide side = number.getSide();

                    if (side == VerticalSide.TOP) {
                        nums.add(inter);
                    } else {
                        dens.add(inter);
                    }
                }
            }
        }

        // When top and bottom numbers touch, the symbols step sees one glyph and can only map it
        // to a predefined combo shape: 7/4 for example has none and comes out as 2/4.
        // So, within each combo candidate, we look for the numbers separately.
        for (Rectangle box : comboBoxes) {
            lookupHalves(box);
        }
    }

    //--------------//
    // lookupHalves //
    //--------------//
    /**
     * Look for top and bottom numbers within the abscissa range of a combo candidate,
     * the staff height being split by staff middle line.
     *
     * @param box the glyph bounds of combo candidate
     */
    private void lookupHalves (Rectangle box)
    {
        final int xMid = box.x + (box.width / 2);
        final int top = staff.getFirstLine().yAt(xMid);
        final int height = staff.getLastLine().yAt(xMid) - top + 1;

        for (TimeKind half : new TimeKind[] { NUM, DEN }) {
            final List<Inter> inters = (half == NUM) ? nums : dens;
            final int count = inters.size();
            final Rectangle rect = halfArea(half, box, top, height);
            rect.grow(0, -params.yMargin);
            lookupHalf(half, rect);
            halves.addAll(inters.subList(count, inters.size()));
        }
    }

    //--------------//
    // removeHalves //
    //--------------//
    /**
     * Remove the numbers looked up within combo candidates.
     */
    void removeHalves ()
    {
        for (Inter inter : halves) {
            inter.remove();
        }
    }

    //~ Static Methods -----------------------------------------------------------------------------

    //-------------//
    // addComboBox //
    //-------------//
    /**
     * Include the glyph bounds of a combo candidate into the boxes to search for halves.
     * <p>
     * Several shapes may have been assigned to the same glyph, so overlapping boxes are merged.
     *
     * @param boxes    the boxes so far
     * @param comboBox the glyph bounds of combo candidate
     */
    static void addComboBox (List<Rectangle> boxes,
                             Rectangle comboBox)
    {
        for (Rectangle box : boxes) {
            if (box.intersects(comboBox)) {
                box.add(comboBox);
                return;
            }
        }

        boxes.add(new Rectangle(comboBox));
    }

    //----------//
    // halfArea //
    //----------//
    /**
     * Report the area of top or bottom half, within the abscissa range of a combo candidate.
     * <p>
     * As in staff header, the staff height is split by staff middle line.
     *
     * @param half   NUM or DEN
     * @param box    the glyph bounds of combo candidate
     * @param top    ordinate of staff first line
     * @param height staff height, from first to last line
     * @return the half area
     */
    static Rectangle halfArea (TimeKind half,
                               Rectangle box,
                               int top,
                               int height)
    {
        final int y = top + ((half == NUM) ? 0 : (height - (height / 2)));

        return new Rectangle(box.x, y, box.width, height / 2);
    }
}
