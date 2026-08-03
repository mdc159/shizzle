# CUDA runtime base (good general default for current RunPod GPUs)
FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

# System deps: python, ffmpeg, basic build tools (some pip wheels still need them)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv \
    ffmpeg git ca-certificates curl \
    build-essential \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install PyTorch w/ CUDA (cu121)
# If you later choose a different CUDA base, change this index URL accordingly.
RUN pip3 install --upgrade pip \
 && pip3 install --index-url https://download.pytorch.org/whl/cu121 \
      torch torchaudio

COPY requirements.txt .
RUN pip3 install -r requirements.txt

# Copy all handler modules
COPY handler.py config.py s3_ops.py audio_processing.py manifest.py callbacks.py ./

CMD ["python3", "handler.py"]
