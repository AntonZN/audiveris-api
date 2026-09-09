# syntax=docker/dockerfile:1

# Python строго 3.10: Zeus объявляет requires-python = ">=3.10,<3.11".
# Отдельный образ ровно поэтому — тащить TensorFlow 2.12 и вторую версию Python
# в окружение API незачем, там homr работает на чистом ONNX.
FROM python:3.10-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

# TensorFlow ставим ОТДЕЛЬНО и первым: слой тяжёлый, пусть кэшируется сам по себе.
# Пакет именно `tensorflow`, а НЕ `tensorflow-cpu`: у второго для 2.12 нет колеса
# под aarch64 (проверено — только x86_64 и win_amd64), и сборка на arm64-докере
# падает с «No matching distribution found». У обычного `tensorflow` 2.12 колёса
# есть и под manylinux aarch64, и под x86_64, то есть образ собирается и на
# Apple Silicon, и на обычном сервере.
RUN pip install --no-cache-dir "tensorflow~=2.12.0"

RUN pip install --no-cache-dir "zeus @ git+https://github.com/OmniOMR/zeus.git@v1.0.0"

# Зависимости пакета omr: он готовит страницу и режет её на станы.
RUN pip install --no-cache-dir \
        "opencv-python-headless>=4.8,<5.0" \
        "numpy>=1.23,<2.0" \
        "pillow>=10.0" \
        "pypdfium2>=4.30,<6.0"

# Снапшоты моделей. Лежат в соседних репозиториях авторов, не в релизах zeus.
# Кладём в образ, чтобы прогон не зависел от сети.
ARG ZEUS_MODEL_URL=https://github.com/Jirka-Mayer/ijdar/releases/download/model-snapshots/ayce-2026-08-03.model.tar.gz
ARG ZEUS_CAMERA_MODEL_URL=https://github.com/ufal/olimpic-icdar24/releases/download/grandstaff-models/zeus-camera-grandstaff-lmx-1.0-2024-02-12.model.tar.gz
RUN mkdir -p /models \
    && curl -fsSL "$ZEUS_MODEL_URL" | tar -xz -C /models \
    && curl -fsSL "$ZEUS_CAMERA_MODEL_URL" | tar -xz -C /models \
    && ls -la /models

COPY omr /srv/omr
COPY zeus_eval /srv/zeus_eval

ENV PYTHONPATH=/srv
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/srv/zeus_eval/transcribe.py"]
CMD ["--help"]
