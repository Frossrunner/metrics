from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Optional
from influxdb_client import Point

from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time


class rlcMetricsParser:
    def __init__(self, main_exporter):
        # make an exporter
        self.exporter = main_exporter
        # Counters and tracking
        self.message_count = 0
        self.parse_error_count = 0
        self.active_drbs = set()  # Track active DRBs by composite key (du_id, ue_id, drb_id)
        self.rlc_performance_history = defaultdict(lambda: defaultdict(list))  # Track RLC performance by DRB
        self.max_history_length = 50  # Keep last 50 readings for trend analysis

        # Expected field sets for validation
        self.EXPECTED_DRB_FIELDS = {'du_id', 'ue_id', 'drb_id', 'tx', 'rx'}
        self.EXPECTED_TX_FIELDS = {
            'num_sdus', 'num_sdu_bytes', 'num_dropped_sdus', 'num_discarded_sdus',
            'num_discard_failures', 'num_pdus', 'num_pdu_bytes', 'sum_sdu_latency_us',
            'sum_pdu_latency_ns', 'max_pdu_latency_ns', 'pull_latency_histogram'
        }
        self.EXPECTED_RX_FIELDS = {
            'num_sdus', 'num_sdu_bytes', 'num_pdus', 'num_pdu_bytes',
            'num_lost_pdus', 'num_malformed_pdus'
        }
        self.EXPECTED_PULL_LATENCY_BIN_FIELDS = {'pull_latency_bin_start_usec', 'pull_latency_bin_count'}
        self.EXPECTED_RLC_ENTRY_FIELDS = {'drb'}
        self.EXPECTED_TOP_FIELDS = {'timestamp', 'rlc_metrics'}

        # Performance thresholds for RLC (can be configured)
        self.sdu_drop_rate_warning_percent = 1.0  # 1% SDU drop rate warning
        self.sdu_drop_rate_critical_percent = 5.0  # 5% SDU drop rate critical
        self.pdu_loss_rate_warning_percent = 0.5  # 0.5% PDU loss rate warning
        self.pdu_loss_rate_critical_percent = 2.0  # 2% PDU loss rate critical
        self.avg_sdu_latency_warning_us = 5000.0  # 5ms average SDU latency warning
        self.avg_sdu_latency_critical_us = 10000.0  # 10ms average SDU latency critical
        self.max_pdu_latency_warning_ns = 10000000.0  # 10ms max PDU latency warning (10M ns)
        self.max_pdu_latency_critical_ns = 20000000.0  # 20ms max PDU latency critical (20M ns)

    def generate_drb_key(self, du_id: int, ue_id: int, drb_id: int) -> str:
        """Generate a composite key for DRB identification."""
        return f"du{du_id}_ue{ue_id}_drb{drb_id}"

    def calculate_rlc_statistics(self, drb_key: str, metric_type: str, current_value: float):
        """Calculate statistics for RLC performance trends."""
        try:
            # Add current value to history
            self.rlc_performance_history[drb_key][metric_type].append(current_value)

            # Keep only the last N readings
            if len(self.rlc_performance_history[drb_key][metric_type]) > self.max_history_length:
                self.rlc_performance_history[drb_key][metric_type] = \
                    self.rlc_performance_history[drb_key][metric_type][-self.max_history_length:]

            history = self.rlc_performance_history[drb_key][metric_type]

            # Calculate statistics if we have enough data points
            if len(history) >= 2:
                avg_value = sum(history) / len(history)
                min_value = min(history)
                max_value = max(history)

                # Calculate moving average (last 5 readings)
                recent_readings = history[-5:] if len(history) >= 5 else history
                moving_avg = sum(recent_readings) / len(recent_readings)

                # Calculate variance and standard deviation
                variance = sum((x - avg_value) ** 2 for x in history) / len(history)
                std_dev = variance ** 0.5

                return {
                    'average': avg_value,
                    'minimum': min_value,
                    'maximum': max_value,
                    'moving_average': moving_avg,
                    'standard_deviation': std_dev,
                    'sample_count': len(history)
                }

            return None

        except Exception as e:
            log_both(f"Error calculating RLC statistics for {drb_key} {metric_type}: {e}", "error")
            return None

    def update_pull_latency_histogram(self, histogram_data: List[Dict[str, Any]], drb_key: str,
                                      timestamp_dt: Optional[datetime] = None):
        """Update pull latency histogram metrics."""
        influx_points = []

        try:
            total_pulls = 0
            weighted_latency_sum = 0
            max_bin_count = 0
            max_bin_start = 0

            for bin_entry in histogram_data:
                bin_data = bin_entry.get('pull_latency_bin', {})

                # Validate bin structure
                unexpected_bin_fields = set(bin_data.keys()) - self.EXPECTED_PULL_LATENCY_BIN_FIELDS
                if unexpected_bin_fields:
                    log_both(f"Unexpected pull latency bin fields for {drb_key}: {unexpected_bin_fields}", "warning")

                bin_start = safe_numeric(bin_data.get('pull_latency_bin_start_usec'), 'pull_latency_bin_start_usec')
                bin_count = safe_numeric(bin_data.get('pull_latency_bin_count'), 'pull_latency_bin_count')

                if bin_start is not None and bin_count is not None:
                    # Write individual bin data
                    point = Point("rlc_pull_latency_histogram") \
                        .field("bin_count", bin_count) \
                        .tag("drb_key", drb_key) \
                        .tag("bin_start_usec", str(int(bin_start))) \
                        .tag("component", "rlc")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

                    # Calculate aggregate statistics
                    total_pulls += bin_count
                    weighted_latency_sum += bin_start * bin_count

                    if bin_count > max_bin_count:
                        max_bin_count = bin_count
                        max_bin_start = bin_start

            # Calculate and write aggregate histogram statistics
            if total_pulls > 0:
                weighted_avg_latency = weighted_latency_sum / total_pulls

                # Write aggregate metrics
                agg_point = Point("rlc_pull_latency_stats") \
                    .field("total_pulls", total_pulls) \
                    .field("weighted_avg_latency_usec", weighted_avg_latency) \
                    .field("max_bin_count", max_bin_count) \
                    .field("max_bin_start_usec", max_bin_start) \
                    .tag("drb_key", drb_key) \
                    .tag("component", "rlc")
                if timestamp_dt:
                    agg_point = agg_point.time(timestamp_dt)
                influx_points.append(agg_point)

                # Track trends for weighted average latency
                stats = self.calculate_rlc_statistics(drb_key, "pull_latency_weighted_avg", weighted_avg_latency)
                if stats:
                    for stat_name, stat_value in stats.items():
                        if stat_value is not None:
                            stat_point = Point("rlc_pull_latency_trends") \
                                .field(f"weighted_avg_latency_{stat_name}", stat_value) \
                                .tag("drb_key", drb_key) \
                                .tag("statistic", stat_name) \
                                .tag("component", "rlc")
                            if timestamp_dt:
                                stat_point = stat_point.time(timestamp_dt)
                            influx_points.append(stat_point)

            # Write all histogram metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating pull latency histogram for {drb_key}: {e}", "error")

    def calculate_rlc_derived_metrics(self, tx_metrics: Dict[str, float], rx_metrics: Dict[str, float],
                                      drb_key: str, timestamp_dt: Optional[datetime] = None):
        """Calculate derived metrics from TX and RX RLC data."""
        influx_points = []

        try:
            # Calculate SDU drop rate
            tx_sdus = tx_metrics.get('num_sdus', 0)
            dropped_sdus = tx_metrics.get('num_dropped_sdus', 0)
            discarded_sdus = tx_metrics.get('num_discarded_sdus', 0)
            total_failed_sdus = dropped_sdus + discarded_sdus

            if tx_sdus > 0:
                sdu_drop_rate = (total_failed_sdus / tx_sdus) * 100
                point = Point("rlc_derived_metrics") \
                    .field("sdu_drop_rate_percent", sdu_drop_rate) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "drop_rate") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Calculate PDU loss rate
            rx_pdus = rx_metrics.get('num_pdus', 0)
            lost_pdus = rx_metrics.get('num_lost_pdus', 0)
            total_expected_pdus = rx_pdus + lost_pdus

            if total_expected_pdus > 0:
                pdu_loss_rate = (lost_pdus / total_expected_pdus) * 100
                point = Point("rlc_derived_metrics") \
                    .field("pdu_loss_rate_percent", pdu_loss_rate) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "loss_rate") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Calculate average SDU latency
            sum_sdu_latency = tx_metrics.get('sum_sdu_latency_us', 0)
            if tx_sdus > 0 and sum_sdu_latency > 0:
                avg_sdu_latency = sum_sdu_latency / tx_sdus
                point = Point("rlc_derived_metrics") \
                    .field("avg_sdu_latency_us", avg_sdu_latency) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "latency") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Calculate SDU and PDU size averages
            tx_sdu_bytes = tx_metrics.get('num_sdu_bytes', 0)
            tx_pdu_bytes = tx_metrics.get('num_pdu_bytes', 0)
            tx_pdus = tx_metrics.get('num_pdus', 0)
            rx_sdu_bytes = rx_metrics.get('num_sdu_bytes', 0)
            rx_pdu_bytes = rx_metrics.get('num_pdu_bytes', 0)
            rx_sdus = rx_metrics.get('num_sdus', 0)

            if tx_sdus > 0:
                avg_tx_sdu_size = tx_sdu_bytes / tx_sdus
                point = Point("rlc_derived_metrics") \
                    .field("avg_tx_sdu_size_bytes", avg_tx_sdu_size) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "size") \
                    .tag("direction", "tx") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            if tx_pdus > 0:
                avg_tx_pdu_size = tx_pdu_bytes / tx_pdus
                point = Point("rlc_derived_metrics") \
                    .field("avg_tx_pdu_size_bytes", avg_tx_pdu_size) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "size") \
                    .tag("direction", "tx") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            if rx_sdus > 0:
                avg_rx_sdu_size = rx_sdu_bytes / rx_sdus
                point = Point("rlc_derived_metrics") \
                    .field("avg_rx_sdu_size_bytes", avg_rx_sdu_size) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "size") \
                    .tag("direction", "rx") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            if rx_pdus > 0:
                avg_rx_pdu_size = rx_pdu_bytes / rx_pdus
                point = Point("rlc_derived_metrics") \
                    .field("avg_rx_pdu_size_bytes", avg_rx_pdu_size) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "size") \
                    .tag("direction", "rx") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Calculate efficiency metrics
            malformed_pdus = rx_metrics.get('num_malformed_pdus', 0)
            if rx_pdus > 0:
                pdu_integrity_rate = ((rx_pdus - malformed_pdus) / rx_pdus) * 100
                point = Point("rlc_derived_metrics") \
                    .field("pdu_integrity_rate_percent", pdu_integrity_rate) \
                    .tag("drb_key", drb_key) \
                    .tag("metric_type", "integrity") \
                    .tag("component", "rlc")
                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Write derived metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error calculating RLC derived metrics for {drb_key}: {e}", "error")

    def check_rlc_performance_thresholds(self, drb_key: str, tx_metrics: Dict[str, float],
                                         rx_metrics: Dict[str, float], timestamp_dt: Optional[datetime] = None):
        """Check RLC performance against thresholds and generate alerts."""
        influx_points = []

        try:
            alerts = []

            # Check SDU drop rate
            tx_sdus = tx_metrics.get('num_sdus', 0)
            dropped_sdus = tx_metrics.get('num_dropped_sdus', 0)
            discarded_sdus = tx_metrics.get('num_discarded_sdus', 0)
            total_failed_sdus = dropped_sdus + discarded_sdus

            if tx_sdus > 0:
                sdu_drop_rate = (total_failed_sdus / tx_sdus) * 100
                if sdu_drop_rate >= self.sdu_drop_rate_critical_percent:
                    alerts.append(('critical', f"{drb_key} SDU drop rate critical: {sdu_drop_rate:.2f}%"))
                elif sdu_drop_rate >= self.sdu_drop_rate_warning_percent:
                    alerts.append(('warning', f"{drb_key} SDU drop rate high: {sdu_drop_rate:.2f}%"))

            # Check PDU loss rate
            rx_pdus = rx_metrics.get('num_pdus', 0)
            lost_pdus = rx_metrics.get('num_lost_pdus', 0)
            total_expected_pdus = rx_pdus + lost_pdus

            if total_expected_pdus > 0:
                pdu_loss_rate = (lost_pdus / total_expected_pdus) * 100
                if pdu_loss_rate >= self.pdu_loss_rate_critical_percent:
                    alerts.append(('critical', f"{drb_key} PDU loss rate critical: {pdu_loss_rate:.2f}%"))
                elif pdu_loss_rate >= self.pdu_loss_rate_warning_percent:
                    alerts.append(('warning', f"{drb_key} PDU loss rate high: {pdu_loss_rate:.2f}%"))

            # Check average SDU latency
            sum_sdu_latency = tx_metrics.get('sum_sdu_latency_us', 0)
            if tx_sdus > 0 and sum_sdu_latency > 0:
                avg_sdu_latency = sum_sdu_latency / tx_sdus
                if avg_sdu_latency >= self.avg_sdu_latency_critical_us:
                    alerts.append(('critical', f"{drb_key} average SDU latency critical: {avg_sdu_latency:.1f}μs"))
                elif avg_sdu_latency >= self.avg_sdu_latency_warning_us:
                    alerts.append(('warning', f"{drb_key} average SDU latency high: {avg_sdu_latency:.1f}μs"))

            # Check max PDU latency
            max_pdu_latency_ns = tx_metrics.get('max_pdu_latency_ns', 0)
            if max_pdu_latency_ns > 0:
                if max_pdu_latency_ns >= self.max_pdu_latency_critical_ns:
                    alerts.append(
                        ('critical', f"{drb_key} max PDU latency critical: {max_pdu_latency_ns / 1000000:.1f}ms"))
                elif max_pdu_latency_ns >= self.max_pdu_latency_warning_ns:
                    alerts.append(('warning', f"{drb_key} max PDU latency high: {max_pdu_latency_ns / 1000000:.1f}ms"))

            # Log and write alerts
            for alert_level, alert_message in alerts:
                log_level = "error" if alert_level == "critical" else "warning"
                log_both(alert_message, log_level)

                # Write alert to InfluxDB
                point = Point("rlc_alerts") \
                    .field("alert_level_numeric", 2 if alert_level == "critical" else 1) \
                    .tag("drb_key", drb_key) \
                    .tag("alert_level", alert_level) \
                    .tag("alert_message", alert_message) \
                    .tag("component", "rlc")

                if timestamp_dt:
                    point = point.time(timestamp_dt)

                influx_points.append(point)

            # Write normal status if no alerts
            if not alerts:
                point = Point("rlc_alerts") \
                    .field("alert_level_numeric", 0) \
                    .tag("drb_key", drb_key) \
                    .tag("alert_level", "normal") \
                    .tag("component", "rlc")

                if timestamp_dt:
                    point = point.time(timestamp_dt)

                influx_points.append(point)

            # Write alert metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error checking RLC performance thresholds for {drb_key}: {e}", "error")

    def update_rlc_direction_metrics(self, direction_data: Dict[str, Any], direction: str,
                                     drb_key: str, timestamp_dt: Optional[datetime] = None):
        """Update RLC metrics for a specific direction (TX or RX)."""
        influx_points = []
        current_metrics = {}

        try:
            expected_fields = self.EXPECTED_TX_FIELDS if direction == 'tx' else self.EXPECTED_RX_FIELDS

            # Process all RLC direction metrics except histogram
            for field in expected_fields:
                if field == 'pull_latency_histogram':  # Handle histogram separately
                    continue

                value = safe_numeric(direction_data.get(field), field)
                if value is not None:
                    current_metrics[field] = value

                    # Write current value
                    point = Point("rlc_metrics") \
                        .field(field, value) \
                        .tag("direction", direction) \
                        .tag("drb_key", drb_key) \
                        .tag("component", "rlc")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

                    # Calculate and write statistics for key metrics
                    if field in ['num_sdus', 'num_sdu_bytes', 'sum_sdu_latency_us', 'max_pdu_latency_ns']:
                        stats = self.calculate_rlc_statistics(drb_key, f"{direction}_{field}", value)
                        if stats:
                            for stat_name, stat_value in stats.items():
                                if stat_value is not None:
                                    stat_point = Point("rlc_statistics") \
                                        .field(f"{field}_{stat_name}", stat_value) \
                                        .tag("direction", direction) \
                                        .tag("drb_key", drb_key) \
                                        .tag("metric_type", field) \
                                        .tag("statistic", stat_name) \
                                        .tag("component", "rlc")
                                    if timestamp_dt:
                                        stat_point = stat_point.time(timestamp_dt)
                                    influx_points.append(stat_point)

            # Handle pull latency histogram for TX direction
            if direction == 'tx':
                histogram_data = direction_data.get('pull_latency_histogram', [])
                if histogram_data:
                    self.update_pull_latency_histogram(histogram_data, drb_key, timestamp_dt)

            # Check for unexpected fields
            unexpected_fields = set(direction_data.keys()) - expected_fields
            if unexpected_fields:
                log_both(f"Unexpected RLC {direction} fields for {drb_key}: {unexpected_fields}", "warning")

            # Write RLC direction metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

            return current_metrics

        except Exception as e:
            log_both(f"Error updating RLC {direction} metrics for {drb_key}: {e}", "error")
            return {}

    def update_drb_metrics(self, drb_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update DRB (Data Radio Bearer) metrics."""
        try:
            # Extract DRB identifiers
            du_id = safe_numeric(drb_data.get('du_id'), 'du_id')
            ue_id = safe_numeric(drb_data.get('ue_id'), 'ue_id')
            drb_id = safe_numeric(drb_data.get('drb_id'), 'drb_id')

            if du_id is None or ue_id is None or drb_id is None:
                log_both("DRB data missing required identifiers (du_id, ue_id, drb_id)", "warning")
                return

            drb_key = self.generate_drb_key(int(du_id), int(ue_id), int(drb_id))

            # Add DRB to active set
            if drb_key not in self.active_drbs:
                self.active_drbs.add(drb_key)
                log_both(f"New DRB discovered: {drb_key}")

            # Check for unexpected DRB fields
            unexpected_fields = set(drb_data.keys()) - self.EXPECTED_DRB_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected DRB fields for {drb_key}: {unexpected_fields}", "warning")

            # Write DRB identifier metrics
            id_point = Point("rlc_drb_info") \
                .field("du_id", du_id) \
                .field("ue_id", ue_id) \
                .field("drb_id", drb_id) \
                .tag("drb_key", drb_key) \
                .tag("component", "rlc")
            if timestamp_dt:
                id_point = id_point.time(timestamp_dt)
            self.exporter.write_to_influx([id_point])

            tx_metrics = {}
            rx_metrics = {}

            # Process TX metrics
            tx_data = drb_data.get('tx', {})
            if tx_data:
                tx_metrics = self.update_rlc_direction_metrics(tx_data, 'tx', drb_key, timestamp_dt)
            else:
                log_both(f"DRB {drb_key} missing TX data", "warning")

            # Process RX metrics
            rx_data = drb_data.get('rx', {})
            if rx_data:
                rx_metrics = self.update_rlc_direction_metrics(rx_data, 'rx', drb_key, timestamp_dt)
            else:
                log_both(f"DRB {drb_key} missing RX data", "warning")

            # Calculate derived metrics and check thresholds if we have both TX and RX data
            if tx_metrics and rx_metrics:
                self.calculate_rlc_derived_metrics(tx_metrics, rx_metrics, drb_key, timestamp_dt)
                self.check_rlc_performance_thresholds(drb_key, tx_metrics, rx_metrics, timestamp_dt)

            # log_both(f"RLC metrics updated for {drb_key} - TX SDUs: {tx_metrics.get('num_sdus', 'N/A')}, "
            #          f"RX SDUs: {rx_metrics.get('num_sdus', 'N/A')}")

        except Exception as e:
            log_both(f"Error updating DRB metrics: {e}", "error")

    def update_rlc_metrics_list(self, rlc_metrics_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Update metrics for all RLC entries."""
        try:
            # Track which DRBs we received data for in this update
            received_drbs = set()

            for rlc_entry in rlc_metrics_list:
                # Validate RLC entry structure
                unexpected_entry_fields = set(rlc_entry.keys()) - self.EXPECTED_RLC_ENTRY_FIELDS
                if unexpected_entry_fields:
                    log_both(f"Unexpected RLC entry fields: {unexpected_entry_fields}", "warning")

                drb_data = rlc_entry.get('drb', {})
                if not drb_data:
                    log_both("RLC entry missing DRB data", "warning")
                    continue

                # Extract DRB key for tracking
                du_id = safe_numeric(drb_data.get('du_id'), 'du_id')
                ue_id = safe_numeric(drb_data.get('ue_id'), 'ue_id')
                drb_id = safe_numeric(drb_data.get('drb_id'), 'drb_id')

                if du_id is not None and ue_id is not None and drb_id is not None:
                    drb_key = self.generate_drb_key(int(du_id), int(ue_id), int(drb_id))
                    received_drbs.add(drb_key)

                # Update DRB metrics
                self.update_drb_metrics(drb_data, timestamp_dt)

            # Log missing data DRBs
            missing_data_drbs = self.active_drbs - received_drbs
            # if missing_data_drbs:
            #     log_both(f"Missing data for active DRBs: {missing_data_drbs}")

            # log_both(f"RLC metrics update complete. Active DRBs: {len(self.active_drbs)}, "
            #          f"Received data: {len(received_drbs)}, Missing data: {len(missing_data_drbs)}")

        except Exception as e:
            log_both(f"Error updating RLC metrics list: {e}", "error")

    def update_metrics(self, entry: Dict[str, Any]):
        """Main metrics update function."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)

            # STEP 1: Update RLC metrics
            rlc_metrics_list = entry.get("rlc_metrics", [])
            if isinstance(rlc_metrics_list, list) and rlc_metrics_list:
                self.update_rlc_metrics_list(rlc_metrics_list, timestamp_dt)
            elif rlc_metrics_list:
                log_both("RLC metrics data is not a list or is empty", "warning")
            else:
                log_both("Message missing RLC metrics data", "warning")

            # STEP 2: Update system metrics
            self.message_count += 1
            system_points = []

            if timestamp:
                timestamp_val = safe_numeric(timestamp, "timestamp")
                if timestamp_val is not None:
                    system_points.append(
                        Point("rlc_system_metrics").field("last_update_timestamp", timestamp_val).tag("component",
                                                                                                      "rlc")
                    )

            system_points.append(
                Point("rlc_system_metrics").field("total_messages_received", self.message_count).tag("component",
                                                                                                     "rlc")
            )

            system_points.append(
                Point("rlc_system_metrics").field("total_parse_errors", self.parse_error_count).tag("component", "rlc")
            )

            # Add RLC monitoring health metrics
            system_points.append(
                Point("rlc_system_metrics").field("active_drbs_count", len(self.active_drbs)).tag("component", "rlc")
            )

            # Count total history samples across all DRBs
            total_history_samples = sum(
                len(metrics_dict.get('tx_num_sdus', []))
                for metrics_dict in self.rlc_performance_history.values()
            )

            system_points.append(
                Point("rlc_system_metrics").field("total_history_samples", total_history_samples).tag("component",
                                                                                                      "rlc")
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
