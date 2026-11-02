//------------------------------------------------------------------------------------------------//
//                                                                                                //
//                                      T i m e B u i l d e r                                     //
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

import org.audiveris.omr.classifier.Evaluation;
import org.audiveris.omr.classifier.ShapeClassifier;
import org.audiveris.omr.constant.Constant;
import org.audiveris.omr.constant.ConstantSet;
import org.audiveris.omr.glyph.Glyph;
import org.audiveris.omr.glyph.GlyphCluster;
import org.audiveris.omr.glyph.GlyphFactory;
import org.audiveris.omr.glyph.GlyphIndex;
import org.audiveris.omr.glyph.Glyphs;
import org.audiveris.omr.glyph.Grades;
import org.audiveris.omr.glyph.Shape;
import org.audiveris.omr.glyph.ShapeSet;
import org.audiveris.omr.run.Orientation;
import org.audiveris.omr.run.RunTable;
import org.audiveris.omr.run.RunTableFactory;
import org.audiveris.omr.score.TimeRational;
import org.audiveris.omr.sheet.Picture;
import org.audiveris.omr.sheet.Scale;
import org.audiveris.omr.sheet.Scale.InterlineScale;
import org.audiveris.omr.sheet.Sheet;
import org.audiveris.omr.sheet.Staff;
import org.audiveris.omr.sheet.SystemInfo;
import static org.audiveris.omr.sheet.time.TimeBuilder.TimeKind.NUM;
import org.audiveris.omr.sig.SIGraph;
import org.audiveris.omr.sig.inter.AbstractTimeInter;
import org.audiveris.omr.sig.inter.Inter;
import org.audiveris.omr.sig.inter.InterEnsemble;
import org.audiveris.omr.sig.inter.TimeNumberInter;
import org.audiveris.omr.sig.inter.TimePairInter;
import org.audiveris.omr.sig.relation.Exclusion;
import org.audiveris.omr.sig.relation.TimeTopBottomRelation;
import org.audiveris.omr.util.Navigable;
import org.audiveris.omr.util.VerticalSide;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import ij.process.Blitter;
import ij.process.ByteProcessor;

import java.awt.Rectangle;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.EnumMap;
import java.util.EnumSet;
import java.util.Iterator;
import java.util.List;
import java.util.ListIterator;
import java.util.Map;
import java.util.Set;

/**
 * Class <code>TimeBuilder</code> is the abstract basis for handling a time signature (such
 * as 4/4 or C) from a staff.
 * <p>
 * Subclass {@link HeaderTimeBuilder} is used at the beginning of a staff (in staff header) while
 * subclass {@link BasicTimeBuilder} is used farther down the staff.
 * <p>
 * <img src="doc-files/TimeBuilder.png" alt="TimeBuilder UML">
 *
 * @author Hervé Bitteur
 */
public abstract class TimeBuilder
{
    //~ Static fields/initializers -----------------------------------------------------------------

    private static final Constants constants = new Constants();

    private static final Logger logger = LoggerFactory.getLogger(TimeBuilder.class);

    /** Possible shapes for whole time signatures. */
    protected static final EnumSet<Shape> wholeShapes = ShapeSet.WholeTimes;

    /** Possible shapes for top or bottom half of time signatures. */
    protected static final EnumSet<Shape> halfShapes = EnumSet.copyOf(ShapeSet.PartialTimes);

    //~ Instance fields ----------------------------------------------------------------------------

    /** Dedicated staff to analyze. */
    protected final Staff staff;

    /** The column manager. */
    protected final TimeColumn column;

    /** The containing system. */
    @Navigable(false)
    protected final SystemInfo system;

    /** The related SIG. */
    protected final SIGraph sig;

    /** Related scale. */
    protected final Scale scale;

    /** Scale-dependent parameters. */
    protected final Parameters params;

    /** whole candidates. (common or cut or combo like 6/8 ...) */
    protected final List<Inter> wholes = new ArrayList<>();

    /** Top half candidates. (6/ ...) */
    protected final List<Inter> nums = new ArrayList<>();

    /** Bottom half candidates. (/8 ...) */
    protected final List<Inter> dens = new ArrayList<>();

    /** The time inter instance chosen for the staff. */
    protected AbstractTimeInter timeInter;

    //~ Constructors -------------------------------------------------------------------------------

