from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.ws import RunState

OUTPUT_ROOT = Path("outputs")
RUNS: dict[str, "RunState"] = {}
RUN_SEMAPHORE = asyncio.Semaphore(1)
