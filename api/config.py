from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    audiveris_cmd: str = "audiveris"
    audiveris_args: str = "-batch -transcribe -export"
    input_dir: str = "/storage/in"
    output_dir: str = "/storage/out"
    max_error_len: int = 4000
    max_listed_files: int = 25
    min_interline: int = 11
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


    class Config:
        env_prefix = ""


settings = Settings()
