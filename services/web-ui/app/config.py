from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration, read from MINUS20_* environment variables.

    Shares the Postgres DSN with analyzer-worker by convention: the UI reads the
    same incident history the worker writes.
    """

    postgres_dsn: str = "postgresql://minus20:minus20@localhost:5432/minus20"
    # How many incidents one page shows. The history is a growing dataset, so
    # the UI never selects it unbounded.
    page_size: int = 50
    log_level: str = "INFO"

    model_config = {"env_prefix": "MINUS20_"}


settings = Settings()
