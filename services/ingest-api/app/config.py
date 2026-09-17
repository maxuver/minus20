from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration, read from MINUS20_* environment variables."""

    redis_url: str = "redis://localhost:6379/0"
    alerts_stream: str = "minus20:alerts"
    # Cap stream length so a dead worker can't grow Redis unbounded (ADR-0003).
    alerts_stream_maxlen: int = 10_000
    log_level: str = "INFO"

    model_config = {"env_prefix": "MINUS20_"}


settings = Settings()
