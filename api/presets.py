"""Audiveris processing presets for different instrument types."""

from enum import Enum


class Preset(str, Enum):
    """Available processing presets."""

    default = "default"
    jazz = "jazz"
    drums = "drums"
    drums_1line = "drums_1line"
    guitar = "guitar"
    bass = "bass"
    vocal = "vocal"
    piano = "piano"
    small_notes = "small_notes"


# Base constant prefix
_PREFIX = "org.audiveris.omr.sheet.ProcessingSwitches"
_FONT_PREFIX = "org.audiveris.omr.ui.symbol.MusicFont"


PRESET_CONSTANTS: dict[Preset, list[str]] = {
    Preset.default: [
        # Standard settings - Bravura font, lyrics and articulations enabled by default
    ],

    Preset.jazz: [
        f"{_FONT_PREFIX}.defaultMusicFamily=FinaleJazz",
        f"{_PREFIX}.chordNames=true",
    ],

    Preset.drums: [
        f"{_FONT_PREFIX}.defaultMusicFamily=JazzPerc",
        f"{_PREFIX}.drumNotation=true",
        f"{_PREFIX}.crossHeads=true",
    ],

    Preset.drums_1line: [
        f"{_FONT_PREFIX}.defaultMusicFamily=JazzPerc",
        f"{_PREFIX}.drumNotation=true",
        f"{_PREFIX}.oneLineStaves=true",
        f"{_PREFIX}.crossHeads=true",
        f"{_PREFIX}.fiveLineStaves=false",
    ],

    Preset.guitar: [
        # f"{_PREFIX}.sixStringTablatures=true",
        f"{_PREFIX}.fingerings=true",
        f"{_PREFIX}.frets=true",
        f"{_PREFIX}.pluckings=true",
        f"{_PREFIX}.chordNames=true",
    ],


    Preset.bass: [
        f"{_PREFIX}.fourStringTablatures=true",
        f"{_PREFIX}.fingerings=true",
    ],

    Preset.vocal: [
        f"{_PREFIX}.lyrics=true",
        f"{_PREFIX}.lyricsAboveStaff=true",
    ],

    Preset.piano: [
        # Piano is default - 2-staff parts auto-detected
        # Just ensure articulations are on
        f"{_PREFIX}.articulations=true",
    ],

    Preset.small_notes: [
        f"{_PREFIX}.smallHeads=true",
        f"{_PREFIX}.smallBeams=true",
    ],
}


def get_preset_args(preset: Preset) -> list[str]:
    """Get CLI arguments for a preset.

    Returns a list of ["-constant", "key=value", "-constant", "key=value", ...]
    """
    constants = PRESET_CONSTANTS.get(preset, [])
    args = []
    for const in constants:
        args.extend(["-constant", const])
    return args


def get_preset_description(preset: Preset) -> str:
    """Get human-readable description of a preset."""
    descriptions = {
        Preset.default: "Standard classical/sheet music (Bravura font)",
        Preset.jazz: "Jazz music with chord names (FinaleJazz font)",
        Preset.drums: "Drum notation on 5-line staves (JazzPerc font)",
        Preset.drums_1line: "Drum notation on 1-line staves (JazzPerc font)",
        Preset.guitar: "Guitar with tablature, fingerings, frets, chord names",
        Preset.bass: "Bass guitar with 4-line tablature",
        Preset.vocal: "Vocal/choir with lyrics above and below staff",
        Preset.piano: "Piano (2-staff parts, articulations)",
        Preset.small_notes: "Scores with cue/small notes and beams",
    }
    return descriptions.get(preset, "Unknown preset")