    /**
     * Creates a new <code>TimeBuilder</code> object.
     *
     * @param staff  the underlying staff
     * @param column the column manager
     */
    public TimeBuilder (Staff staff,
                        TimeColumn column)
    {
        this.staff = staff;
        this.column = column;

        system = staff.getSystem();
        sig = system.getSig();
        scale = system.getSheet().getScale();

        params = new Parameters(scale, staff.getSpecificInterline());
    }

    //~ Methods ------------------------------------------------------------------------------------

    /**
     * This is called when we discover that a candidate is wrong,
     * so that all related data inserted in sig is removed.
     */
    public abstract void cleanup ();

    //---------------//
    // createTimeSig //
    //---------------//
    /**
     * Actually assign the time signature to the staff.
     *
     * @param bestTimeInter the time inter instance for this staff
     */
    protected void createTimeSig (AbstractTimeInter bestTimeInter)
    {
        timeInter = bestTimeInter;

        if (bestTimeInter != null) {
            bestTimeInter.setStaff(staff);

            final GlyphIndex index = system.getSheet().getGlyphIndex();

            if (bestTimeInter instanceof TimePairInter) {
                TimePairInter pair = (TimePairInter) bestTimeInter;

                for (Inter inter : pair.getMembers()) {
                    if (inter.getGlyph() != null) {
                        inter.setGlyph(index.registerOriginal(inter.getGlyph()));
                    }
                }
            } else {
                if (bestTimeInter.getGlyph() != null) {
                    bestTimeInter.setGlyph(index.registerOriginal(bestTimeInter.getGlyph()));
                }
            }
        }
    }

    /**
     * Discard all inters that do not pertain to precise chosen timeInter instance.
     */
    protected void discardOthers ()
    {
        // Chosen timeInter is either a TimePairInter (gathering two separate number inters) or
        // a single whole inter (TimeWholeInter or TimeCustomInter)
        if (timeInter instanceof TimePairInter) {
            final TimePairInter chosenPair = (TimePairInter) timeInter;

            // Discard all wholes
            for (Inter inter : wholes) {
                inter.remove();
            }

            // Check numbers
            for (VerticalSide side : VerticalSide.values()) {
                final List<Inter> members = (side == VerticalSide.TOP) ? nums : dens;
                final TimeNumberInter chosenNumber = chosenPair.getMember(side);

                for (Inter inter : members) {
                    if (inter != chosenNumber) {
                        // Remove the pair this number is part of, if any
                        for (Inter ensemble : inter.getAllEnsembles()) {
                            if (inter instanceof TimePairInter) {
                                ensemble.remove();
                            }
                        }

                        // Remove this member as well (if not yet done)
                        inter.remove();
                    }
                }
            }
        } else {
            // Check wholes
            for (Inter inter : wholes) {
                if (inter != timeInter) {
                    inter.remove();
                }
            }

            // Discard all numbers
            for (List<Inter> members : Arrays.asList(nums, dens)) {
                for (Inter inter : members) {
                    inter.remove();
                }
            }
        }
    }

    //------------------//
    // filterCandidates //
    //------------------//
    /**
     * Filter the candidates found for this staff.
     * <p>
     * Some basic tests are run at staff level to purge candidates
     * (every 'num' item must have a compatible 'den' item and vice versa).
     *
     * @return true if success, false otherwise (when no suitable candidate at all has been left)
     */
    protected boolean filterCandidates ()
    {
        // Establish all possible num/den pairs
        for (Inter topInter : nums) {
            TimeNumberInter top = (TimeNumberInter) topInter;
            final int topX = top.getCenter().x;

            for (Inter bottomInter : dens) {
                TimeNumberInter bottom = (TimeNumberInter) bottomInter;

                // Make sure top & bottom are abscissa-wise compatible
                int dx = bottom.getCenter().x - topX;

                if (Math.abs(dx) <= params.maxHalvesDx) {
                    // Restrict num/den pairs to supported combinations
                    TimeRational nd = new TimeRational(top.getValue(), bottom.getValue());

                    if (AbstractTimeInter.isSupported(nd)) {
                        // Halves support each other
                        sig.addEdge(top, bottom, new TimeTopBottomRelation());
                    } else {
                        sig.insertExclusion(top, bottom, Exclusion.ExclusionCause.INCOMPATIBLE);
                    }
                }
            }
        }

        // Make sure each half has a compatible partnering half
        for (List<Inter> list : Arrays.asList(nums, dens)) {
            for (Iterator<Inter> it = list.iterator(); it.hasNext();) {
                Inter inter = it.next();

                if (!sig.hasRelation(inter, TimeTopBottomRelation.class)) {
                    inter.remove();
                    it.remove();
                }
            }
        }

        // Compute contextual grades of nums & dens
        for (List<Inter> list : Arrays.asList(nums, dens)) {
            for (Inter inter : list) {
                sig.computeContextualGrade(inter);
            }
        }

        // Conflicts with existing inters?
        final List<Inter> all = new ArrayList<>();
        all.addAll(wholes);
        all.addAll(nums);
        all.addAll(dens);
        settleConflicts(all);
        wholes.retainAll(all);
        nums.retainAll(all);

        return !wholes.isEmpty() || !nums.isEmpty();
    }

