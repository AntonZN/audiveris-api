# syntax=docker/dockerfile:1

FROM eclipse-temurin:25-jdk-jammy AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY --exclude=api . .

RUN ./gradlew :app:distTar --no-daemon

FROM eclipse-temurin:25-jdk-noble

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        ffmpeg \
        fluid-soundfont-gm \
        fluidsynth \
        libfreetype6 \
        libcairo2 \
        libgl1 \
        libglib2.0-0 \
        python3 \
        python3-venv \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY --from=builder /src/app/build/distributions/app-*.tar /tmp/audiveris.tar
RUN mkdir -p /opt \
    && tar -xf /tmp/audiveris.tar -C /opt \
    && mv /opt/app-* /opt/audiveris \
    && ln -s /opt/audiveris/bin/Audiveris /usr/local/bin/audiveris \
    && rm /tmp/audiveris.tar

# Audiveris initializes Tesseract in legacy mode, which the apt tesseract-ocr-eng
# package only ships as an LSTM-only model (init fails: "Could not initialize ...
# eng in legacy mode"). Ship the combined legacy+LSTM eng.traineddata instead.
COPY --from=builder /src/app/dev/tessdata/eng.traineddata /opt/tessdata/eng.traineddata
ENV TESSDATA_PREFIX=/opt/tessdata
ENV INPUT_DIR=/data/in
ENV OUTPUT_DIR=/data/out
ENV KEEP_ARTIFACTS=1
ENV PYTHONUNBUFFERED=1

ENV VIRTUAL_ENV=/opt/venv
RUN python3 -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

COPY api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Запекаем ONNX-модели homr (сегментация + трансформер + OCR заголовков) в образ,
# чтобы первый запрос не качал их в рантайме.
RUN homr --init

COPY api /srv/api

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
