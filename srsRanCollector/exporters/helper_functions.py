import logging
from datetime import datetime
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def log_both(message: str, level: str = "info"):
    """Helper function for dual logging"""
    getattr(logger, level)(message)


def timestamp_to_influx_time(timestamp_value: Any) -> Optional[datetime]:
    """Convert timestamp to InfluxDB-compatible datetime object."""
    if timestamp_value is None:
        return None

    try:
        # Assume timestamp is in seconds (Unix timestamp)
        timestamp_float = float(timestamp_value)
        return datetime.fromtimestamp(timestamp_float)
    except (ValueError, TypeError) as e:
        log_both(f"Error converting timestamp {timestamp_value} to datetime: {e}", "warning")
        return None


def safe_numeric(value: Any, field_name: str = "") -> Optional[float]:
    """Safely convert value to float, handling various edge cases."""
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("", "n/a", "null", "none", "nan"):
            return None
        try:
            return float(value)
        except ValueError:
            if field_name:
                log_both(f"Could not convert '{value}' to float for field '{field_name}'", "warning")
            return None

    if field_name:
        log_both(f"Unexpected type {type(value)} for field '{field_name}': {value}", "warning")
    return None
