from datetime import datetime, timedelta, timezone
from typing import Self

from pydantic import BaseModel, Field

from mq.config import config


class Retry(BaseModel):
    attempt: int = Field(default=config.jobs_retry)
    delay: int = Field(default=config.jobs_retry_delay)
    backoff: int = Field(default=config.jobs_retry_backoff)

    def next_run_at(self) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=self.delay)

    def attempted(self):
        self.attempt -= 1
        # TODO: Implement backoff
        self.delay += self.backoff * self.delay

    def __bool__(self) -> bool:
        return self.attempt > 0

    @classmethod
    def get_default(cls) -> Self:
        if config.jobs_retry:
            return cls()
        return None
