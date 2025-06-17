from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, Optional
from influxdb_client import Point

from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time


class cuUpMetricsParser:
    def __init__(self, main_exporter):
        # make an exporter
        self.exporter = main_exporter
        # Counters and tracking
        self.message_count = 0
        self.parse_error_count = 0
        self.pdcp_performance_history = defaultdict(lambda: defaultdict(list))  # Track PDCP performance by direction
        self.max_history_length = 50  # Keep last 50 readings for trend analysis

        # Expected field sets for validation
        self.EXPECTED_PDCP_DIRECTION_FIELDS = {
            'average_latency_us', 'min_latency_us', 'max_latency_us',
            'average_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_PDCP_FIELDS = {'dl', 'ul'}
        self.EXPECTED_CU_UP_FIELDS = {'pdcp'}
        self.EXPECTED_TOP_FIELDS = {'timestamp', 'cu-up'}

        # Performance thresholds for PDCP (can be configured)
        self.latency_warning_threshold_us = 1000.0  # microseconds
        self.latency_critical_threshold_us = 5000.0  # microseconds
        self.throughput_warning_threshold_mbps = 0.1  # Mbps (very low throughput warning)
        self.cpu_warning_threshold_percent = 10.0  # %
        self.cpu_critical_threshold_percent = 20.0  # %

    def calculate_pdcp_statistics(self, direction: str, metric_type: str, current_value: float):
        """Calculate statistics for PDCP performance trends."""
        try:
            history_key = f"{direction}_{metric_type}"

            # Add current value to history
            self.pdcp_performance_history[direction][metric_type].append(current_value)

            # Keep only the last N readings
            if len(self.pdcp_performance_history[direction][metric_type]) > self.max_history_length:
                self.pdcp_performance_history[direction][metric_type] = \
                    self.pdcp_performance_history[direction][metric_type][-self.max_history_length:]

            history = self.pdcp_performance_history[direction][metric_type]

            # Calculate statistics if we have enough data points
            if len(history) >= 2:
                avg_value = sum(history) / len(history)
                min_value = min(history)
                max_value = max(history)

                # Calculate moving average (last 10 readings)
                recent_readings = history[-10:] if len(history) >= 10 else history
                moving_avg = sum(recent_readings) / len(recent_readings)

                # Calculate variance and standard deviation
                variance = sum((x - avg_value) ** 2 for x in history) / len(history)
                std_dev = variance ** 0.5

                # Calculate trend (simple slope of last 5 readings)
                trend = 0.0
                if len(history) >= 5:
                    recent_5 = history[-5:]
                    x_vals = list(range(len(recent_5)))
                    n = len(recent_5)
                    sum_x = sum(x_vals)
                    sum_y = sum(recent_5)
                    sum_xy = sum(x * y for x, y in zip(x_vals, recent_5))
                    sum_x2 = sum(x * x for x in x_vals)

                    # Linear regression slope calculation
                    if n * sum_x2 - sum_x * sum_x != 0:
                        trend = (n * sum_xy - sum_x * sum_y) / (n * sum_x2 - sum_x * sum_x)

                return {
                    'average': avg_value,
                    'minimum': min_value,
                    'maximum': max_value,
                    'moving_average': moving_avg,
                    'standard_deviation': std_dev,
                    'trend': trend,
                    'sample_count': len(history)
                }

            return None

        except Exception as e:
            log_both(f"Error calculating PDCP statistics for {direction} {metric_type}: {e}", "error")
            return None

    def check_pdcp_performance_thresholds(self, direction: str, metrics: Dict[str, float],
                                          timestamp_dt: Optional[datetime] = None):
        """Check PDCP performance against thresholds and generate alerts."""
        influx_points = []

        try:
            alerts = []

            # Check latency thresholds
            avg_latency = metrics.get('average_latency_us')
            max_latency = metrics.get('max_latency_us')

            if avg_latency is not None:
                if avg_latency >= self.latency_critical_threshold_us:
                    alerts.append(
                        ('critical', f"{direction.upper()} PDCP average latency critical: {avg_latency:.1f}μs"))
                elif avg_latency >= self.latency_warning_threshold_us:
                    alerts.append(('warning', f"{direction.upper()} PDCP average latency high: {avg_latency:.1f}μs"))

            if max_latency is not None:
                if max_latency >= self.latency_critical_threshold_us:
                    alerts.append(('critical', f"{direction.upper()} PDCP max latency critical: {max_latency:.1f}μs"))
                elif max_latency >= self.latency_warning_threshold_us:
                    alerts.append(('warning', f"{direction.upper()} PDCP max latency high: {max_latency:.1f}μs"))

            # Check throughput thresholds (low throughput warning)
            throughput = metrics.get('average_throughput_Mbps')
            if throughput is not None and throughput > 0:
                if throughput < self.throughput_warning_threshold_mbps:
                    alerts.append(('warning', f"{direction.upper()} PDCP throughput low: {throughput:.6f}Mbps"))

            # Check CPU usage thresholds
            cpu_usage = metrics.get('cpu_usage_percent')
            if cpu_usage is not None:
                if cpu_usage >= self.cpu_critical_threshold_percent:
                    alerts.append(('critical', f"{direction.upper()} PDCP CPU usage critical: {cpu_usage:.2f}%"))
                elif cpu_usage >= self.cpu_warning_threshold_percent:
                    alerts.append(('warning', f"{direction.upper()} PDCP CPU usage high: {cpu_usage:.2f}%"))

            # Log and write alerts
            for alert_level, alert_message in alerts:
                log_level = "error" if alert_level == "critical" else "warning"
                log_both(alert_message, log_level)

                # Write alert to InfluxDB
                point = Point("cu_up_pdcp_alerts") \
                    .field("alert_level_numeric", 2 if alert_level == "critical" else 1) \
                    .tag("direction", direction) \
                    .tag("alert_level", alert_level) \
                    .tag("alert_message", alert_message) \
                    .tag("source", "srs_cu_up")

                if timestamp_dt:
                    point = point.time(timestamp_dt)

                influx_points.append(point)

            # Write normal status if no alerts
            if not alerts:
                point = Point("cu_up_pdcp_alerts") \
                    .field("alert_level_numeric", 0) \
                    .tag("direction", direction) \
                    .tag("alert_level", "normal") \
                    .tag("source", "srs_cu_up")

                if timestamp_dt:
                    point = point.time(timestamp_dt)

                influx_points.append(point)

            # Write alert metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error checking PDCP performance thresholds for {direction}: {e}", "error")

    def calculate_pdcp_derived_metrics(self, dl_metrics: Dict[str, float], ul_metrics: Dict[str, float],
                                       timestamp_dt: Optional[datetime] = None):
        """Calculate derived metrics from DL and UL PDCP data."""
        influx_points = []

        try:
            # Calculate total throughput
            dl_throughput = dl_metrics.get('average_throughput_Mbps', 0)
            ul_throughput = ul_metrics.get('average_throughput_Mbps', 0)
            total_throughput = dl_throughput + ul_throughput

            if total_throughput > 0:
                point = Point("cu_up_pdcp_derived") \
                    .field("total_throughput_Mbps", total_throughput) \
                    .tag("metric_type", "throughput") \
                    .tag("source", "srs_cu_up")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Calculate throughput asymmetry ratio (DL/UL)
            if ul_throughput > 0 and dl_throughput > 0:
                asymmetry_ratio = dl_throughput / ul_throughput
                point = Point("cu_up_pdcp_derived") \
                    .field("throughput_asymmetry_ratio", asymmetry_ratio) \
                    .tag("metric_type", "asymmetry") \
                    .tag("source", "srs_cu_up")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Calculate total CPU usage
            dl_cpu = dl_metrics.get('cpu_usage_percent', 0)
            ul_cpu = ul_metrics.get('cpu_usage_percent', 0)
            total_cpu = dl_cpu + ul_cpu

            point = Point("cu_up_pdcp_derived") \
                .field("total_cpu_usage_percent", total_cpu) \
                .tag("metric_type", "cpu") \
                .tag("source", "srs_cu_up")
            if timestamp_dt:
                point = point.time(timestamp_dt)
            influx_points.append(point)

            # Calculate latency difference (UL - DL)
            dl_latency = dl_metrics.get('average_latency_us')
            ul_latency = ul_metrics.get('average_latency_us')

            if dl_latency is not None and ul_latency is not None:
                latency_diff = ul_latency - dl_latency
                point = Point("cu_up_pdcp_derived") \
                    .field("latency_difference_us", latency_diff) \
                    .tag("metric_type", "latency") \
                    .tag("source", "srs_cu_up")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Calculate efficiency metrics (throughput per CPU usage)
            if dl_cpu > 0:
                dl_efficiency = dl_throughput / dl_cpu
                point = Point("cu_up_pdcp_derived") \
                    .field("dl_efficiency_mbps_per_cpu_percent", dl_efficiency) \
                    .tag("metric_type", "efficiency") \
                    .tag("direction", "dl") \
                    .tag("source", "srs_cu_up")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            if ul_cpu > 0:
                ul_efficiency = ul_throughput / ul_cpu
                point = Point("cu_up_pdcp_derived") \
                    .field("ul_efficiency_mbps_per_cpu_percent", ul_efficiency) \
                    .tag("metric_type", "efficiency") \
                    .tag("direction", "ul") \
                    .tag("source", "srs_cu_up")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Write derived metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error calculating PDCP derived metrics: {e}", "error")

    def update_pdcp_direction_metrics(self, direction_data: Dict[str, Any], direction: str,
                                      timestamp_dt: Optional[datetime] = None):
        """Update PDCP metrics for a specific direction (DL or UL)."""
        influx_points = []
        current_metrics = {}

        try:
            # Process all PDCP direction metrics
            for field in self.EXPECTED_PDCP_DIRECTION_FIELDS:
                value = safe_numeric(direction_data.get(field), field)
                if value is not None:
                    current_metrics[field] = value

                    # Write current value
                    point = Point("cu_up_pdcp_metrics") \
                        .field(field, value) \
                        .tag("direction", direction) \
                        .tag("source", "srs_cu_up")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

                    # Calculate and write statistics
                    stats = self.calculate_pdcp_statistics(direction, field, value)
                    if stats:
                        for stat_name, stat_value in stats.items():
                            if stat_value is not None:
                                stat_point = Point("cu_up_pdcp_statistics") \
                                    .field(f"{field}_{stat_name}", stat_value) \
                                    .tag("direction", direction) \
                                    .tag("metric_type", field) \
                                    .tag("statistic", stat_name) \
                                    .tag("source", "srs_cu_up")
                                if timestamp_dt:
                                    stat_point = stat_point.time(timestamp_dt)
                                influx_points.append(stat_point)

            # Check for unexpected fields
            unexpected_fields = set(direction_data.keys()) - self.EXPECTED_PDCP_DIRECTION_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected PDCP {direction} fields: {unexpected_fields}", "warning")

            # Write PDCP direction metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

            # Check performance thresholds
            # if_current_metrics:
            #self.check_pdcp_performance_thresholds(direction, current_metrics, timestamp_dt)

            # log_both(f"PDCP {direction.upper()} metrics updated - "
            #          f"Latency: {current_metrics.get('average_latency_us', 'N/A')}μs, "
            #          f"Throughput: {current_metrics.get('average_throughput_Mbps', 'N/A')}Mbps, "
            #          f"CPU: {current_metrics.get('cpu_usage_percent', 'N/A')}%")

            return current_metrics

        except Exception as e:
            log_both(f"Error updating PDCP {direction} metrics: {e}", "error")
            return {}

    def update_pdcp_metrics(self, pdcp_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update PDCP protocol metrics."""
        try:
            # Check for unexpected PDCP fields
            unexpected_fields = set(pdcp_data.keys()) - self.EXPECTED_PDCP_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected PDCP fields: {unexpected_fields}", "warning")

            dl_metrics = {}
            ul_metrics = {}

            # Process DL metrics
            dl_data = pdcp_data.get('dl', {})
            if dl_data:
                dl_metrics = self.update_pdcp_direction_metrics(dl_data, 'dl', timestamp_dt)
            else:
                log_both("PDCP missing DL data", "warning")

            # Process UL metrics
            ul_data = pdcp_data.get('ul', {})
            if ul_data:
                ul_metrics = self.update_pdcp_direction_metrics(ul_data, 'ul', timestamp_dt)
            else:
                log_both("PDCP missing UL data", "warning")

            # Calculate derived metrics if we have both DL and UL data
            if dl_metrics and ul_metrics:
                self.calculate_pdcp_derived_metrics(dl_metrics, ul_metrics, timestamp_dt)

        except Exception as e:
            log_both(f"Error updating PDCP metrics: {e}", "error")

    def update_cu_up_metrics(self, cu_up_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update CU-UP level metrics."""
        influx_points = []

        try:
            # Validate CU-UP structure
            unexpected_cu_up_fields = set(cu_up_data.keys()) - self.EXPECTED_CU_UP_FIELDS
            if unexpected_cu_up_fields:
                log_both(f"Unexpected CU-UP fields: {unexpected_cu_up_fields}", "warning")

            # Write CU-UP status metrics
            pdcp_data = cu_up_data.get('pdcp', {})
            has_dl = 'dl' in pdcp_data
            has_ul = 'ul' in pdcp_data

            point = Point("cu_up_metrics") \
                .field("pdcp_dl_active", 1 if has_dl else 0) \
                .field("pdcp_ul_active", 1 if has_ul else 0) \
                .field("pdcp_fully_active", 1 if (has_dl and has_ul) else 0) \
                .tag("source", "srs_cu_up")
            if timestamp_dt:
                point = point.time(timestamp_dt)
            influx_points.append(point)

            # Write CU-UP metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

            # Process PDCP data
            if pdcp_data:
                self.update_pdcp_metrics(pdcp_data, timestamp_dt)
            else:
                log_both("CU-UP missing PDCP data", "warning")

        except Exception as e:
            log_both(f"Error updating CU-UP metrics: {e}", "error")

    def update_metrics(self, entry: Dict[str, Any]):
        """Main metrics update function."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)

            # STEP 1: Update CU-UP metrics
            cu_up_data = entry.get("cu-up", {})
            if cu_up_data:
                self.update_cu_up_metrics(cu_up_data, timestamp_dt)
            else:
                log_both("Message missing CU-UP data", "warning")

            # STEP 2: Update system metrics
            self.message_count += 1
            system_points = []

            if timestamp:
                timestamp_val = safe_numeric(timestamp, "timestamp")
                if timestamp_val is not None:
                    system_points.append(
                        Point("cu_up_system_metrics").field("last_update_timestamp", timestamp_val).tag("source",
                                                                                                        "srs_cu_up")
                    )

            system_points.append(
                Point("cu_up_system_metrics").field("total_messages_received", self.message_count).tag("source",
                                                                                                       "srs_cu_up")
            )

            system_points.append(
                Point("cu_up_system_metrics").field("total_parse_errors", self.parse_error_count).tag("source",
                                                                                                      "srs_cu_up")
            )

            # Add PDCP monitoring health metrics
            dl_samples = len(self.pdcp_performance_history.get('dl', {}).get('average_latency_us', []))
            ul_samples = len(self.pdcp_performance_history.get('ul', {}).get('average_latency_us', []))

            system_points.append(
                Point("cu_up_system_metrics").field("pdcp_dl_history_samples", dl_samples).tag("source", "srs_cu_up")
            )

            system_points.append(
                Point("cu_up_system_metrics").field("pdcp_ul_history_samples", ul_samples).tag("source", "srs_cu_up")
            )

            # Add timestamp to system points
            if timestamp_dt:
                system_points = [p.time(timestamp_dt) for p in system_points]

            self.exporter.write_to_influx(system_points)

            # Check for unexpected top-level fields
            unexpected_fields = set(entry.keys()) - self.EXPECTED_TOP_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected top-level fields: {unexpected_fields}", "warning")

        except Exception as e:
            log_both(f"Error in update_metrics: {e}", "error")
            self.parse_error_count += 1
