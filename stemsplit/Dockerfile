# Shizzle GPU worker — RunPod serverless container
#
# Layer ordering follows worker/Dockerfile.reference (known-good k25 recipe)
# for cache efficiency: CUDA base -> system deps -> torch cu121 (largest,
# cached early) -> demucs/audio deps -> small handler deps -> code.
#
# Build:       docker build -t shizzle-worker worker/
# Local smoke: docker run --rm --gpus all \
#                -v <fixtures>:/fixture -v <out>:/out \
#                -v <torch-cache>:/root/.cache/torch \
#                shizzle-worker python3 handler.py --local /fixture/golden-30s.mp4 /out

FROM nvidia/cuda:12.1.1-cudnn8-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

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

# Handler deps (runpod, boto3, requests; demucs/audio already satisfied)
COPY requirements.txt .
RUN pip3 install -r requirements.txt

# Copy all handler modules
COPY handler.py config.py s3_ops.py audio_processing.py integrity.py manifest.py callbacks.py ./

# Note: htdemucs_6s weights are not baked; they download to /root/.cache/torch
# on first run (mount a cache volume locally to avoid re-downloads).

# No Docker HEALTHCHECK: RunPod serverless manages worker health via its own
# probe, and a container HEALTHCHECK fights it — on this ~15 GB image the torch
# +demucs import exceeds the probe window during warmup, so RunPod marks the
# worker "unhealthy" and cycles it before it can take a job. Verified 2026-08-03:
# the pipeline runs clean end-to-end on GPU (proofc538, both gates pass); the
# only failure was RunPod cycling workers on this healthcheck. Keep it removed.

CMD ["python3", "handler.py"]
