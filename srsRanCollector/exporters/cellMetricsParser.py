from collections import defaultdict
from typing import Dict, Any, List, Optional
from datetime import datetime
from influxdb_client import InfluxDBClient, Point, WriteOptions
from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time
from exporters.exporter import exporter


class cellMetricsParser:
    def __init__(self, main_exporter: exporter):
        # make an exporter
        self.exporter = main_exporter
        # Counters and tracking
        self.message_count = 0
        self.parse_error_count = 0
        self.ue_rem = 0
        self.ue_create = 0
        self.active_ues = set()  # Track which RNTIs are currently active
        self.event_counter = defaultdict(int)

        # Expected field sets for validation
        self.EXPECTED_UE_FIELDS = {
            'pci',
            'rnti',
            'cqi',
            'dl_ri',
            'ul_ri',
            'ri',
            'dl_mcs',
            'dl_brate',
            'dl_nof_ok',
            'dl_nof_nok',
            'dl_bs',
            'pusch_snr_db',
            'pusch_rsrp_db',
            'pucch_snr_db',
            'ta_ns',
            'pusch_ta_ns',
            'pucch_ta_ns',
            'srs_ta_ns',
            'ul_mcs',
            'ul_brate',
            'ul_nof_ok',
            'ul_nof_nok',
            'last_phr',
            'bsr',
            'nof_pucch_f0f1_invalid_harqs',
            'nof_pucch_f2f3f4_invalid_harqs',
            'nof_pucch_f2f3f4_invalid_csis',
            'nof_pusch_invalid_harqs',
            'nof_pusch_invalid_csis',
            'avg_ce_delay',
            'max_ce_delay',
            'avg_crc_delay',
            'max_crc_delay',
            'avg_pusch_harq_delay',
            'max_pusch_harq_delay',
            'avg_pucch_harq_delay',
            'max_pucch_harq_delay'
        }
        self.EXPECTED_EVENT_FIELDS = {'sfn', 'slot_index', 'rnti', 'event_type'}
        self.EXPECTED_TOP_FIELDS = {'timestamp', 'cell_metrics', 'ue_list', 'event_list'}

    def update_cell_metrics(self, cell_metrics: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update cell-level metrics to InfluxDB."""
        influx_points = []

        try:
            # Handle basic cell metrics
            for field in ["average_latency", "error_indication_count"]:
                value = safe_numeric(cell_metrics.get(field), field)
                if value is not None:
                    point = Point("cell_metrics").field(field, value).tag("source", "srs_ran")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Handle latency histogram
            hist = cell_metrics.get("latency_histogram", [])
            if isinstance(hist, list):
                for i, bucket_val in enumerate(hist[:10]):  # Limit to 10 buckets
                    bucket_val = safe_numeric(bucket_val, f"latency_histogram[{i}]")
                    if bucket_val is not None:
                        point = Point("cell_metrics") \
                            .field("latency_histogram_bucket", bucket_val) \
                            .tag("bucket", str(i)) \
                            .tag("source", "srs_ran")
                        if timestamp_dt:
                            point = point.time(timestamp_dt)
                        influx_points.append(point)

            # Write all cell metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating cell metrics: {e}", "error")
            self.parse_error_count += 1

    def handle_ue_lifecycle_events(self, event_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Handle UE creation and removal events to manage active UE tracking."""
        influx_points = []

        for event in event_list:
            cell_event = event.get("cell_events", {})
            if not cell_event:
                continue

            event_type = cell_event.get("event_type")
            rnti = cell_event.get("rnti")

            if not event_type or rnti is None:
                log_both(f"cell_event logged with no rnti and/or event_type: {cell_event}")
                continue

            rnti_str = str(rnti)

            if event_type == "ue_create":
                if rnti_str not in self.active_ues:
                    self.ue_create += 1
                    self.active_ues.add(rnti_str)
                    log_both(f"UE created: RNTI {rnti_str} (Total active: {len(self.active_ues)})")

                    # Write UE creation event to InfluxDB
                    point = Point("ue_lifecycle").field("event", self.ue_create).tag("event_type", "create").tag("rnti",
                                                                                                                 rnti_str).tag(
                        "source", "srs_ran")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)
                else:
                    log_both(f"Warning: UE creation event for already active RNTI {rnti_str}", "warning")

            elif event_type == "ue_rem":
                self.ue_rem += 1
                if rnti_str in self.active_ues:
                    self.active_ues.remove(rnti_str)
                    log_both(f"UE removed: RNTI {rnti_str} (Total active: {len(self.active_ues)})")

                    # Write UE removal event to InfluxDB
                    point = Point("ue_lifecycle").field("event", self.ue_rem).tag("event_type", "remove").tag("rnti",
                                                                                                              rnti_str).tag(
                        "source", "srs_ran")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)
                else:
                    log_both(f"Warning: UE removal event for unknown RNTI {rnti_str}", "warning")

            elif event_type != "ue_reconf":
                self.parse_error_count += 1
        # Write lifecycle events to InfluxDB
        if influx_points:
            self.exporter.write_to_influx(influx_points)

    def update_ue_metrics(self, ue_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Update UE-specific metrics only for actively tracked UEs."""
        influx_points = []

        try:
            # Write UE count to InfluxDB
            point = Point("ue_metrics").field("ue_count", len(self.active_ues)).tag("source", "srs_ran")
            if timestamp_dt:
                point = point.time(timestamp_dt)
            influx_points.append(point)

            # Track which RNTIs we received data for in this update
            received_rntis = set()

            # Process UEs that have data in this message
            for ue in ue_list:
                container = ue.get("ue_container", {})
                if not container:
                    continue

                rnti = str(container.get("rnti", "unknown"))
                received_rntis.add(rnti)

                # Only update metrics for UEs that are actively tracked
                if rnti not in self.active_ues:
                    log_both(f"Warning: Received UE data for non-active RNTI {rnti}. Skipping metrics update.",
                             "warning")
                    continue

                # Update all UE metrics for this active UE
                for field in self.EXPECTED_UE_FIELDS:
                    if field == "rnti":  # Skip rnti as it's used as a tag
                        continue

                    value = safe_numeric(container.get(field), field)
                    if value is not None:
                        point = Point("ue_metrics").field(field, value).tag("rnti", rnti).tag("source", "srs_ran")
                        if timestamp_dt:
                            point = point.time(timestamp_dt)
                        influx_points.append(point)

                # Check for unexpected fields
                unexpected_fields = set(container.keys()) - self.EXPECTED_UE_FIELDS
                if unexpected_fields:
                    log_both(f"Unexpected UE fields for RNTI {rnti}: {unexpected_fields}", "warning")
                    self.parse_error_count += 1

            # dont want to comment this out but I cannot think of another way of doing this elegantly
            #  if metrics get sent seperately for each rnti
            # --------------------------------------------
            # missing_data_rntis = self.active_ues - received_rntis
            # if missing_data_rntis:
            #     log_both(f"missing data, active_ues: {self.active_ues}, received_rnti's: {received_rntis}")
            # ----------------------------------------------

            # Write all UE metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

            # log_both( f"UE metrics update complete. Active: {len(self.active_ues)}, Received data: {len(
            # received_rntis)}, Missing data: {len(missing_data_rntis)}")

        except Exception as e:
            log_both(f"Error updating UE metrics: {e}", "error")
            self.parse_error_count += 1

    def update_event_metrics(self, event_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Update event-related metrics."""
        influx_points = []

        try:
            for event in event_list:
                cell_event = event.get("cell_events", {})
                if not cell_event:
                    continue

                event_type = cell_event.get("event_type")
                if event_type:
                    self.event_counter[event_type] += 1

                    # Write event count to InfluxDB
                    point = Point("event_metrics").field("event_count", self.event_counter[event_type]).tag(
                        "event_type",
                        event_type).tag(
                        "source", "srs_ran")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

                    # Track latest event timing info
                    for field in ["sfn", "slot_index"]:
                        value = safe_numeric(cell_event.get(field), field)
                        if value is not None:
                            point = Point("event_timing").field(field, value).tag("event_type", event_type).tag(
                                "source",
                                "srs_ran")
                            if timestamp_dt:
                                point = point.time(timestamp_dt)
                            influx_points.append(point)

                # Check for unexpected event fields
                unexpected_fields = set(cell_event.keys()) - self.EXPECTED_EVENT_FIELDS
                if unexpected_fields:
                    self.parse_error_count += 1
                    log_both(f"Unexpected event fields: {unexpected_fields}", "warning")

            # Write all event metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating event metrics: {e}", "error")
            self.parse_error_count += 1

    def update_metrics(self, entry: Dict[str, Any]):
        """Main metrics update function."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)

            # STEP 1: Process lifecycle events FIRST to update active_ues
            event_list = entry.get("event_list", [])
            if event_list:
                self.handle_ue_lifecycle_events(event_list, timestamp_dt)

            # STEP 2: Update cell metrics
            cell_metrics = entry.get("cell_metrics", {})
            if cell_metrics:
                self.update_cell_metrics(cell_metrics, timestamp_dt)

            # STEP 3: Update UE metrics for active UEs
            ue_list = entry.get("ue_list", [])
            self.update_ue_metrics(ue_list, timestamp_dt)

            # STEP 4: Update event metrics
            if event_list:
                self.update_event_metrics(event_list, timestamp_dt)

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

            # Check for unexpected top-level fields
            unexpected_fields = set(entry.keys()) - self.EXPECTED_TOP_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected top-level fields: {unexpected_fields}", "warning")
                self.parse_error_count += 1

        except Exception as e:
            log_both(f"Error in update_metrics: {e}", "error")
