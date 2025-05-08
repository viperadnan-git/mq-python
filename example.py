from dotenv import load_dotenv

load_dotenv()
import logging
import threading
import time
from datetime import datetime

from bson.objectid import ObjectId
from pymongo.database import Database

from mq import MongoQueue
from mq.config import config

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def clean_database(db: Database):
    """
    Clean up the MongoDB collections used by the job queue.
    This helps start from a clean state for demos and tests.
    """
    logger.info("Connecting to MongoDB...")
    # Get a list of collections
    collections = db.list_collection_names()
    logger.info(f"Collections in database: {collections}")

    # Drop collections used by MongoQueue
    if "jobs" in collections:
        logger.info("Dropping 'jobs' collection...")
        db.jobs.drop()
        db.jobs.drop_indexes()

    if "workers" in collections:
        logger.info("Dropping 'workers' collection...")
        db.workers.drop()

    logger.info("Database cleanup complete!")


def main():
    logger.info("Initializing MongoQueue with JobStore functionality demo")

    # Initialize the main MongoQueue with the default job store
    mq = MongoQueue()
    if input("\n\nClean database? (recommended for demo) (y/n): ").lower() in [
        "y",
        "yes",
    ]:
        clean_database(mq._db)

    # Display default store name from config
    logger.info(f"Default job store name: {config.job_stores_default_name}")

    # Create job stores with different capacities
    logger.info("Creating job stores with different capacities")
    users_store = mq.get_job_store("users", max_capacity=5)
    posts_store = mq.get_job_store("posts", max_capacity=10)
    analytics_store = mq.get_job_store("analytics")  # No capacity limit

    # Add jobs to different stores
    logger.info("Adding jobs to different stores")

    # Add job to main queue (uses default store name)
    mq.put({"type": "system_maintenance", "scheduled_at": datetime.now().isoformat()})

    # Add job to users store
    users_store.put({"user_id": "user_1", "action": "update_profile"})

    # Add jobs to posts store using bulk operation
    post_payloads = [
        {"post_id": f"post_{i}", "action": "index_content"} for i in range(1, 6)
    ]
    logger.info(f"Bulk adding {len(post_payloads)} jobs to posts store")
    post_job_ids = posts_store.bulk_put(post_payloads)
    logger.info(f"Added {len(post_job_ids)} jobs with IDs: {post_job_ids}")

    # Add jobs to analytics store with different priorities
    logger.info("Adding jobs with different priorities to analytics store")
    analytics_store.put({"report": "daily_users", "period": "yesterday"}, priority=10)
    analytics_store.put(
        {"report": "weekly_engagement", "period": "last_week"}, priority=5
    )
    analytics_store.put(
        {"report": "monthly_revenue", "period": "last_month"}, priority=1
    )

    # List jobs in the stores
    logger.info("\n--- Listing Jobs in Stores ---")

    users_jobs = users_store.list_jobs()
    logger.info(f"Users store jobs: {users_jobs['total']} total")

    posts_jobs = posts_store.list_jobs()
    logger.info(f"Posts store jobs: {posts_jobs['total']} total")

    main_queue_jobs = mq.list_jobs()
    logger.info(f"Default store jobs: {main_queue_jobs['total']} total")

    # Get capacity stats
    logger.info("\n--- Capacity Statistics ---")

    users_capacity = users_store.get_capacity_stats()
    logger.info(f"Users store capacity: {users_capacity}")

    posts_capacity = posts_store.get_capacity_stats()
    logger.info(f"Posts store capacity: {posts_capacity}")

    analytics_capacity = analytics_store.get_capacity_stats()
    logger.info(f"Analytics store capacity: {analytics_capacity}")

    main_capacity = mq.get_capacity_stats()
    logger.info(f"Default store capacity: {main_capacity}")

    # List available job stores
    stores = mq.list_job_stores()
    logger.info(f"Available stores: {stores}")

    # Test capacity limits
    logger.info("\n--- Testing Capacity Limits ---")
    try:
        logger.info("Attempting to exceed users store capacity...")
        for i in range(10):
            users_store.put(
                {"user_id": f"user_overflow_{i}", "action": "test_capacity"}
            )
    except ValueError as e:
        logger.info(f"Expected error: {e}")

    # Define a job processing function
    def process_job(job):
        logger.info(f"Processing job {job.id} with payload: {job.payload}")
        time.sleep(0.1)  # Simulate work
        job.complete()
        return True

    # Process jobs with priority
    logger.info("\n--- Processing Jobs by Priority ---")
    logger.info("Processing analytics jobs (should process in priority order):")

    # Process 3 jobs from analytics store (should be in priority order)
    for i in range(3):
        worker_id = f"analytics-worker-{i}"
        job = analytics_store.acquire_job(worker_id)
        if job:
            logger.info(f"Acquired job with priority {job.priority}: {job.payload}")
            process_job(job)

    # Demonstrate stalled job repair
    logger.info("\n--- Demonstrating Stalled Job Repair ---")

    # Create a "stalled" job manually
    stalled_job = posts_store.put(
        {"post_id": "stalled_post", "action": "simulate_stall"}
    )
    job_id = stalled_job.id

    # Manually mark it as processing but don't complete it
    logger.info(f"Marking job {job_id} as processing but not completing it")
    posts_store._job_store.update_one(
        {"_id": ObjectId(stalled_job.id)},
        {"$set": {"status": "PROCESSING", "locked_by": "crashed_worker"}},
    )

    # Repair all stores
    logger.info("Repairing all stores")
    mq.repair()
    # Verify the stalled job is now pending
    repaired_job = posts_store._job_store.find_one({"_id": ObjectId(stalled_job.id)})
    logger.info(f"Stalled job status after repair: {repaired_job['status']}")
    if repaired_job["status"] == "PENDING":
        logger.info("Stalled job repaired successfully")
    else:
        raise Exception("Stalled job not repaired")

    # Run demo of multi-threaded job processing (briefly)
    logger.info("\n--- Multi-threaded Job Processing Demo ---")

    # Add more jobs for the demo
    for i in range(5):
        mq.put({"type": f"main_job_{i}", "created_at": datetime.now().isoformat()})

    # Flag to stop threads
    stop_threads = threading.Event()

    # Function to run in a thread that processes jobs
    def run_worker(store, worker_name):
        store.run_jobs(process_job)

    # Start threads for each store
    threads = []

    main_thread = threading.Thread(target=run_worker, args=(mq, "default-worker"))
    threads.append(main_thread)

    posts_thread = threading.Thread(
        target=run_worker, args=(posts_store, "posts-worker")
    )
    threads.append(posts_thread)

    # Start the threads
    for thread in threads:
        thread.start()

    # Let them run for a short time
    time.sleep(2)

    # Stop the threads
    logger.info("Stopping worker threads")
    stop_threads.set()

    # Wait for threads to complete
    for thread in threads:
        thread.join()

    # Get overall stats
    logger.info("\n--- Overall Statistics ---")
    stats = mq.stats()
    logger.info(f"Overall stats: {stats}")

    # Demonstrate finding job by payload field
    logger.info("\n--- Finding Job by Payload Field ---")
    found_job = mq.get_job_by_payload_field("post_id", "post_1")
    if found_job:
        logger.info(f"Found job with post_id='post_1': {found_job.id}")

    logger.info("MongoQueue with JobStore demo completed!")


if __name__ == "__main__":
    main()
