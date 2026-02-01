# Пресеты обработки Audiveris API

Документация по использованию пресетов для разных типов музыкальных нот.

## Содержание

- [default](#default)
- [drums](#drums)
- [drums_1line](#drums_1line)
- [jazz](#jazz)
- [guitar](#guitar)
- [bass](#bass)
- [vocal](#vocal)
- [piano](#piano)
- [small_notes](#small_notes)

---

## default

Стандартный пресет для классических нот и большинства партитур.

**Когда использовать:**
- Классическая музыка
- Поп/рок ноты (кроме барабанов)
- Любые ноты со скрипичным или басовым ключом
- Фортепиано, скрипка, флейта и др.

**Шрифт:** Bravura

---

## drums

Пресет для барабанной нотации на 5-линейном стане.

### Когда использовать

Используйте этот пресет **только** когда изображение содержит:

1. **Перкуссионный ключ** (две вертикальные черты вместо скрипичного/басового):
   ```
    ║║
    ║║
   ```

2. **Специальные головки нот:**
   - `x` — тарелки (hi-hat, crash, ride, cymbal)
   - `●` — барабаны (snare, tom, bass drum)
   - `◆` — специальные техники (rim shot, bell)
   - `▲` — cowbell и др.

3. **5-линейный стан** с нотами для ударной установки

### Пример правильной drum нотации

```
Перкуссионный ключ    Hi-hat (x)     Snare (●)    Bass drum (●)
       ║║              x   x          ●            ●
       ║║            ─────────────────────────────────
                       на линии -5    на линии -1   на линии 3-4
```

### Где взять тестовые изображения

#### Redeye Percussion (рекомендовано Audiveris)

Сотни бесплатных PDF с профессиональной drum нотацией:

| Песня | Исполнитель | Ссылка |
|-------|-------------|--------|
| Ophelia | The Band | [PDF](https://www.redeyepercussion.com/music/Ophelia_TheBand_redeyepercussion.com.pdf) |
| Back In Black | AC/DC | [PDF](https://www.redeyepercussion.com/music/BackInBlack_ACDC_redeyepercussion.com.pdf) |
| Enter Sandman | Metallica | [PDF](https://www.redeyepercussion.com/music/EnterSandman_Metallica_redeyepercussion.com.pdf) |
| Sweet Child of Mine | Guns N' Roses | [PDF](https://www.redeyepercussion.com/music/SweetChildOfMine_GunsNRoses_redeyepercussion.com.pdf) |
| Smells Like Teen Spirit | Nirvana | [PDF](https://www.redeyepercussion.com/music/SmellsLikeTeenSpirit_Nirvana_redeyepercussion.com.pdf) |
| Billie Jean | Michael Jackson | [PDF](https://www.redeyepercussion.com/music/BillieJean_MichaelJackson_redeyepercussion.com.pdf) |

Полный каталог: [redeyepercussion.com](https://www.redeyepercussion.com/)

#### Другие ресурсы

- [TheDrumNinja](https://thedrumninja.com/drum-transcriptions/) — 165+ бесплатных транскрипций
- [Stephen Mack](https://www.stephenmack.space/drumming) — 200+ транскрипций
- [Virtual Drumming](https://www.virtualdrumming.com/drums/drum-sheet-music.html) — уроки и ноты

### Важно

**НЕ используйте** пресет `drums` для:
- Обычных нот со скрипичным/басовым ключом
- Нот для мелодических инструментов
- Партитур где барабаны записаны обычными нотами

Если загрузить обычные ноты с пресетом `drums`, Audiveris выдаст ошибку:
```
NullPointerException: Cannot load from int array because "pitches" is null
```

### Пример использования API

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@drum_score.png" \
  -F "preset=drums" \
  http://localhost:8000/tasks/single
```

---

## drums_1line

Пресет для барабанной нотации на 1-линейном стане.

### Когда использовать

- Простая перкуссия на одной линии
- Партии отдельных инструментов (бонго, конга, тамбурин)
- Ритмические паттерны без указания высоты

### Отличие от drums

| Параметр | drums | drums_1line |
|----------|-------|-------------|
| Станы | 5-линейные | 1-линейные |
| fiveLineStaves | true | false |
| oneLineStaves | false | true |

---

## jazz

Пресет для джазовой музыки.

### Когда использовать

- Джазовые стандарты
- Lead sheets с аккордами
- Любые ноты где нужно распознавать названия аккордов (Am7, G7, Cmaj9)

### Что включено

- Шрифт **FinaleJazz** (другая форма нот, особенно для cross heads)
- Распознавание **chord names** (названий аккордов)

### Пример использования

```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -F "file=@jazz_lead_sheet.png" \
  -F "preset=jazz" \
  http://localhost:8000/tasks/single
```

---

## guitar

Пресет для гитарных нот.

### Когда использовать

- Ноты с гитарной табулатурой
- Классическая гитара с аппликатурой
- Любые ноты с указанием ладов и пальцев

### Что включено

- 6-линейные табулатуры (определяются и игнорируются)
- Аппликатура левой руки (0, 1, 2, 3, 4)
- Позиции ладов (I, II, IV...)
- Пальцы правой руки (p, i, m, a)
- Названия аккордов

### Важно о табулатуре

Audiveris **определяет** табулатуру и **игнорирует** её область. Сами цифры на табулатуре НЕ распознаются — только исключаются из обработки обычных нот.

---

## bass

Пресет для бас-гитары.

### Когда использовать

- Ноты с 4-линейной басовой табулатурой
- Басовые партии с аппликатурой

### Что включено

- 4-линейные табулатуры
- Аппликатура (fingerings)

---

## vocal

Пресет для вокальных партий.

### Когда использовать

- Вокальные партитуры
- Хоровые ноты
- Любые ноты с текстом песни (lyrics)

### Что включено

- Распознавание текста (lyrics)
- Текст над станом (не только под)

---

## piano

Пресет для фортепиано.

### Когда использовать

- Фортепианные ноты
- Любые 2-строчные партитуры

### Что включено

- Артикуляция (staccato, accent, etc.)

Audiveris автоматически определяет 2-staff parts как фортепиано.

---

## small_notes

Пресет для нот с маленькими (cue) нотами.

### Когда использовать

- Оркестровые партии с cue notes
- Ноты с маленькими вспомогательными нотами
- Партитуры с grace notes

### Что включено

- Маленькие головки нот (small heads)
- Маленькие вязки (small beams)

---

## Сводная таблица

| Пресет | Шрифт | Основное назначение |
|--------|-------|---------------------|
| default | Bravura | Классика, стандартные ноты |
| jazz | FinaleJazz | Джаз, lead sheets, аккорды |
| drums | JazzPerc | Барабаны (5 линий) |
| drums_1line | JazzPerc | Барабаны (1 линия) |
| guitar | Bravura | Гитара с табами |
| bass | Bravura | Бас-гитара |
| vocal | Bravura | Вокал с текстом |
| piano | Bravura | Фортепиано |
| small_notes | Bravura | Cue/маленькие ноты |

---

## Получение списка пресетов через API

```bash
curl http://localhost:8000/presets
```

Ответ содержит все пресеты с описаниями и CLI-константами Audiveris.