    /**
     * Retrieve all acceptable candidates (whole or half) for this staff.
     * <p>
     * All candidates are stored as Inter instances in system sig, and in dedicated builder lists
     * (wholes, nums or dens).
     */
    protected abstract void findCandidates ();

    //----------//
    // getParts //
    //----------//
    /**
     * Retrieve all glyph instances that could be part of time sig.
     *
     * @param rect the area to pick glyphs from
     * @return time sig possible parts
     */
    protected List<Glyph> getParts (Rectangle rect)
    {
        final Sheet sheet = system.getSheet();

        // Grab pixels out of staff-free source
        ByteProcessor source = sheet.getPicture().getSource(Picture.SourceKey.NO_STAFF);
        ByteProcessor buf = new ByteProcessor(rect.width, rect.height);
        buf.copyBits(source, -rect.x, -rect.y, Blitter.COPY);

        // Extract parts
        RunTable runTable = new RunTableFactory(Orientation.VERTICAL).createTable(buf);
        List<Glyph> parts = GlyphFactory.buildGlyphs(runTable, rect.getLocation());

        // Keep only interesting parts
        purgeParts(parts, rect);

        final GlyphIndex glyphIndex = sheet.getGlyphIndex();

        for (ListIterator<Glyph> li = parts.listIterator(); li.hasNext();) {
            final Glyph part = li.next();
            Glyph glyph = glyphIndex.registerOriginal(part);
            li.set(glyph);
        }

        return parts;
    }

    //--------------//
    // getTimeInter //
    //--------------//
    /**
     * Report the time sig instance, if any, for the staff.
     *
     * @return the timeInter or null
     */
    public AbstractTimeInter getTimeInter ()
    {
        return timeInter;
    }

    //------------//
    // lookupHalf //
    //------------//
    /**
     * Lookup the provided area for half time-sig candidates.
     * Populates 'nums' or 'dens' and sig.
     *
     * @param half which half (top or bottom) is being searched for
     * @param rect the area to search, already restricted to this half
     * @return the adapter used, which keeps the candidates found
     */
    protected TimeAdapter lookupHalf (TimeKind half,
                                      Rectangle rect)
    {
        final List<Inter> inters = (half == NUM) ? nums : dens;

        //TODO: Should not try each and every combination of parts
        // Perhaps take all of them (or the largest ones?) and evaluate the compound
        List<Glyph> parts = getParts(rect);
        HalfAdapter adapter = new HalfAdapter(half, parts);
        new GlyphCluster(adapter, null).decompose();
        logger.debug(
                "Staff#{} {} {} trials:{}",
                staff.getId(),
                half,
                Glyphs.ids("parts", parts),
                adapter.trials);

        if (!adapter.bestMap.isEmpty()) {
            for (Map.Entry<Shape, Inter> entry : adapter.bestMap.entrySet()) {
                Inter inter = entry.getValue();
                Rectangle timeBox = inter.getSymbolBounds(staff.getSpecificInterline());
                inter.setBounds(timeBox);
                inter.setStaff(staff);
                sig.addVertex(inter);
                inters.add(inter);

                int gid = inter.getGlyph().getId();
                logger.debug("Staff#{} {} {} g#{} {}", staff.getId(), half, inter, gid, timeBox);
            }
        }

        return adapter;
    }

    //------------//
    // purgeParts //
    //------------//
    /**
     * Purge the population of parts candidates as much as possible, since the cost
     * of their later combinations is worse than exponential.
     *
     * @param parts the collection to purge
     * @param rect  the slice rectangle
     */
    private void purgeParts (List<Glyph> parts,
                             Rectangle rect)
    {
        //        // The rect is used for cropping only.
        //        // Use a smaller core rectangle which must be intersected by any part candidate
        //        Rectangle core = new Rectangle(rect);
        //        core.grow(-params.xCoreMargin, -params.yCoreMargin);
        //        staff.addAttachment("c", core);
        List<Glyph> toRemove = new ArrayList<>();

        for (Glyph part : parts) {
            ///if ((part.getWeight() < params.minPartWeight) || !part.getBounds().intersects(core)) {
            if (part.getWeight() < params.minPartWeight) {
                toRemove.add(part);
            }
        }

        if (!toRemove.isEmpty()) {
            parts.removeAll(toRemove);
        }
    }

