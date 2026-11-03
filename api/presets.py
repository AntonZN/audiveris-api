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
    jianpu = "jianpu"


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
        # Leland, не JazzPerc: с JazzPerc Audiveris не узнаёт ни одной крестовой
        # головки (хай-хэт, тарелки) в нотах из MuseScore — F1 55-83% против 96-100%
        # на tests/images/drum.
        f"{_FONT_PREFIX}.defaultMusicFamily=Leland",
        f"{_PREFIX}.drumNotation=true",
        # Однолинейные станы рядом с пятилинейными (установка + перкуссия на одной
        # странице): без ключа Audiveris их не видит, и эти партии пропадают.
        # Little Drummer Boy из tests/images/drum/pdmx: 70 → 92%. Пятилинейные
        # остаются включены — выключает их только drums_1line.
        # Известная регрессия: если на листе есть TAB, Audiveris с этим ключом
        # падает (NegativeArraySizeException, mix-under-the-bridge) — такой файл
        # надо повторять без ключа (это делает сервис, `_drum_attempts`).
        f"{_PREFIX}.oneLineStaves=true",
        f"{_PREFIX}.crossHeads=true",
        # Форшлаги (флэмы, дроби): без малых головок Audiveris их не видит.
        # Drummer's Delight 82.5 → 97.2, Jingle Bells 68.4 → 91.5 на tests/images/drum/pdmx,
        # остальные не хуже. Ложные малые головки (стык цифр размера 4/4, паузы, слэши)
        # отсекает форк: NoteHeadsBuilder на драм-стане ищет их только от семени штиля и
        # с оценкой ≥ 0.65. Без этого Stinkfist терял размер 4/4 и падал 82 → 52.
        f"{_PREFIX}.smallHeads=true",
        # Малый барабан в старых изданиях пишут на пятилинейном стане с басовым (любым)
        # ключом — Audiveris читал его нотами с высотой (Count-Down: 0 ударных из 663).
        # Форк: лист, где нет ни одного ударного стана (однолинейного или с
        # перкуссионным ключом), читается по месту на стане (Staff.isDrumByContext).
        # Листы, где ударные соседствуют с басом или мелодией, не задеваются.
        "org.audiveris.omr.sheet.Staff.pitchedClefDrums=true",
    ],

    Preset.drums_1line: [
        # Leland, не JazzPerc: с JazzPerc Audiveris не узнаёт ни одной крестовой
        # головки (хай-хэт, тарелки) в нотах из MuseScore — F1 55-83% против 96-100%
        # на tests/images/drum.
        f"{_FONT_PREFIX}.defaultMusicFamily=Leland",
        f"{_PREFIX}.drumNotation=true",
        f"{_PREFIX}.oneLineStaves=true",
        f"{_PREFIX}.crossHeads=true",
        f"{_PREFIX}.smallHeads=true",
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

    Preset.jianpu: [
        # Audiveris не участвует: цифровую нотацию распознаёт jpeditor (api/jianpu.py).
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
        Preset.drums: "Drum notation on 5-line staves (Leland font)",
        Preset.drums_1line: "Drum notation on 1-line staves (Leland font)",
        Preset.guitar: "Guitar with tablature, fingerings, frets, chord names",
        Preset.bass: "Bass guitar with 4-line tablature",
        Preset.vocal: "Vocal/choir with lyrics above and below staff",
        Preset.piano: "Piano (2-staff parts, articulations)",
        Preset.small_notes: "Scores with cue/small notes and beams",
        Preset.jianpu: "Numbered notation 简谱 (jpeditor engine, not Audiveris)",
    }
    return descriptions.get(preset, "Unknown preset")
