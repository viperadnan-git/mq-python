import logging
from typing import Dict, Type

from bson import CodecOptions
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.collection import Collection
from pymongo.database import Database

from mq.config import config
from mq.constants import (
    FIELD_EXPIRE_AT,
    FIELD_ID,
    FIELD_LOCKED_BY,
    FIELD_NEXT_RUN_AT,
    FIELD_PRIORITY,
    FIELD_STATUS,
    FIELD_STORE_NAME,
)
from mq.models.job import Job
from mq.models.worker import Worker

logger = logging.getLogger(__name__)

codec_options = CodecOptions(
    tz_aware=True,
)


class _MongoQueue:
    _db: Database
    _job_store: Collection
    _worker_store: Collection
    _job_class: Type[Job] = Job
    _worker_class: Type[Worker] = Worker
    _job_stores: Dict[str, "_MongoQueue"] = {}  # Store job store instances

    def __init__(
        self,
        database: Database,
        job_class: Type[Job] = Job,
        worker_class: Type[Worker] = Worker,
    ):
        self._db = database
        self._job_store = self._db.get_collection(
            config.mongo_jobs_collection, codec_options=codec_options
        )
        self._worker_store = self._db.get_collection(
            config.mongo_workers_collection, codec_options=codec_options
        )
        self._job_stores = {}  # Initialize job stores dict
        self._job_class = job_class._set_client(self._job_store)
        self._worker_class = worker_class._set_client(self._worker_store)
        # Ensure indexes are created
        self._ensure_indexes()

    def _ensure_indexes(self):
        """Create optimized indexes for better query performance"""
        # Index for store-specific job queries with compound index
        store_index_fields = [(FIELD_STORE_NAME, ASCENDING)]

        # Create indexes
        indexes = [
            # Index for job expiration
            IndexModel([(FIELD_EXPIRE_AT, 1)], expireAfterSeconds=0, name="job_expiry"),
            # Index for efficient job acquisition
            IndexModel(
                store_index_fields
                + [
                    (FIELD_STATUS, ASCENDING),
                    (FIELD_NEXT_RUN_AT, ASCENDING),
                    (FIELD_PRIORITY, DESCENDING),
                ],
                name=f"job_acquisition",
            ),
            # Index for listing jobs
            IndexModel(
                store_index_fields + [(FIELD_ID, DESCENDING)],
                name=f"job_listing",
            ),
            # # Index for job status queries
            IndexModel(
                store_index_fields + [(FIELD_STATUS, ASCENDING)],
                name=f"job_status",
            ),
            # Index for repair operations (locked_by)
            IndexModel([(FIELD_LOCKED_BY, ASCENDING)], name="job_locked_by"),
        ]

        # Create the indexes, ignoring if they already exist
        try:
            self._job_store.create_indexes(indexes)
        except Exception as e:
            logger.warning(f"Error creating indexes: {e}")
