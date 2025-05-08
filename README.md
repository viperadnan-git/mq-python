# MongoQueue with Job Stores

This implementation of MongoQueue provides job store functionality, allowing you to organize jobs into separate stores with customizable capacity limits.

## Features

- Create named job stores with specific capacity limits
- Isolate jobs in different stores for better organization and management
- Limit the number of jobs in specific stores to control resource usage
- All MongoDB queue operations available for each job store
- Store-specific job processing with dedicated workers
- Statistics for job stores and capacity management

## Architecture

- `JobStore` - Base class for managing jobs in a specific store
- `MongoQueue` - A special JobStore that manages jobs with no specific store (main queue) and maintains a registry of all job stores

## Usage

### Basic Usage

```python
from mq import MongoQueue

# Initialize the main MongoQueue
mq = MongoQueue()

# Create job stores with different capacities
users_store = mq.get_job_store("users", max_capacity=5)
posts_store = mq.get_job_store("posts", max_capacity=10)
analytics_store = mq.get_job_store("analytics")  # No capacity limit

# Add jobs to different stores
mq.put({"type": "system_maintenance"})  # Job in main queue (no store_name)
users_store.put({"user_id": "user_1", "action": "update_profile"})  # Job in users store
posts_store.put({"post_id": "post_1", "action": "index_content"})  # Job in posts store

# List jobs in the users store
users_jobs = users_store.list_jobs()
print("Users store jobs:", users_jobs)

# List jobs in the main queue
main_queue_jobs = mq.list_jobs()
print("Main queue jobs:", main_queue_jobs)

# Get capacity stats for users store
users_capacity = users_store.get_capacity_stats()
print("Users store capacity:", users_capacity)

# List available job stores
stores = mq.list_job_stores()
print("Available stores:", stores)
```

## Running Jobs

### Option 1: Simple Job Processing

The simplest way to process jobs from a specific job store:

```python
# Define a job processing function
def process_job(job):
    print(f"Processing job {job.id} with payload: {job.payload}")
    # Process the job...
    job.complete()
    return True

# Process jobs from a specific store
# This will run continuously, processing jobs as they become available
users_store.run_jobs(process_job, max_workers=2, poll_interval=1)
```

### Option 2: Process Jobs from All Stores

Process jobs from all job stores in parallel:

```python
# This will run jobs from all stores (including main queue) in parallel
# It creates worker pools for each store
mq.run_all_job_stores(process_job, max_workers_per_store=2, poll_interval=1)
```

### Option 3: Using ThreadPoolExecutor

For more control over job processing, you can use ThreadPoolExecutor:

```python
from concurrent.futures import ThreadPoolExecutor
import signal

def run_store_with_timeout(store, timeout=10):
    """Run jobs from a store with a timeout"""
    # Set up a timeout handler
    def handle_timeout(signum, frame):
        raise TimeoutError(f"Processing time limit reached")
    
    signal.signal(signal.SIGALRM, handle_timeout)
    signal.alarm(timeout)
    
    try:
        store.run_jobs(process_job, max_workers=2, poll_interval=0.5)
    except TimeoutError:
        print("Timeout reached")
    finally:
        signal.alarm(0)

# Process jobs using ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=4) as executor:
    # Get all stores
    stores = [mq] + [mq.get_job_store(name) for name in mq.list_job_stores()]
    
    # Submit jobs for each store with a timeout
    futures = {
        executor.submit(run_store_with_timeout, store, 5): store 
        for store in stores
    }
    
    # Wait for all to complete
    for future in futures:
        try:
            future.result()
        except Exception as e:
            print(f"Error: {str(e)}")
```

### Option 4: Independent Threads

Run job stores in separate threads:

