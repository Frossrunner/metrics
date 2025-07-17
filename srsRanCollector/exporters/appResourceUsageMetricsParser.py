from collections import defaultdict
from typing import Dict, Any, Optional
from datetime import datetime
from influxdb_client import Point
from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time
from exporters.exporter import exporter


class appResourceUsageMetricsParser:
    def __init__(self, main_exporter: exporter):
        self.exporter = main_exporter
        self.message_count = 0
        self.parse_error_count = 0

        # Expected fields for the app resource usage
        self.EXPECTED_RESOURCE_FIELDS = {'cpu_usage_percent', 'memory_usage_MB', 'power_consumption_Watts'}
        self.EXPECTED_TOP_FIELDS = {'timestamp', 'app_resource_usage'}

    def update_app_resource_metrics(self, usage_metrics: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update app resource usage metrics to InfluxDB."""
        influx_points = []

        try:
            # Process each resource metric
            for field in self.EXPECTED_RESOURCE_FIELDS:
                value = safe_numeric(usage_metrics.get(field), field)
                if value is not None:
                    point = Point("app_resource_usage").field(field, value).tag("component", "app_monitor")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Write to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating app resource usage metrics: {e}", "error")

    def update_metrics(self, entry: Dict[str, Any]):
        """Main metrics update function."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)

            # Update app resource usage metrics
            app_resource_usage = entry.get("app_resource_usage", {})
            if app_resource_usage:
                self.update_app_resource_metrics(app_resource_usage, timestamp_dt)

            # Update system tracking metrics
            self.message_count += 1
            system_points = []

            if timestamp:
                timestamp_val = safe_numeric(timestamp, "timestamp")
                if timestamp_val is not None:
                    system_points.append(
                        Point("system_metrics").field("last_update_timestamp", timestamp_val).tag("component",
                                                                                                  "app_monitor")
                    )

            system_points.append(
                Point("system_metrics").field("total_messages_received", self.message_count).tag("component",
                                                                                                 "app_monitor")
            )

            system_points.append(
                Point("system_metrics").field("total_parse_errors", self.parse_error_count).tag("component", "app_monitor")
            )

            # Add timestamp to system points
            if timestamp_dt:
                system_points = [p.time(timestamp_dt) for p in system_points]

            self.exporter.write_to_influx(system_points)

            # Check for unexpected fields
            unexpected_fields = set(entry.keys()) - self.EXPECTED_TOP_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected top-level fields: {unexpected_fields}", "warning")
                self.parse_error_count += 1

        except Exception as e:
            log_both(f"Error in update_metrics: {e}", "error")
            self.parse_error_count += 1