FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    OPENBLAS_NUM_THREADS=2 \
    HF_HOME=/data/models/huggingface \
    DOCLING_ARTIFACTS_PATH=/data/models/docling

RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      tesseract-ocr \
      tesseract-ocr-eng \
      tesseract-ocr-vie \
      poppler-utils \
      libgl1 \
      libglib2.0-0 \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/app
COPY requirements.lock ./
RUN pip install --upgrade pip==25.2 \
    && pip install --index-url https://download.pytorch.org/whl/cpu \
         torch==2.7.1+cpu torchvision==0.22.1+cpu \
    && pip install -r requirements.lock

COPY pyproject.toml README.md ./
COPY app ./app
COPY tests ./tests
RUN pip install --no-deps .

RUN useradd --system --uid 10001 --create-home vault && mkdir -p /data && chown vault:vault /data
USER vault

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS http://127.0.0.1:5001/api/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5001", "--proxy-headers", "--forwarded-allow-ips", "*"]
