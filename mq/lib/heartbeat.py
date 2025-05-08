import logging
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from pymongo.collection import Collection

logger = logging.getLogger(__name__)


class Heartbeat:
    def __init__(
        self,
        id: str,
        client: Collection,
        scheduler: BackgroundScheduler,
        interval: int = 60,
    ):
        self.id = id
        self.client = client
        self.scheduler = scheduler
        self.HEARTBEAT_INTERVAL = interval

    def start_heartbeat(self):
        logger.debug(f"Starting heartbeat for {self.id}")
        self.heartbeat()
        self.scheduler.add_job(
            self.heartbeat,
            "interval",
            seconds=self.HEARTBEAT_INTERVAL,
            id=self.heartbeat_id,
            max_instances=1,
            misfire_grace_time=self.HEARTBEAT_INTERVAL,
        )

    def stop_heartbeat(self):
        logger.debug(f"Stopping heartbeat for {self.id}")
        self.scheduler.remove_job(self.heartbeat_id)
        self.client.delete_one({"_id": self.id})

    def heartbeat(self):
        logger.debug(f"Updating heartbeat for {self.id}")
        self.client.update_one(
            {"_id": self.id},
            {"$set": {"last_heartbeat": datetime.now(tz=timezone.utc)}},
            upsert=True,
        )

    @property
    def heartbeat_id(self):
        return f"heartbeat-{self.id}"

    def __enter__(self):
        self.start_heartbeat()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop_heartbeat()
        return False
