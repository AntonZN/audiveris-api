//------------------------------------------------------------------------------------------------//
//                                                                                                //
//                                 S t e m I t e m S o r t T e s t                                //
//                                                                                                //
//------------------------------------------------------------------------------------------------//
package org.audiveris.omr.sheet.stem;

import org.audiveris.omr.glyph.Glyph;
import org.audiveris.omr.run.Orientation;
import org.audiveris.omr.run.RunTable;
import org.audiveris.omr.sheet.stem.StemItem.GapItem;
import org.audiveris.omr.sheet.stem.StemItem.HalfLinkerItem;
import org.audiveris.omr.sig.inter.Inter;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.junit.Assert.fail;
import org.junit.Test;

import java.awt.geom.Area;
import java.awt.geom.Line2D;
import java.awt.geom.Point2D;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Random;

/**
 * Tests for the ordinate sorting of {@link StemItem} instances in {@link StemBuilder}.
 * <p>
 * The former comparator was pair-dependent: two {@link HalfLinkerItem}s compared on their
 * linker reference-point ordinate, while every other pair compared on line endpoints.
 * Such a comparator is not transitive; TimSort can detect this at merge time and throw
 * <code>IllegalArgumentException: "Comparison method violates its general contract!"</code>.
 * (Lists shorter than TimSort's MIN_MERGE of 32 elements are sorted via binary insertion,
 * which never checks the contract, so real crashes require larger item lists.)
 */
public class StemItemSortTest
{
    //~ Methods ------------------------------------------------------------------------------------

    /**
     * Demonstrate the former crash: the pair-dependent comparator, applied to a 40-item
     * list of plausible stem items, makes TimSort detect the contract violation and throw.
     */
    @Test
    public void formerComparatorCrashesTimSort ()
    {
        final List<StemItem> items = buildAdversarialItems(21, 40);

        try {
            Collections.sort(items, formerComparator(1));
            fail("Former pair-dependent comparator should violate the sorting contract");
        } catch (IllegalArgumentException ex) {
            assertEquals("Comparison method violates its general contract!", ex.getMessage());
        }
    }

    /**
     * The fixed comparator sorts the very same adversarial list, in both directions and
     * under many input permutations, and the result is pairwise consistent.
     */
    @Test
    public void ordinateComparatorSortsAdversarialList ()
    {
        for (int yDir : new int[] { 1, -1 }) {
            final Comparator<StemItem> comparator = StemBuilder.ordinateComparator(yDir);
            final Random rnd = new Random(4285);

            for (int shuffle = 0; shuffle < 100; shuffle++) {
                final List<StemItem> items = buildAdversarialItems(21, 40);
                Collections.shuffle(items, rnd);

                Collections.sort(items, comparator); // Must not throw

                for (int i = 0; i < items.size(); i++) {
                    for (int j = i + 1; j < items.size(); j++) {
                        assertTrue(
                                "Sorted order must be pairwise consistent",
                                comparator.compare(items.get(i), items.get(j)) <= 0);
                    }
                }
            }
        }
    }

    //~ Helper methods -----------------------------------------------------------------------------

    /** Deterministic mix of half-linker items (with stumps) and gap items. */
    private static List<StemItem> buildAdversarialItems (long seed,
                                                         int n)
    {
        final Random rnd = new Random(seed);
        final List<StemItem> items = new ArrayList<>();

        for (int i = 0; i < n; i++) {
            if (rnd.nextBoolean()) {
                final double refY = rnd.nextInt(1000);
                final int stumpTop = rnd.nextInt(1000);
                items.add(
                        new HalfLinkerItem(
                                new FakeHalfLinker(refY, makeStump(100, stumpTop, 10)),
                                10));
            } else {
                final int y1 = rnd.nextInt(1000);
                items.add(new GapItem(new Line2D.Double(50, y1, 50, y1 + 10 + rnd.nextInt(20))));
            }
        }

        return items;
    }

    /** The pair-dependent comparator formerly used by StemBuilder.sortItems, copied verbatim. */
    private static Comparator<StemItem> formerComparator (int yDir)
    {
        return (se1,
                se2) ->
        {
            // Linker pairs are sorted on their refPt ordinate
            if (se1 instanceof HalfLinkerItem hl1) {
                if (se2 instanceof HalfLinkerItem hl2) {
                    final Point2D p1 = hl1.linker.getReferencePoint();
                    final Point2D p2 = hl2.linker.getReferencePoint();
                    return yDir * Double.compare(p1.getY(), p2.getY());
                }
            }

            // Others are sorted on their line starting ordinate
            return (yDir > 0) //
                    ? Double.compare(se1.line.getY1(), se2.line.getY1())
                    : Double.compare(se2.line.getY2(), se1.line.getY2());
        };
    }

    /** Build a small vertical stump glyph whose center line starts at the provided top. */
    private static Glyph makeStump (int left,
                                    int top,
                                    int height)
    {
        final RunTable table = new RunTable(Orientation.VERTICAL, 1, height);
        table.addRun(0, 0, height);

        return new Glyph(left, top, table);
    }

    //~ Inner classes ------------------------------------------------------------------------------

    /**
     * Minimal half linker: its reference point ordinate and its stump geometry are
     * chosen independently, exactly like a CLinker/VLinker whose stump center line
     * does not start at the reference point.
     */
    private static class FakeHalfLinker
            extends StemHalfLinker
    {
        private final Point2D refPt;

        private final Glyph stump;

        FakeHalfLinker (double refY,
                        Glyph stump)
        {
            this.refPt = new Point2D.Double(100, refY);
            this.stump = stump;
        }

        @Override
        public Collection<? extends StemHalfLinker> getHalfLinkers ()
        {
            return Collections.emptyList();
        }

        @Override
        public String getId ()
        {
            return "fake-" + refPt.getY();
        }

        @Override
        public Area getLookupArea ()
        {
            return null;
        }

        @Override
        public Point2D getReferencePoint ()
        {
            return refPt;
        }

        @Override
        public Inter getSource ()
        {
            return null;
        }

        @Override
        public Glyph getStump ()
        {
            return stump;
        }

        @Override
        public Line2D getTheoreticalLine ()
        {
            return null;
        }

        @Override
        public boolean isClosed ()
        {
            return false;
        }

        @Override
        public boolean isLinked ()
        {
            return false;
        }

        @Override
        public void setClosed (boolean closed)
        {
        }

        @Override
        public void setLinked (boolean linked)
        {
        }
    }
}
