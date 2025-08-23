import functools
import logging
import time

logger = logging.getLogger(__name__)


def query_timer(func):
    """Decorator to time database queries for performance monitoring"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        elapsed_time = time.time() - start_time
        if elapsed_time > 0.15:  # Only log slow queries (> 150ms)
            logger.debug(f"Slow query: {func.__name__} took {elapsed_time:.3f}s")
        return result

    return wrapper