    //-----------------//
    // settleConflicts //
    //-----------------//
    /**
     * Resolve conflicts with other inters.
     *
     * @param times time (whole or part) candidates
     */
    private void settleConflicts (List<Inter> times)
    {
        TimeLoop:
        for (Iterator<Inter> itt = times.iterator(); itt.hasNext();) {
            final Inter time = itt.next();
            final double timeGrade = time.getGrade();
            final Rectangle box = time.getBounds();
            final List<Inter> neighbors = sig.inters(
                    (inter) -> inter.getBounds().intersects(box)
                            && !(inter instanceof InterEnsemble));
            neighbors.removeAll(times);

            for (Iterator<Inter> itn = neighbors.iterator(); itn.hasNext();) {
                final Inter neighbor = itn.next();

                if (neighbor.overlaps(time)) {
                    if (neighbor.getGrade() <= timeGrade) {
                        if (neighbor.isVip()) {
                            logger.info("VIP Deleting {} overlapping {}", neighbor, time);
                        }
                        neighbor.remove();
                        itn.remove();
                    } else {
                        if (time.isVip()) {
                            logger.info("VIP Deleting {} overlapping {}", time, neighbor);
                        }
                        time.remove();
                        itt.remove();
                        continue TimeLoop;
                    }
                }
            }
        }
    }

    //----------//
    // toString //
    //----------//
    @Override
    public String toString ()
    {
        return getClass().getSimpleName() + "#" + staff.getId();
    }

    //~ Inner Classes ------------------------------------------------------------------------------

    //-----------//
    // Constants //
    //-----------//
    private static class Constants
            extends ConstantSet
    {
        private final Constant.Integer maxEvalRank = new Constant.Integer(
                "none",
                3,
                "Maximum acceptable rank in time evaluation");

        private final Scale.Fraction roiWidth = new Scale.Fraction(
                4.0,
                "Width of region of interest for time signature");

        private final Scale.Fraction yMargin = new Scale.Fraction(
                0.10,
                "Vertical white margin on raw rectangle");

        private final Scale.Fraction minTimeWidth = new Scale.Fraction(
                1.0,
                "Minimum width for a time signature");

        private final Scale.Fraction maxTimeWidth = new Scale.Fraction(
                2.0,
                "Maximum width for a time signature");

        private final Scale.Fraction maxHalvesDx = new Scale.Fraction(
                1,
                "Maximum abscissa shift between top & bottom halves of a time signature");

        private final Scale.AreaFraction minPartWeight = new Scale.AreaFraction(
                0.01,
                "Minimum weight for a glyph part");

        private final Scale.Fraction maxPartGap = new Scale.Fraction(
                1.0,
                "Maximum distance between two glyph parts of a single time symbol");

        private final Scale.AreaFraction minWholeTimeWeight = new Scale.AreaFraction(
                1.0,
                "Minimum weight for a whole time signature");

        private final Scale.AreaFraction minHalfTimeWeight = new Scale.AreaFraction(
                0.75,
                "Minimum weight for a half time signature");

        private final Scale.Fraction maxSpaceCumul = new Scale.Fraction(
                0.4,
                "Maximum cumul value in space");

        // Beware: A too small value might miss the whole time-sig
        private final Scale.Fraction maxFirstSpaceWidth = new Scale.Fraction(
                2.5,
                "Maximum initial space before time signature");

        // Beware: A too small value might miss some time-sig items
        private final Scale.Fraction maxInnerSpace = new Scale.Fraction(
                0.5,
                "Maximum inner space within time signature");
    }

