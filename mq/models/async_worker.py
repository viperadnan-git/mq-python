import asyncio
import logging
from traceback import format_exc
from typing import Callable
from uuid import uuid4

from pydantic import Field

from mq.lib.heartbeat import Heartbeat
from mq.lib.scheduler import scheduler
from mq.models.base import BaseMongoModel
from mq.models.job import Job

logger = logging.getLogger(__name__)


class AsyncWorker(BaseMongoModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    func: Callable
    job: Job

    async def run(self):
        """Run the async job function"""
        if asyncio.iscoroutinefunction(self.func):
            await self.func(self.job)
        else:
            # If the function is not async, run it in executor
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self.func, self.job)

    async def _run(self):
        """Run the async worker with heartbeat and error handling"""
        with Heartbeat(self.id, self._client, scheduler):
            try:
                logger.info(f"Processing async job {self.job.id}")
                await self.run()
                self.job.complete()
                logger.info(f"Async job {self.job.id} succeeded")
            except Exception as error:
                logger.error(f"Async job {self.job.id} failed: {error}")
                self.job.failed(format_exc())
