import asyncio
import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional
from uuid import uuid4

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.database import Database
from pymongo.errors import BulkWriteError

from mq.base import _MongoQueue
from mq.config import config
from mq.constants import (
    FIELD_ID,
    FIELD_LOCKED_BY,
    FIELD_NEXT_RUN_AT,
    FIELD_PRIORITY,
    FIELD_STATUS,
    FIELD_STORE_NAME,
    FIELD_WORKER_INFO,
)
from mq.enum import JobStatus
from mq.models.async_worker import AsyncWorker
from mq.models.job import Job
from mq.models.retry import Retry
from mq.utils import query_timer

logger = logging.getLogger(__name__)


class JobStore(_MongoQueue):
    """
    A JobStore represents a collection of jobs with a specific name and capacity limit.
    """

    def __init__(
        self,
        database: Database,
        store_name: str = config.job_stores_default_name,
        max_capacity: Optional[int] = config.job_stores_default_capacity,
        *args,
        **kwargs,
    ):
        super().__init__(database=database, *args, **kwargs)
        self.store_name = store_name
        self.max_capacity = max_capacity

    def _build_query(self, additional_conditions: Dict = None) -> Dict:
        """
        Private method to build MongoDB queries consistently throughout the class.
        This centralizes query logic for store_name filtering.

        Args:
            additional_conditions: Additional query conditions to include

        Returns:
            Dict: MongoDB query dictionary
        """
        query = {FIELD_STORE_NAME: self.store_name}

        # Add any additional conditions
        if additional_conditions:
            for key, value in additional_conditions.items():
                query[key] = value

        return query

    @query_timer
    def put(self, payload: Any, job_id: str = None, priority: int = 0):
        """
        Add a job to the job store, respecting the maximum capacity if set.
        """
        if self.max_capacity is not None:
            # Check if adding this job would exceed capacity using a fast query with projection
            # Only request count(*) essentially, not the whole documents
            current_count = self._job_store.count_documents(
                self._build_query(), limit=self.max_capacity + 1
            )
            if current_count >= self.max_capacity:
                raise ValueError(
                    f"Job store '{self.store_name}' has reached its maximum capacity of {self.max_capacity} jobs"
                )

        return self._job_class.create(
            _id=job_id,
            payload=payload,
            retry=Retry.get_default(),
            priority=priority,
            store_name=self.store_name,
        )

    def bulk_put(self, payloads: List[Dict], priority: int = 0) -> List[str]:
        """
        Add multiple jobs to the job store in bulk for better performance.

        Args:
            payloads: List of job payloads
            priority: Priority for all jobs (optional)

        Returns:
            List of job IDs created
        """
        if not payloads:
            return []

        if self.max_capacity is not None:
            # Check if adding these jobs would exceed capacity
            current_count = self._job_store.count_documents(
                self._build_query(), limit=self.max_capacity + 1
            )
            if current_count + len(payloads) > self.max_capacity:
                raise ValueError(
                    f"Job store '{self.store_name}' has reached its maximum capacity of "
                    f"{self.max_capacity} jobs. Current: {current_count}, Attempting to add: {len(payloads)}"
                )

        # Prepare jobs for bulk insert
        now = datetime.now(timezone.utc)
        job_docs = []
        job_ids = []

        for payload in payloads:
            job_id = ObjectId()
            job_ids.append(str(job_id))

            job_docs.append(
                {
                    FIELD_ID: job_id,
                    "payload": payload,
                    "priority": priority,
                    "retry": Retry.get_default().dict(),
                    FIELD_STATUS: JobStatus.PENDING.value,
                    FIELD_STORE_NAME: self.store_name,
                    FIELD_NEXT_RUN_AT: now,
                }
            )

        # Perform bulk insert
        try:
            result = self._job_store.insert_many(job_docs, ordered=False)
            return [str(id) for id in result.inserted_ids]
        except BulkWriteError as e:
            # Some inserts might have succeeded, return the successful IDs
            logger.error(f"Bulk write error: {e}")
            return job_ids[: len(e.details.get("nInserted", 0))]

    @query_timer
    def acquire_job(self, worker_id: str) -> Optional[Job]:
        """
        Acquire a job from this specific job store.
        Uses FindAndModify to atomically find and update in one database trip.
        """
        query = self._build_query(
            {
                "$or": [
                    {FIELD_STATUS: JobStatus.PENDING.value},
                    {FIELD_STATUS: {"$exists": False}},
                    {FIELD_STATUS: ""},
                ],
                FIELD_NEXT_RUN_AT: {"$lte": datetime.now(timezone.utc)},
            }
        )

        # Use projection to only fetch the fields we need
        job = self._job_store.find_one_and_update(
            query,
            {"$set": {FIELD_STATUS: JobStatus.PROCESSING, FIELD_LOCKED_BY: worker_id}},
            sort=[
                (FIELD_PRIORITY, -1),
                (FIELD_NEXT_RUN_AT, 1),
            ],  # Add next_run_at as secondary sort
            return_document=ReturnDocument.AFTER,
        )
        if job:
            return Job(**job)

        return None

    @query_timer
    def list_jobs(
        self,
        offset: int = 0,
        limit: int = 20,
        status: JobStatus = None,
        additional_conditions: Dict = None,
        sort_field: str = "_id",
        sort_direction: int = -1,
    ):
        """
        List jobs in this specific job store with optimized query, projection, sorting and filtering.

        Args:
            offset: Number of records to skip for pagination
            limit: Maximum number of records to return
            status: Filter by job status
            additional_conditions: Additional query conditions
            sort_field: Field to sort by
            sort_direction: Sort direction (1 for ascending, -1 for descending)

        Returns:
            Dict with total count, jobs list, and pagination metadata
        """
        query_conditions = {}
        if status:
            query_conditions[FIELD_STATUS] = status.value

        # Add store name condition
        query_conditions[FIELD_STORE_NAME] = self.store_name

        # Add any additional conditions
        if additional_conditions:
            for key, value in additional_conditions.items():
                query_conditions[key] = value

        # Use projection to only fetch fields we need for listing
        projection = {
            "payload": 1,
            FIELD_STATUS: 1,
            FIELD_NEXT_RUN_AT: 1,
            "priority": 1,
            "created_at": 1,
            "retry": 1,
        }

        # First get the count with a fast query - no need to fetch actual documents
        total = self._job_store.count_documents(query_conditions)

        # Validate sort field and ensure we have a valid MongoDB field
        valid_sort_fields = ["_id", "status", "priority", "next_run_at", "created_at"]
        if sort_field not in valid_sort_fields:
            sort_field = "_id"  # Default to ID sort if invalid field

        # Validate sort direction
        if sort_direction not in [1, -1]:
            sort_direction = -1  # Default to descending if invalid

        # Then get the actual documents with pagination and sorting
        cursor = (
            self._job_store.find(query_conditions, projection=projection)
            .sort(sort_field, sort_direction)
            .skip(offset)
            .limit(limit)
        )

        jobs = list(cursor)

        return {
            "total": total,
            "jobs": jobs,
            "offset": offset,
            "limit": limit,
        }

    @query_timer
    def get_capacity_stats(self):
        """
        Get statistics about the job store's capacity using an optimized count query.
        """
        query = self._build_query()
        # Use a fast count with limit if we're just checking against max capacity
        if self.max_capacity:
            # If we only need to know if we're at/above capacity, limit the count
            limit = self.max_capacity + 1
            current_count = self._job_store.count_documents(query, limit=limit)
            is_full = current_count >= self.max_capacity
            # Only do an accurate count if we're not at capacity yet
            if not is_full and current_count < self.max_capacity:
                available = self.max_capacity - current_count
            else:
                available = 0
        else:
            # No max capacity, do a regular count
            current_count = self._job_store.count_documents(query)
            is_full = False
            available = None

        return {
            "store_name": self.store_name,
            "current_jobs": current_count,
            "max_capacity": self.max_capacity,
            "is_full": is_full,
            "available_capacity": available,
        }

    def run_jobs(
        self, job_func: Callable, max_workers: int = 1, poll_interval: int = 1
    ):
        """
        Run jobs only from this specific job store with improved worker management.
        """
        logger.info(
            f"Running jobs from {self.store_name} store with {max_workers} workers, polling every {poll_interval} seconds"
        )

        # Use a larger thread pool to handle potential I/O-bound job processing
        with ThreadPoolExecutor(max_workers=max(max_workers * 2, 4)) as executor:
            futures: Dict[Future, Job] = {}
            active_jobs = 0

            # Use exponential backoff for polling when no jobs are found
            base_poll_interval = poll_interval
            current_poll_interval = base_poll_interval
            max_poll_interval = 10

            while True:
                try:
                    # Check for completed futures and remove them
                    completed = []
                    for future in list(futures.keys()):
                        if future.done():
                            completed.append(future)
                            active_jobs -= 1

                    # Remove completed futures
                    for future in completed:
                        futures.pop(future)

                    # Try to acquire new jobs if we have space
                    jobs_to_acquire = max_workers - active_jobs

                    if jobs_to_acquire > 0:
                        acquired_jobs = 0

                        # Try to acquire jobs in batches for efficiency
                        for _ in range(
                            min(jobs_to_acquire, 10)
                        ):  # Batch up to 10 job acquisitions
                            worker_id = str(uuid4())
                            job = self.acquire_job(worker_id)

                            if job:
                                worker = self._worker_class(
                                    id=worker_id, job=job, func=job_func
                                )
                                future = executor.submit(worker._run)
                                futures[future] = job
                                active_jobs += 1
                                acquired_jobs += 1

                                logger.debug(
                                    f"Started worker {worker.id} to process job {job.id} from {self.store_name} store"
                                )
                            else:
                                # No more jobs available right now
                                break

                        if acquired_jobs > 0:
                            # Reset polling interval when jobs are found
                            current_poll_interval = base_poll_interval
                        else:
                            # Use exponential backoff when no jobs are found
                            current_poll_interval = min(
                                current_poll_interval * 1.5, max_poll_interval
                            )
                            time.sleep(current_poll_interval)
                    else:
                        # If all workers are busy, use a short sleep
                        time.sleep(0.1)  # Short interval to check for completed jobs

                except Exception as e:
                    logger.exception(
                        f"Error in job pool for {self.store_name} store: {str(e)}"
                    )
                    time.sleep(poll_interval)
                    continue
                except KeyboardInterrupt:
                    logger.info(
                        f"Keyboard interrupt received, stopping job pool for {self.store_name} store"
                    )
                    # Cancel all futures
                    for future in futures.keys():
                        future.cancel()
                    break

    @query_timer
    def repair(self):
        """
        Repair jobs with optimized queries.
        """
        threshold_time = datetime.now(timezone.utc) - timedelta(
            seconds=config.workers_heartbeat_interval * 2
        )

        # For simplicity, we still check all workers since they're not store-specific
        delete_result = self._worker_store.delete_many(
            {"last_heartbeat": {"$lt": threshold_time}}
        )
        if delete_result.deleted_count:
            logger.info(f"Removed {delete_result.deleted_count} stale workers")
        else:
            logger.debug("No stale workers found")

        # Find stale jobs with a more efficient query
        pipeline = [
            {
                "$lookup": {
                    "from": "workers",
                    "localField": FIELD_LOCKED_BY,
                    "foreignField": FIELD_ID,
                    "as": FIELD_WORKER_INFO,
                }
            },
            {
                "$match": {
                    FIELD_WORKER_INFO: {"$size": 0},
                    FIELD_LOCKED_BY: {"$ne": None},
                }
            },
            {"$project": {FIELD_ID: 1}},
        ]

        invalid_jobs = list(self._job_store.aggregate(pipeline))

        if invalid_jobs:
            job_ids_to_unlock = [job["_id"] for job in invalid_jobs]

            unlock_result = self._job_store.update_many(
                {"_id": {"$in": job_ids_to_unlock}},
                {"$set": {"status": JobStatus.PENDING.value, "locked_by": None}},
            )
            logger.info(
                f"Released {unlock_result.modified_count} jobs locked by non-existent workers"
            )
        else:
            logger.debug(
                f"No jobs found locked by non-existent workers in {self.store_name} store"
            )

    @query_timer
    def get_jobs_by_status(self, status: JobStatus, limit: int = 100):
        """
        Get jobs with a specific status from this job store with optimized query.
        """
        query = self._build_query({FIELD_STATUS: status.value})
        # Use projection to only fetch the fields we need
        projection = {
            "payload": 1,
            FIELD_STATUS: 1,
            FIELD_NEXT_RUN_AT: 1,
            "priority": 1,
        }

        jobs = list(
            self._job_store.find(query, projection=projection)
            .sort(FIELD_ID, -1)
            .limit(limit)
        )
        return [Job(**job) for job in jobs]

    @query_timer
    def count_jobs_by_status(self) -> Dict[str, int]:
        """
        Count jobs by status in this job store using an aggregation pipeline
        for better performance than multiple queries.
        """
        base_query = self._build_query()

        # Use aggregation framework for counting by status in one query
        pipeline = [
            {"$match": base_query},
            {"$group": {FIELD_ID: "$status", "count": {"$sum": 1}}},
        ]

        result = {
            "pending": 0,
            "processing": 0,
            "failed": 0,
            "completed": 0,
            "no_status": 0,
        }

        # Get counts from aggregation
        status_counts = list(self._job_store.aggregate(pipeline))

        # Convert to our result format
        for status_group in status_counts:
            status = status_group.get(FIELD_ID, "")
            count = status_group.get("count", 0)

            if not status:
                result["no_status"] = count
            else:
                result[status.lower()] = count

        return result

    async def run_jobs_async(
        self,
        func: Callable,
        max_workers: int = 1,
        poll_interval: int = 1,
        stop_event: Optional[asyncio.Event] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        """
        Run jobs asynchronously from this specific job store.
        Supports both async and sync job functions.

        Args:
            func: Async or sync function to process jobs
            max_workers: Maximum number of concurrent workers
            poll_interval: Time in seconds between polling for new jobs
            stop_event: Optional asyncio.Event to signal when to stop processing
            loop: Optional event loop to use (defaults to current running loop)
        """
        logger.info(
            f"Running async jobs from {self.store_name} store with {max_workers} workers, "
            f"polling every {poll_interval} seconds"
        )

        # Get the event loop if not provided
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                raise RuntimeError(
                    "No running event loop found. Either call this from within an async context "
                    "or provide a loop parameter."
                )

        # Track active tasks
        tasks = set()

        # Use exponential backoff for polling when no jobs are found
        base_poll_interval = poll_interval
        current_poll_interval = base_poll_interval
        max_poll_interval = 10

        try:
            while True:
                # Check if we should stop
                if stop_event and stop_event.is_set():
                    logger.info(f"Stop event triggered for {self.store_name} store")
                    break

                # Clean up completed tasks
                if tasks:
                    done, _ = await asyncio.wait(
                        tasks, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                    )

                    # Remove completed tasks
                    for task in done:
                        tasks.discard(task)
                        try:
                            # Get the result to see if there were any exceptions
                            await task
                        except Exception as e:
                            logger.error(f"Task failed with error: {e}")

                # Try to acquire new jobs if we have space
                jobs_to_acquire = max_workers - len(tasks)

                if jobs_to_acquire > 0:
                    acquired_jobs = 0

                    # Try to acquire jobs in batches for efficiency
                    for _ in range(min(jobs_to_acquire, 10)):
                        worker_id = str(uuid4())
                        job = self.acquire_job(worker_id)

                        if job:
                            # Create async worker
                            worker = AsyncWorker(id=worker_id, job=job, func=func)
                            worker._set_client(self._worker_store)

                            # Create and add task using the provided loop
                            task = loop.create_task(worker._run())
                            tasks.add(task)
                            acquired_jobs += 1

                            logger.debug(
                                f"Started async worker {worker.id} to process job {job.id} "
                                f"from {self.store_name} store"
                            )
                        else:
                            # No more jobs available right now
                            break

                    if acquired_jobs > 0:
                        # Reset polling interval when jobs are found
                        current_poll_interval = base_poll_interval
                    else:
                        # Use exponential backoff when no jobs are found
                        current_poll_interval = min(
                            current_poll_interval * 1.5, max_poll_interval
                        )
                        await asyncio.sleep(current_poll_interval)
                else:
                    # If all workers are busy, use a short sleep
                    await asyncio.sleep(0.1)

        except asyncio.CancelledError:
            logger.info(
                f"Async job pool for {self.store_name} store cancelled, "
                f"waiting for {len(tasks)} tasks to complete"
            )
            # Wait for all tasks to complete
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except KeyboardInterrupt:
            logger.info(
                f"Keyboard interrupt received, stopping async job pool for {self.store_name} store"
            )
            # Cancel all tasks
            for task in tasks:
                task.cancel()
            # Wait for all tasks to complete
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
        except Exception as e:
            logger.exception(
                f"Error in async job pool for {self.store_name} store: {str(e)}"
            )
            # Cancel all tasks on error
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            # Ensure all tasks are complete before returning
            if tasks:
                logger.info(f"Waiting for {len(tasks)} remaining tasks to complete")
                await asyncio.gather(*tasks, return_exceptions=True)

    def __repr__(self) -> str:
        return f"<JobStore name={self.store_name} max_capacity={self.max_capacity}>"

    def get_job(self, job_id: str | ObjectId) -> Optional[Job]:
        job = self._job_store.find_one({FIELD_ID: job_id})
        if job:
            return Job(**job)
        return None
