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


class Worker(BaseMongoModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    func: Callable
    job: Job

    def run(self):
        self.func(self.job)

    def _run(self):
        with Heartbeat(self.id, self._client, scheduler):
            try:
                logger.info(f"Processing job {self.job.id}")
                self.run()
                self.job.complete()
                logger.info(f"Job {self.job.id} succeeded")
            except Exception as error:
                logger.error(f"Job {self.job.id} failed: {error}")
                self.job.failed(format_exc())
