from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Runtime configuration, read from SENTINELOPS_* environment variables.

    Shares the alert-stream settings with ingest-api by convention (same
    SENTINELOPS_ prefix, same defaults) so the two services agree on the queue.
    """

    # --- queue (must match ingest-api) ---
    redis_url: str = "redis://localhost:6379/0"
    alerts_stream: str = "sentinelops:alerts"
    dead_letter_stream: str = "sentinelops:alerts:dead"
    consumer_group: str = "analyzers"
    consumer_name: str = "analyzer-1"
    # XREADGROUP tuning: how many messages to pull and how long to block (ms).
    read_count: int = 10
    block_ms: int = 5_000
    # A message that keeps failing is parked in the dead-letter stream after this
    # many delivery attempts, so a poison message can never wedge the loop (ADR-0003).
    max_delivery_attempts: int = 5
    # A message delivered to a consumer that died before acknowledging is taken
    # over once it has been idle this long (XAUTOCLAIM). Longer than one full
    # analysis, so a slow-but-alive consumer is never robbed of its message.
    reclaim_idle_ms: int = 120_000

    # --- context collectors (comma-separated: stub, k8s-events, prometheus, loki) ---
    # Fixed in config, not chosen by the model at runtime (ADR-0001).
    collectors: str = "stub"
    k8s_max_events: int = 20
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    loki_max_lines: int = 50
    loki_window_minutes: int = 15

    # --- LLM backend (ADR-0002: selecting a backend is configuration, not code) ---
    llm_provider: str = "stub"  # "anthropic" | "ollama" | "openai" | "stub"
    # Fast path defaults to Haiku: analysing *every* alert then costs cents
    # (VISION §3, ADR-0001). Override per deployment.
    anthropic_model: str = "claude-haiku-4-5"
    anthropic_max_tokens: int = 512
    # Pricing in USD per 1M tokens, used only to record cost per incident.
    price_in_per_mtok: float = 1.00
    price_out_per_mtok: float = 5.00
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    # Any OpenAI-compatible endpoint. Defaults target DeepSeek, the cheapest
    # capable cloud option; point base_url at Groq, Together, OpenRouter, or a
    # local vLLM/LM Studio server instead. Key is optional for local servers.
    openai_base_url: str = "https://api.deepseek.com/v1"
    openai_model: str = "deepseek-chat"
    openai_api_key: str = ""
    # Output budget for the one structured call. Reasoning models (Gemini 3.x,
    # DeepSeek reasoner) spend part of it on thinking; 512 truncated Gemini's
    # JSON mid-object in the first live run.
    openai_max_tokens: int = 4096
    # USD per 1M tokens, used only to record cost per incident (DeepSeek list price).
    openai_price_in_per_mtok: float = 0.28
    openai_price_out_per_mtok: float = 0.42
    # Hard ceiling on a single analysis call — analysis must never hang the loop.
    llm_timeout_seconds: float = 30.0
    # Per-UTC-day spend cap; on exhaustion the pipeline degrades to raw delivery.
    daily_budget_usd: float = 5.00

    # --- deduplication ---
    # Alertmanager re-sends a firing alert every repeat_interval. Analysing the
    # same fingerprint again costs money and spams the engineer, so suppress
    # repeats inside this window (default 1h, matching a typical repeat_interval).
    dedup_window_seconds: int = 3600

    # --- persistence (ADR-0002: only post-redaction data is stored) ---
    store: str = "memory"  # "memory" | "postgres"
    postgres_dsn: str = "postgresql://sentinel:sentinel@localhost:5432/sentinelops"

    # --- delivery ---
    notifier: str = "stub"  # "telegram" | "slack" | "stub"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    # Slack Incoming Webhook URL. One pasted URL is all a customer needs:
    # no OAuth app, no bot token, no scopes to review.
    slack_webhook_url: str = ""

    # --- agent (ADR-0005: the deliberate, on-demand loop; separate process) ---
    # Every question is bounded three ways: tool calls, wall clock, daily spend.
    agent_max_tool_calls: int = 6
    agent_timeout_seconds: float = 600.0
    agent_daily_budget_usd: float = 2.00
    # Memory: local embeddings via Ollama, stored in pgvector (dimension must
    # match the model; nomic-embed-text is 768).
    embed_model: str = "nomic-embed-text"
    embed_dim: int = 768
    # Directory of .md/.txt runbooks to index into memory (mounted ConfigMap).
    runbooks_dir: str = ""
    # Screenshots sent to the agent are transcribed by a vision model first,
    # then handled as text. Ollama: qwen2.5vl, gemma3, llava. For the openai
    # provider the chat model is used unless a vision one is named.
    vision_model: str = "qwen2.5vl:7b"
    openai_vision_model: str = ""

    # --- MCP server (the same read-only tools, for any agent CLI) ---
    mcp_transport: str = "stdio"  # "stdio" (local CLI) | "http" (in-cluster Service)
    mcp_port: int = 8765

    log_level: str = "INFO"

    model_config = {"env_prefix": "SENTINELOPS_"}


settings = Settings()
