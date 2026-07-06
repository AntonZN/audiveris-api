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
