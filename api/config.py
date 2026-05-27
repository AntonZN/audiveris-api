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


    class Config:
        env_prefix = ""


settings = Settings()
