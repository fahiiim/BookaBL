"""Lifecycle supervision for running BOOKABL workers inside the API process."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

logger = logging.getLogger(__name__)


class BackgroundWorker(Protocol):
    """A long-running worker that stops when its shared event is set."""

    async def run_forever(self, stop: asyncio.Event) -> None:
        """Process work until shutdown is requested."""


class WorkerRuntime(Protocol):
    """Runtime services supervised alongside the HTTP API."""

    @property
    def event_processor(self) -> BackgroundWorker:
        """Return the persisted-event processor."""

    @property
    def outbox_worker(self) -> BackgroundWorker:
        """Return the notification outbox worker."""

    @property
    def scheduler(self) -> BackgroundWorker:
        """Return the automation scheduler."""


@asynccontextmanager
async def supervise_workers(runtime: WorkerRuntime) -> AsyncIterator[None]:
    """Start all workers and stop them cleanly with the API lifespan."""

    stop = asyncio.Event()
    logger.info("embedded_workers_starting")
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(
            runtime.event_processor.run_forever(stop), name="bookabl-event-processor"
        )
        tasks.create_task(runtime.outbox_worker.run_forever(stop), name="bookabl-outbox")
        tasks.create_task(runtime.scheduler.run_forever(stop), name="bookabl-scheduler")
        try:
            yield
        finally:
            stop.set()
    logger.info("embedded_workers_stopped")
