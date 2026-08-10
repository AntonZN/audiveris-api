from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    debug: bool = False
    audiveris_cmd: str = "audiveris"
    audiveris_args: str = "-batch -transcribe -export"
    input_dir: str = "/storage/in"
    output_dir: str = "/storage/out"
    max_error_len: int = 4000
    max_listed_files: int = 25
    min_interline: int = 9
    task_workers: int = 1
    media_root: str = "/storage"
    media_base_url: str = "http://localhost:8081"
    media_path_prefix: str = ""
    redis_url: str = "redis://redis:6379/0"
    task_queue_key: str = "audiveris:queue"
    task_key_prefix: str = "audiveris:task:"
    requeue_running: bool = True
    api_token: str = '123'
    task_ttl_seconds: int = 86400
    # Файлы из /validate эфемерны (клиент забирает ссылку сразу), поэтому у них
    # свой, более короткий TTL, чем у выходов задач.
    validate_ttl_seconds: int = 3600
    cleanup_interval_seconds: int = 3600
    max_pdf_pages: int = 5
    processing_timeout_per_file_seconds: int = 60
    # Рендер PDF в Audiveris. Audiveris рендерит PDF на фиксированных 300 DPI и
    # ОТБРАСЫВАЕТ лист, если картинка > maxPixelCount (20 млн пикселей, хардкод
    # LoadStep.java) → «Too large image» → Created scores: [] → нет MusicXML.
    # Ловушка: телефонные «фото → PDF» (iOS Quartz PDFContext) кладут MediaBox в
    # пиксельных размерах фото (напр. 1448×2048 pt), и 300 DPI даёт ~51 Мп >> 20 Мп.
    # Поэтому для PDF мы считаем БЕЗОПАСНЫЙ DPI по размеру самой большой страницы,
    # чтобы уложиться в pdf_max_pixels, и передаём его Audiveris как -constant
    # org.audiveris.omr.image.ImageLoading.pdfResolution. Нормальные PDF (влезающие
    # на 300 DPI) рендерятся на полном pdf_render_dpi без потери качества.
    pdf_render_dpi: int = 300      # верхняя граница (дефолт Audiveris)
    pdf_max_pixels: int = 18_000_000  # бюджет пикселей на страницу (запас под 20М лимит)
    # Image preprocessing
    # Upscale only genuinely tiny scans. Normal sheet-music scans (~1000-1800px)
    # transcribe fine at native resolution; upscaling them blurs small tempo/metronome
    # digits and breaks OCR of the BPM (e.g. "95" -> "9s" -> tempo=9), so we keep a low
    # threshold to leave such images untouched.
    image_min_dimension: int = 1000  # Minimum width/height to skip upscale
    image_upscale_factor: float = 2.0  # Upscale multiplier (only for images below the threshold)
    image_contrast_factor: float = 1.2  # Contrast enhancement
    image_sharpness_factor: float = 1.5  # Sharpness enhancement

    # --- homr (фото-OMR) ---
    # Для single-задач любое одиночное ИЗОБРАЖЕНИЕ (JPEG/HEIC/PNG/WebP) идёт не
    # через Audiveris, а через трансформерный homr — он устойчивее к перекосу,
    # шуму и перспективе телефонных снимков. homr ставится как обычная зависимость
    # (см. requirements.txt / pyproject) и работает в том же окружении; вызывается
    # отдельным процессом (sys.executable -m homr.main), чтобы тяжёлый onnxruntime
    # не жил в памяти веб-воркера.
    homr_enabled: bool = True
    homr_timeout_seconds: int = 180
    # Гейт «пустого» результата: если music21 насчитал в выходе МЕНЬШЕ этого числа
    # нот, считаем распознавание провалом (homr/Audiveris на мусоре — скриншот,
    # фото без нот — «успешно» отдают пустую партитуру в 0-2 ноты). Такой результат
    # → ProcessingError → задача падает с ошибкой и попадает в архив провалов
    # (failed_files), а не отдаётся клиенту как completed с пустым .mxl. 0 отключает
    # проверку.
    min_recognized_notes: int = 3
    # homr слабо распознаёт темп. Если на фото BPM не нашёлся, дополнительно
    # прогоняем снимок через Audiveris (он надёжнее достаёт темп) и вписываем
    # найденное значение в результат — и в отдаваемый файл, и в API-поле bpm.
    audiveris_bpm_fallback: bool = True

    # --- Каталог нот / админка ---
    # Postgres. Внутри docker-compose host = "postgres".
    database_url: str = "postgresql+psycopg2://catalog:catalog@postgres:5432/catalog"
    # Корневая директория, куда складываются загруженные через админку медиа
    # (обложки, musicxml, midi, mp3, pdf). Лежит внутри media_root, чтобы её
    # раздавал тот же /media/* (Caddy -> /storage/out).
    catalog_media_dir: str = "/storage/out/catalog"

    # --- MP3-превью каталога ---
    # MusicXML -> MIDI (verovio) -> WAV (FluidSynth + GM SoundFont) -> MP3
    # (ffmpeg). Все этапы запускаются в отдельных процессах с общим таймаутом на
    # каждый этап; результат сохраняется в Score.audio_file.
    audio_preview_duration_seconds: float = 20.0
    audio_preview_bitrate_kbps: int = 128
    audio_preview_sample_rate: int = 44100
    audio_preview_fade_out_seconds: float = 1.5
    audio_preview_loudness_lufs: float = -16.0
    audio_preview_timeout_seconds: int = 120
    audio_preview_soundfont_path: str = "/usr/share/sounds/sf2/FluidR3_GM.sf2"
    audio_preview_fluidsynth_cmd: str = "fluidsynth"
    audio_preview_ffmpeg_cmd: str = "ffmpeg"

    # Директория «архива провалов»: входные файлы задач, которые не удалось
    # обработать, НЕ удаляются вместе с временным input_dir, а копируются сюда и
    # заводятся строкой в таблице failed_files (см. api/failures.py) — чтобы позже
    # провести аудит «с какими файлами мы работаем плохо» в админке. Лежит внутри
    # media_root (/storage/out), чтобы файлы отдавал тот же /media/* и в админке
    # было превью/скачивание. cleanup её НЕ трогает (см. api/cleanup.py).
    failures_dir: str = "/storage/out/failures"
    # Логин в админку SQLAdmin (одна учётка).
    admin_username: str = "admin"
    admin_password: str = "admin"
    # Секрет для подписи session-cookie админки. ОБЯЗАТЕЛЬНО переопределить в проде.
    admin_secret: str = "change-me-in-production"

    class Config:
        env_prefix = ""


settings = Settings()

# Префикс рабочих директорий /validate внутри output_dir. По нему cleanup отличает
# эфемерные validate-выходы (свой короткий TTL) от выходов задач.
VALIDATE_DIR_PREFIX = "validate-"
