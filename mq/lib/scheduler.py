import logging
from datetime import UTC

from apscheduler.schedulers.background import BackgroundScheduler

logging.getLogger("apscheduler").setLevel(logging.ERROR)

scheduler = BackgroundScheduler(timezone=UTC)
scheduler.start()