```python
import threading

# Create a thread for each store
threads = []

# Add a thread for the main queue
main_thread = threading.Thread(
    target=lambda: mq.run_jobs(process_job, max_workers=2)
)
threads.append(main_thread)

# Add threads for each job store
for store_name in mq.list_job_stores():
    store = mq.get_job_store(store_name)
    thread = threading.Thread(
        target=lambda s=store: s.run_jobs(process_job, max_workers=2)
    )
    threads.append(thread)

# Start all threads
for thread in threads:
    thread.start()

# Wait for threads to complete (or use a timeout mechanism)
for thread in threads:
    thread.join()
```

### Recommended Production Setup

For production environments, we recommend:

1. Using a separate process for job processing
2. Implementing proper error handling and logging
3. Using a process manager like Supervisor to manage job processing services
4. Setting up monitoring and alerting for job processing

Example production setup:

```python
# job_processor.py
from mq import MongoQueue
import logging
import signal
import sys
import time

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize the main MongoQueue
mq = MongoQueue()

def process_job(job):
    try:
        logger.info(f"Processing job {job.id}")
        # Process the job...
        job.complete()
        return True
    except Exception as e:
        logger.error(f"Error processing job {job.id}: {str(e)}")
        job.failed(str(e))
        return False

def handle_shutdown(signum, frame):
    logger.info("Shutdown signal received, exiting gracefully...")
    sys.exit(0)

def main():
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    
    # Get the store name from command line arguments
    store_name = sys.argv[1] if len(sys.argv) > 1 else None
    
    logger.info(f"Starting job processor for store: {store_name or 'main'}")
    
    try:
        if store_name:
            # Process jobs from a specific store
            job_store = mq.get_job_store(store_name)
            job_store.run_jobs(process_job, max_workers=2)
        else:
            # Process jobs from the main queue
            mq.run_jobs(process_job, max_workers=2)
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
```

Supervisor configuration:

```ini
[program:mq_main]
command=python job_processor.py
autostart=true
autorestart=true
stderr_logfile=/var/log/mq/main.err.log
stdout_logfile=/var/log/mq/main.out.log

[program:mq_users]
command=python job_processor.py users
autostart=true
autorestart=true
stderr_logfile=/var/log/mq/users.err.log
stdout_logfile=/var/log/mq/users.out.log

[program:mq_posts]
command=python job_processor.py posts
autostart=true
autorestart=true
stderr_logfile=/var/log/mq/posts.err.log
stdout_logfile=/var/log/mq/posts.out.log
```

## Capacity Management

Job stores with capacity limits will raise a ValueError when you attempt to exceed the limit:

```python
# This will raise ValueError if users_store already has 5 jobs
try:
    users_store.put({"user_id": "new_user", "action": "verify_email"})
except ValueError as e:
    print(f"Capacity error: {e}")
```

## Statistics

Get information about job stores and their capacities:

```python
# Get capacity stats for a specific store
capacity_stats = users_store.get_capacity_stats()
print(capacity_stats)
# Example output:
# {
#     'store_name': 'users',
#     'current_jobs': 3,
#     'max_capacity': 5,
#     'is_full': False,
#     'available_capacity': 2
# }

# Get overall stats including job stores
stats = mq.stats()
print(stats)
# Example output:
# {
#     'jobs': {
#         'total': 15,
#         'pending': 10,
#         'processing': 2,
#         'failed': 1,
#         'completed': 2,
#         'main_queue': 3,
#     },
#     'workers': {
#         'total': 5,
#         'active': 5,
#         'inactive': 0,
#     },
#     'stores': {
#         'users': 5,
#         'posts': 7,
#         'analytics': 4,
#     }
# }
```

## Configuration

You can configure default job store settings in your environment variables:

```
MQ_JOB_STORES_DEFAULT_CAPACITY=100  # Default capacity for all stores
```

Or directly in your code:

```python
from mq.config import config

config.job_stores_default_capacity = 100  # Default capacity for all stores
config.job_stores_capacities = {
    "users": 5,
    "posts": 10,
    "analytics": None  # No limit
}
```
