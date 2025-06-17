from collections import defaultdict
from typing import Dict, Any, List, Optional
from datetime import datetime
from influxdb_client import InfluxDBClient, Point, WriteOptions
from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time
from exporters.exporter import exporter


class appResourceUsageMetricsParser:
    def __init__(self, main_exporter: exporter):
        # make an exporter
        self.exporter = main_exporter

        # Counters and tracking
        self.event_counter = defaultdict(int)
        self.message_count = 0
        self.parse_error_count = 0

        # Expected field sets for validation
        self.EXPECTED_RESOURCE_FIELDS = {'cpu_usage_percent', 'memory_usage_MB', 'power_consumption_Watts'}
        self.EXPECTED_TOP_FIELDS = {'timestamp', 'app_resource_usage'}

    def update_cell_metrics(self, usage_metrics: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update cell-level metrics to InfluxDB."""
        influx_points = []

        try:
            # Handle basic cell metrics
            for field in self.EXPECTED_RESOURCE_FIELDS:
                value = safe_numeric(usage_metrics.get(field), field)
                if value is not None:
                    point = Point("usage_metrics").field(field, value).tag("source", "srs_ran")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Write all cell metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating usage metrics: {e}", "error")

    def update_metrics(self, entry: Dict[str, Any]):
        """Main metrics update function."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)

            # STEP 2: Update cell metrics
            cell_metrics = entry.get("cell_metrics", {})
            if cell_metrics:
                self.update_cell_metrics(cell_metrics, timestamp_dt)

            # STEP 5: Update system metrics
            self.message_count += 1
            system_points = []

            if timestamp:
                timestamp_val = safe_numeric(timestamp, "timestamp")
                if timestamp_val is not None:
                    system_points.append(
                        Point("system_metrics").field("last_update_timestamp", timestamp_val).tag("source", "srs_ran")
                    )

            system_points.append(
                Point("system_metrics").field("total_messages_received", self.message_count).tag("source", "srs_ran")
            )

            system_points.append(
                Point("system_metrics").field("total_parse_errors", self.parse_error_count).tag("source", "srs_ran")
            )

            # Add timestamp to system points
            if timestamp_dt:
                system_points = [p.time(timestamp_dt) for p in system_points]

            self.exporter.write_to_influx(system_points)
            unexpected_fields = set(entry.keys()) - self.EXPECTED_TOP_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected top-level fields: {unexpected_fields}", "warning")
                self.parse_error_count += 1

        except Exception as e:
            log_both(f"Error in update_metrics: {e}", "error")
