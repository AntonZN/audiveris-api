// Раннер движка цзянпу: картинки -> MusicXML и строки заголовка, одним процессом.
//
//   node _jianpu_runner.mjs <пакет>/omr.js <каталог выхода> картинка...
//
// Зачем свой, а не omr-cli.mjs из пакета. Заголовок листа jpeditor распознаёт
// детектором строк целиком, но оставляет из него только название, «作词/作曲»,
// тональность, размер и метроном. Строку «中速 深情地» он выбрасывает, а набранную
// крупно — делает названием. Все строки он печатает только в отладке
// (`globalThis.__omrDebug`, метка `[header/det]`): раннер эту печать перехватывает
// и кладёт рядом с MusicXML — так же, как _homr_runner.py перехватывает OCR над
// станом ради темпа.
//
// Контракт тот же, что у omr-cli.mjs: про картинку, которую не разобрал, пишет в
// stderr «✗ имя: причина» и идёт дальше; код выхода 1, если не разобрал хоть одну.
import { readFile, writeFile } from "node:fs/promises";
import { basename, extname, join } from "node:path";
import { pathToFileURL } from "node:url";

const MIME = {
  ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
  ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
};
// Отладочная строка: `34px@629,33="梦里故乡"  26px@118,74="1=C"` — текст в JSON-кавычках.
const HEADER_LINE = /(\d+)px@(-?\d+),(-?\d+)=("(?:[^"\\]|\\.)*")/g;

const [omrJs, outDir, ...images] = process.argv.slice(2);
let header = null;
globalThis.__omrDebug = true;
// Остальная отладочная печать движка раннеру не нужна, а в stdout она только шум.
console.log = (...args) => {
  if (args[0] === "[header/det]") header = String(args[1] ?? "");
};
const engine = await import(pathToFileURL(omrJs).href);

let failed = 0;
for (const image of images) {
  const stem = basename(image, extname(image));
  header = null;
  try {
    const bytes = new Uint8Array(await readFile(image));
    const result = await engine.recognizeImage(bytes, { mime: MIME[extname(image).toLowerCase()], format: "jpwabc" });
    await writeFile(join(outDir, `${stem}.musicxml`), result.text, "utf-8");
    const lines = [...(header ?? "").matchAll(HEADER_LINE)]
      .map((m) => ({ text: JSON.parse(m[4]), height: Number(m[1]), x: Number(m[2]), y: Number(m[3]) }));
    await writeFile(join(outDir, `${stem}.header.json`), JSON.stringify(lines), "utf-8");
  } catch (error) {
    console.error(`✗ ${basename(image)}: ${error instanceof Error ? error.message : String(error)}`);
    failed++;
  }
}
process.exit(failed ? 1 : 0);
