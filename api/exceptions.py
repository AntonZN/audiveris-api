from pathlib import Path


class ProcessingError(Exception):
    def __init__(self, message: str, log_path: Path | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.log_path = log_path


class LowInterlineError(ProcessingError):
    def __init__(self, interline: int, message: str, log_path: Path) -> None:
        super().__init__(message, log_path=log_path)
        self.interline = interline
