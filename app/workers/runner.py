"""Run all BOOKABL background worker loops in one asyncio process."""

import asyncio
import signal
from contextlib import suppress

from app.bootstrap import build_runtime
from app.core.config import get_settings
from app.core.logging import configure_logging


async def run() -> None:
    """Build the runtime and supervise event, outbox, and scheduler loops."""

    settings = get_settings()
    configure_logging(settings.log_level)
    runtime = await build_runtime(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            with suppress(NotImplementedError):
                loop.add_signal_handler(getattr(signal, name), stop.set)
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(runtime.event_processor.run_forever(stop))
        tasks.create_task(runtime.outbox_worker.run_forever(stop))
        tasks.create_task(runtime.scheduler.run_forever(stop))


def main() -> None:
    """CLI entry point for ``python -m app.workers.runner``."""

    asyncio.run(run())


if __name__ == "__main__":
    main()