    //-------------//
    // HalfAdapter //
    //-------------//
    /**
     * Handles the integration between glyph clustering class and time-sig environment.
     * <p>
     * For each time kind, we keep the best result found if any.
     */
    protected class HalfAdapter
            extends TimeAdapter
    {
        /** Which half is being searched. (NUM or DEN) */
        private final TimeKind half;

        public HalfAdapter (TimeKind half,
                            List<Glyph> parts)
        {
            super(parts);
            this.half = half;
        }

        @Override
        public void evaluateGlyph (Glyph glyph,
                                   Set<Glyph> parts)
        {
            trials++;

            if (glyph.getId() == 0) {
                glyph = system.registerGlyph(glyph, null);
            }

            Evaluation[] evals = ShapeClassifier.getInstance().evaluate(
                    glyph,
                    staff.getSpecificInterline(),
                    params.maxEvalRank,
                    Grades.timeMinGrade / Grades.intrinsicRatio,
                    null);

            for (Evaluation eval : evals) {
                final Shape shape = eval.shape;

                if (halfShapes.contains(shape)) {
                    final double grade = Grades.intrinsicRatio * eval.grade;
                    logger.debug("   {} eval {} for glyph#{}", half, eval, glyph.getId());

                    Inter bestInter = bestMap.get(shape);

                    if ((bestInter == null) || (bestInter.getGrade() < grade)) {
                        TimeNumberInter inter = TimeNumberInter.create(glyph, shape, grade, staff);

                        if (inter != null) {
                            bestMap.put(shape, inter);
                        }
                    }
                }
            }
        }

        @Override
        public boolean isTooLight (int weight)
        {
            return weight < params.minHalfTimeWeight;
        }
    }

    //------------//
    // Parameters //
    //------------//
    /**
     * Some parameters depend on global sheet scale but some depend on staff specific
     * scale when the sheet contains staves on different sizes (small and large).
     */
    protected static class Parameters
    {
        final int maxEvalRank;

        // Sheet scale dependent
        //----------------------
        //
        final int roiWidth;

        final int maxFirstSpaceWidth;

        final int maxInnerSpace;

        // Staff scale dependent
        //----------------------
        //
        final int yMargin;

        final int minTimeWidth;

        final int maxTimeWidth;

        final int maxHalvesDx;

        final int minPartWeight;

        final double maxPartGap;

        final int minWholeTimeWeight;

        final int minHalfTimeWeight;

        final int maxSpaceCumul;

        /**
         * @param scale
         * @param staffSpecific
         */
        Parameters (Scale scale,
                    int staffSpecific)
        {
            maxEvalRank = constants.maxEvalRank.getValue();

            {
                // Use sheet large interline scale
                final InterlineScale large = scale.getInterlineScale();
                roiWidth = large.toPixels(constants.roiWidth);
                maxFirstSpaceWidth = large.toPixels(constants.maxFirstSpaceWidth);
                maxInnerSpace = large.toPixels(constants.maxInnerSpace);
            }

            {
                // Use staff specific interline value
                final InterlineScale specific = scale.getInterlineScale(staffSpecific);
                yMargin = specific.toPixels(constants.yMargin);
                minTimeWidth = specific.toPixels(constants.minTimeWidth);
                maxTimeWidth = specific.toPixels(constants.maxTimeWidth);
                maxHalvesDx = specific.toPixels(constants.maxHalvesDx);
                minPartWeight = specific.toPixels(constants.minPartWeight);
                maxPartGap = specific.toPixels(constants.maxPartGap);
                minWholeTimeWeight = specific.toPixels(constants.minWholeTimeWeight);
                minHalfTimeWeight = specific.toPixels(constants.minHalfTimeWeight);
                maxSpaceCumul = specific.toPixels(constants.maxSpaceCumul);
            }
        }
    }

    //-------------//
    // TimeAdapter //
    //-------------//
    protected abstract class TimeAdapter
            extends GlyphCluster.AbstractAdapter
    {
        /** Best inter per time shape. */
        public Map<Shape, Inter> bestMap = new EnumMap<>(Shape.class);

        public TimeAdapter (List<Glyph> parts)
        {
            super(parts, params.maxPartGap);
        }

        public void cleanup ()
        {
            for (Inter inter : bestMap.values()) {
                inter.remove();
            }
        }

        public Inter getSingleInter ()
        {
            for (Inter inter : bestMap.values()) {
                if (!inter.isRemoved()) {
                    return inter;
                }
            }

            return null;
        }

        @Override
        public boolean isTooLarge (Rectangle bounds)
        {
            return bounds.width > params.maxTimeWidth;
        }
    }

    //~ Enumerations -------------------------------------------------------------------------------

    /**
     * The different parts of a time signature.
     */
    public static enum TimeKind
    {
        /** Whole signature (common or cut or combo). */
        WHOLE,
        /** Upper half (numerator number). */
        NUM,
        /** Lower half (denominator number). */
        DEN;
    }
}
