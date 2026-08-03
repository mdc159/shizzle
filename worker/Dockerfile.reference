# Local Karaoke Server — GPU container
# Runs FastAPI backend with Demucs stem separation on CUDA
#
# Build: docker compose up --build
# Standalone: docker build -t karaoke-local-server . && docker run --gpus all -p 8001:8001 -v ./data:/app/data karaoke-local-server

FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1 \
    DEMUCS_DIRECT_MODE=true

# System deps: Python 3.11, ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3.11 \
    python3.11-venv \
    python3-pip \
    ffmpeg \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.11 1 \
    && update-alternatives --install /usr/bin/python python /usr/bin/python3.11 1

WORKDIR /app

# PyTorch + CUDA 12.1 (large layer, cached early)
RUN pip3 install --upgrade pip \
    && pip3 install --index-url https://download.pytorch.org/whl/cu121 \
    torch torchaudio

# Demucs and audio deps
RUN pip3 install \
    demucs \
    soundfile \
    numpy'<2'

# Install local-server package
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip3 install -e .

# Persistent data volume mount point
RUN mkdir -p /app/data

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python3 -c "import torch; assert torch.cuda.is_available(), 'No GPU'; import demucs; print('OK')"

EXPOSE 8001

CMD ["uvicorn", "local_server.main:app", "--host", "0.0.0.0", "--port", "8001"]
