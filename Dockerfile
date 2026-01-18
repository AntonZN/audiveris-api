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

FROM eclipse-temurin:25-jdk-jammy

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        fontconfig \
        libfreetype6 \
        python3 \
        python3-pip \
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

ENV TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
ENV INPUT_DIR=/data/in
ENV OUTPUT_DIR=/data/out
ENV KEEP_ARTIFACTS=1
ENV PYTHONUNBUFFERED=1

COPY api/requirements.txt /tmp/requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY api /srv/api

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
