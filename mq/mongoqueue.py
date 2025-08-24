import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Type

from bson import ObjectId
from pymongo import MongoClient, ReadPreference, WriteConcern
from pymongo.database import Database
from pymongo.read_concern import ReadConcern

from mq.base import codec_options
from mq.config import config
from mq.constants import FIELD_ID, FIELD_STATUS, FIELD_STORE_NAME
from mq.enum import JobStatus
from mq.jobstore import JobStore
from mq.lib.scheduler import scheduler
from mq.models.job import Job
from mq.models.worker import Worker
from mq.utils import query_timer

logger = logging.getLogger(__name__)


class MongoQueue(JobStore):
    """
    MongoQueue is a JobStore that maintains a registry of all job stores.
    """

    def __init__(
        self,
        database: Optional[Database | str] = None,
        database_name: Optional[str] = None,
        store_name: str = config.job_stores_default_name,
        auto_repair: bool = True,
        cache_ttl: int = 10,  # Cache TTL in seconds
        job_class: Type[Job] = Job,
        worker_class: Type[Worker] = Worker,
    ):
        # Initialize as a JobStore with default store_name
        mongo_database = None
        if isinstance(database, Database):
            mongo_database = database
        elif isinstance(database, str) or config.mongo_uri:
            mongo_client = MongoClient(database or config.mongo_uri)
            mongo_database = mongo_client.get_database(
                database_name or mongo_client._default_database_name or config.mongo_db,
                codec_options=codec_options,
            )
        else:
            raise ValueError(
                "MQ_MONGO_URI is not set, please set it in the environment variables or pass a database object or database uri in the constructor"
            )

        self._job_class = job_class
        self._worker_class = worker_class

        super().__init__(
            database=mongo_database,
            store_name=store_name,
            max_capacity=config.job_stores_default_capacity,
        )

        self._store_names_cache = None
        self._store_names_cache_time = 0
        self._cache_ttl = cache_ttl
        self._store_stats_cache = {}
        self._store_stats_cache_time = 0

        if auto_repair:
            logger.info("Starting auto-repair")
            self.start_auto_repair()

    def get_job_store(
        self,
        store_name: str,
        max_capacity: Optional[int] = None,
        job_class: Type[Job] = Job,
        worker_class: Type[Worker] = Worker,
    ) -> JobStore:
        """
        Get or create a job store with the specified name and capacity.
        Uses caching for already created stores.
        """
        if store_name not in self._job_stores:
            job_store = JobStore(
                database=self._db,
                store_name=store_name,
                max_capacity=max_capacity,
                job_class=job_class,
                worker_class=worker_class,
            )
            self._job_stores[store_name] = job_store
        return self._job_stores[store_name]

    def list_job_stores(self) -> List[str]:
        """
        List all job store names in use with caching for better performance.
        """
        current_time = time.time()
        # Return cached results if they're fresh
        if (
            self._store_names_cache is not None
            and current_time - self._store_names_cache_time < self._cache_ttl
        ):
            return (
                self._store_names_cache.copy()
            )  # Return a copy to prevent modification

        # Fetch fresh results - only get non-empty store names
        store_names = list(
            set(doc for doc in self._job_store.distinct(FIELD_STORE_NAME) if doc)
        )

        # Update cache
        self._store_names_cache = store_names
        self._store_names_cache_time = current_time

        return store_names

    def start_auto_repair(self):
        """Start the auto-repair process with scheduler."""
        self.repair()  # Run an initial repair
        scheduler.add_job(
            self.repair,
            "interval",
            seconds=60,
            id=f"repair-queue",
            max_instances=1,
            misfire_grace_time=60,
        )

    @query_timer
    def get_job(self, job_id):
        """
        Retrieve a job by ID with optimized query.
        """
        if isinstance(job_id, str):
            try:
                job_id = ObjectId(job_id)
            except Exception:
                pass

        # Use projection to only fetch the fields we need
        job = self._job_store.find_one({FIELD_ID: job_id})
        if job:
            return Job(**job)

        return None

    @query_timer
    def stats(self):
        """
        Get comprehensive stats with optimized queries and caching.
        """
        current_time = time.time()

        # Check if we have fresh cached stats
        if (
            self._store_stats_cache
            and current_time - self._store_stats_cache_time < self._cache_ttl
        ):
            return (
                self._store_stats_cache.copy()
            )  # Return a copy to prevent modification

        with self._db.client.start_session() as session:
            try:
                with session.start_transaction(
                    read_concern=ReadConcern("snapshot"),
                    write_concern=WriteConcern("majority"),
                    read_preference=ReadPreference.PRIMARY,
                ):
                    threshold_time = datetime.now(timezone.utc) - timedelta(seconds=120)

                    # Use a more efficient aggregation pipeline for job stats
                    job_stats_pipeline = [
                        {"$group": {"_id": "$status", "count": {"$sum": 1}}}
                    ]

                    # Execute the aggregation
                    job_stats_results = list(
                        self._job_store.aggregate(job_stats_pipeline, session=session)
                    )

                    # Process results into the stats dictionary
                    job_stats = {
                        "total": 0,
                        "pending": 0,
                        "processing": 0,
                        "failed": 0,
                        "completed": 0,
                    }

                    for result in job_stats_results:
                        status = result.get("_id")
                        count = result.get("count", 0)
                        job_stats["total"] += count

                        if status == JobStatus.PENDING.value:
                            job_stats["pending"] = count
                        elif status == JobStatus.PROCESSING.value:
                            job_stats["processing"] = count
                        elif status == JobStatus.FAILED.value:
                            job_stats["failed"] = count
                        elif status == JobStatus.COMPLETED.value:
                            job_stats["completed"] = count

                    # Worker Stats with optimized queries
                    total_workers = self._worker_store.count_documents(
                        {}, session=session
                    )
                    active_workers = self._worker_store.count_documents(
                        {"last_heartbeat": {"$gte": threshold_time}}, session=session
                    )
                    inactive_workers = total_workers - active_workers

                    # Get job store stats with an optimized aggregation
                    store_pipeline = [
                        {"$match": {FIELD_STORE_NAME: {"$ne": None, "$exists": True}}},
                        {"$group": {"_id": "$store_name", "count": {"$sum": 1}}},
                    ]

                    store_results = list(
                        self._job_store.aggregate(store_pipeline, session=session)
                    )
                    store_stats = {
                        result["_id"]: result["count"] for result in store_results
                    }

                    # Compile stats into a dictionary
                    stats = {
                        "jobs": job_stats,
                        "workers": {
                            "total": total_workers,
                            "active": active_workers,
                            "inactive": inactive_workers,
                        },
                        "stores": store_stats,
                    }

                    # Update cache
                    self._store_stats_cache = stats
                    self._store_stats_cache_time = current_time

                    return stats
            except Exception as e:
                logger.error(f"Error getting stats: {e}")
                # If transaction fails, try without transaction
                return self._fallback_stats()

    def _fallback_stats(self):
        """Fallback method to get stats without a transaction if the transaction fails."""
        try:
            # Simple query approach as fallback
            threshold_time = datetime.now(timezone.utc) - timedelta(seconds=120)

            total_jobs = self._job_store.count_documents({})
            pending_jobs = self._job_store.count_documents(
                {FIELD_STATUS: JobStatus.PENDING.value}
            )
            processing_jobs = self._job_store.count_documents(
                {FIELD_STATUS: JobStatus.PROCESSING.value}
            )
            failed_jobs = self._job_store.count_documents(
                {FIELD_STATUS: JobStatus.FAILED.value}
            )
            completed_jobs = self._job_store.count_documents(
                {FIELD_STATUS: JobStatus.COMPLETED.value}
            )

            # Worker Stats
            total_workers = self._worker_store.count_documents({})
            active_workers = self._worker_store.count_documents(
                {"last_heartbeat": {"$gte": threshold_time}}
            )

            # Get job store stats
            job_stores = self.list_job_stores()
            store_stats = {}
            for store_name in job_stores:
                store_count = self._job_store.count_documents(
                    {FIELD_STORE_NAME: store_name}
                )
                store_stats[store_name] = store_count

            # Compile stats
            stats = {
                "jobs": {
                    "total": total_jobs,
                    "pending": pending_jobs,
                    "processing": processing_jobs,
                    "failed": failed_jobs,
                    "completed": completed_jobs,
                },
                "workers": {
                    "total": total_workers,
                    "active": active_workers,
                    "inactive": total_workers - active_workers,
                },
                "stores": store_stats,
            }

            return stats
        except Exception as e:
            logger.error(f"Error in fallback stats: {e}")
            return {
                "jobs": {"total": 0, "error": str(e)},
                "workers": {"total": 0},
                "stores": {},
            }

    @query_timer
    def reset_job(self, job_id: str):
        """Reset a job to retry state with optimized update."""
        job = self.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        job.reset()

    @query_timer
    def run_job(self, job_id: str):
        """Set a job to run immediately with optimized update."""
        return self._job_class.update_one(
            job_id, status=JobStatus.PENDING, next_run_at=datetime.now(timezone.utc)
        )

    @query_timer
    def delete_job(self, job_id: str):
        """Delete a job with proper error handling."""
        try:
            if isinstance(job_id, str):
                job_id = ObjectId(job_id)

            result = self._job_store.delete_one({FIELD_ID: job_id})
            return result.deleted_count > 0
        except Exception as e:
            logger.error(f"Error deleting job {job_id}: {e}")
            return False

    @query_timer
    def get_job_by_payload_field(self, field: str, value: Any):
        """Find a job by a field in its payload with optimized query."""
        # First try to find in current store
        query = self._build_query({f"payload.{field}": value})
        response = self._job_store.find_one(query)

        if response:
            return Job(**response)

        # If not found, try all stores
        response = self._job_store.find_one({f"payload.{field}": value})
        if response:
            return Job(**response)

        return None

    def __repr__(self) -> str:
        return f"<MongoQueue id={self.id} store={self.store_name}>"
