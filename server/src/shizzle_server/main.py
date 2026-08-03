"""FastAPI app for local karaoke stem splitter."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .processing import check_dependencies
from .routes import _tasks, router, set_data_dir

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    set_data_dir(DATA_DIR)

    missing = check_dependencies()
    if missing:
        logger.warning("Missing tools: %s — processing will fail", ", ".join(missing))
    else:
        logger.info("All dependencies available (ffmpeg, ffprobe, demucs)")

    logger.info("Data directory: %s", DATA_DIR)
    logger.info("Server ready at http://localhost:8001")
    yield
    # Shutdown: cancel and await pending pipeline tasks
    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)


app = FastAPI(title="Local Karaoke Stem Splitter", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
