"""remon — shared utilities for the real estate monitor pipeline.

Modules:
    config         load + validate config.yaml and .env (API keys)
    logging_setup  timestamped console logging
    http           cached, retry-with-backoff downloads
    validate       name-based column checks and dataframe validation
"""

__version__ = "0.1.0"
