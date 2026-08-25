"""Tests for API-embedded worker supervision."""

import asyncio
from dataclasses import dataclass

from app.workers.supervisor import supervise_workers


class RecordingWorker:
    """Record startup and shutdown around the shared stop signal."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run_forever(self, stop: asyncio.Event) -> None:
        self.started.set()
        await stop.wait()
        self.stopped.set()


@dataclass
class RecordingRuntime:
    event_processor: RecordingWorker
    outbox_worker: RecordingWorker
    scheduler: RecordingWorker


async def test_supervise_workers_starts_and_stops_every_worker() -> None:
    workers = [RecordingWorker(), RecordingWorker(), RecordingWorker()]
    runtime = RecordingRuntime(*workers)

    async with supervise_workers(runtime):
        await asyncio.gather(*(worker.started.wait() for worker in workers))
        assert all(not worker.stopped.is_set() for worker in workers)

    assert all(worker.stopped.is_set() for worker in workers)
