import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from pydantic import Field
from pydantic_extra_types.mongo_object_id import MongoObjectId

from mq.config import config
from mq.enum import JobStatus
from mq.models.base import BaseMongoModel
from mq.models.retry import Retry

logger = logging.getLogger(__name__)


class Job(BaseMongoModel):
    id: MongoObjectId | str = Field(alias="_id", default_factory=lambda: ObjectId())
    payload: Dict[str, Any] | Any
    priority: int = 0
    retry: Optional[Retry] = None
    traceback: Optional[str] = None
    expire_at: Optional[datetime] = None
    locked_by: Optional[str] = None
    store_name: Optional[str] = None
    status: JobStatus = Field(default=JobStatus.PENDING)
    next_run_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def failed(self, traceback_info: str, release: bool = True) -> None:
        self.traceback = traceback_info

        if self.retry:
            self.retry.attempted()

            if self.retry.attempt <= 0:
                if config.jobs_failed_result_ttl > 0:
                    self.expire_in(config.jobs_failed_result_ttl, save=False)
                self.status = JobStatus.FAILED
            else:
                self.next_run_at = self.retry.next_run_at()
                self.status = JobStatus.PENDING
        else:
            self.status = JobStatus.FAILED

        if release:
            self.locked_by = None
        self.save()

    def complete(self, release=True, expire_in: Optional[timedelta] = None) -> None:
        self.status = JobStatus.COMPLETED
        if expire_in:
            self.expire_in(expire_in, save=False)
        elif config.jobs_complete_result_ttl > 0:
            self.expire_in(config.jobs_complete_result_ttl, save=False)
        else:
            self.delete()
            return

        if release:
            self.locked_by = None
        self.save()

    def reset(self) -> None:
        if self.status == JobStatus.PROCESSING or self.locked_by:
            raise Exception("Job is already being processed")

        self.status = JobStatus.PENDING
        self.next_run_at = datetime.now(timezone.utc)
        self.expire_at = None
        self.retry = Retry.get_default()
        self.locked_by = None
        self.save()

    def expire_in(self, seconds: int | timedelta, save: bool = True) -> None:
        if isinstance(seconds, timedelta):
            self.expire_at = datetime.now(timezone.utc) + seconds
        else:
            self.expire_at = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        if save:
            self.save()
