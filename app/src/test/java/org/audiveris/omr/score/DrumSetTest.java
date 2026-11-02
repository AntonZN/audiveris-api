//------------------------------------------------------------------------------------------------//
//                                                                                                //
//                                     D r u m S e t T e s t                                      //
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

import org.audiveris.omr.glyph.ShapeSet.HeadMotif;
import org.audiveris.omr.score.DrumSet.DrumInstrument;
import org.audiveris.omr.score.DrumSet.DrumSound;
import org.audiveris.omr.score.DrumSet.MotifSign;

import static org.junit.Assert.*;
import org.junit.Test;

import java.util.Map;

/**
 * Unit tests for {@link DrumSet}.
 */
public class DrumSetTest
{
    /**
     * MusicXML 'midi-unpitched' counts from 1: GM key 36 (Bass_Drum_1) is written as 37.
     * Writing the GM key itself made a hi-hat sound as a floor tom.
     */
    @Test
    public void testMidiUnpitchedIsOneBased ()
    {
        assertEquals(37, DrumSound.Bass_Drum_1.getMidiUnpitched());
        assertEquals(43, DrumSound.Closed_Hi_Hat.getMidiUnpitched());

        for (DrumSound sound : DrumSound.values()) {
            assertEquals(sound.getMidi() + 1, sound.getMidiUnpitched());
        }
    }

    /**
     * Drum set as loaded from res/drum-set.xml.
     * Plain head on the single line is a snare, not Hand_Clap, and bass drum on D4 is found.
     */
    @Test
    public void testLoadedDrumSet ()
    {
        assertEquals(DrumSound.Acoustic_Snare, soundAt(1, 0, HeadMotif.oval));
        assertEquals(DrumSound.Side_Stick, soundAt(1, 0, HeadMotif.cross));
        assertEquals(DrumSound.Acoustic_Bass_Drum, soundAt(5, 5, HeadMotif.oval));
        assertEquals(DrumSound.Closed_Hi_Hat, soundAt(5, -5, HeadMotif.cross));
    }

    private static DrumSound soundAt (int lineCount,
                                      int pitch,
                                      HeadMotif motif)
    {
        final Map<MotifSign, DrumInstrument> map = DrumSet.getInstance().getStaffSet(lineCount)
                .get(pitch);

        if (map == null) {
            return null;
        }

        final DrumInstrument inst = map.get(new MotifSign(motif, null));

        return (inst != null) ? inst.sound : null;
    }
}
