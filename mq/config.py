from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    # MongoDB
    mongo_uri: Optional[str] = None
    mongo_db: str = "mq"
    mongo_jobs_collection: str = "jobs"
    mongo_workers_collection: str = "workers"

    # Jobs Retry
    jobs_retry: int = 3
    jobs_retry_delay: int = 5
    jobs_retry_backoff: int = 2

    # Jobs Result
    jobs_complete_result_ttl: int = 900
    jobs_failed_result_ttl: int = 0

    # Job Stores
    job_stores_default_name: str = "default"
    job_stores_default_capacity: Optional[int] = None

    # Workers
    workers_heartbeat_interval: int = 60

    # Dashboard
    dashboard_username: str = "admin"
    dashboard_password: str = "admin"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="MQ_", extra="ignore")


config = Config()
