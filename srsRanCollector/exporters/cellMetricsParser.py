from collections import defaultdict
from typing import Dict, Any, List, Optional, TYPE_CHECKING
from datetime import datetime, timedelta
from influxdb_client import InfluxDBClient, Point, WriteOptions
from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time
from exporters.exporter import exporter
from exporters.imeisvParser import imeisvParser


class cellMetricsParser:
    def __init__(self, main_exporter: exporter, ue_timeout_seconds: int = 150):
        # make an exporter
        self.exporter = main_exporter

        # Reference to IMEISV mapping parser (will be set by collector)
        self.imeisv_mapper: Optional['imeisvParser'] = None

        # Counters and tracking
        self.message_count = 0
        self.parse_error_count = 0
        self.ue_rem = 0
        self.ue_create = 0
        self.ue_auto_discovered = 0
        self.ue_auto_disconnected = 0
        self.handover_detected = 0  # New: track handovers detected via IMEISV

        # UE tracking - now with IMEISV support
        self.active_ues = set()  # Track which RNTIs are currently active
        self.ue_last_seen = {}  # Track last seen timestamps for each UE {rnti_str: datetime}
        self.ue_timeout_seconds = ue_timeout_seconds

        # IMEISV-enhanced tracking
        self.rnti_to_imeisv_cache = {}  # Local cache for quick lookups
        self.imeisv_persistent_ues = set()  # Track UEs by IMEISV (persistent across handovers)

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

    def set_imeisv_mapper(self, imeisv_mapper: 'imeisvParser'):
        """Set reference to IMEISV mapping parser."""
        self.imeisv_mapper = imeisv_mapper
        log_both("IMEISV mapper linked to cell metrics parser")

    def _get_imeisv_for_rnti(self, rnti: int) -> Optional[int]:
        """Get IMEISV for RNTI, using cache and mapper."""
        # First try cache
        if rnti in self.rnti_to_imeisv_cache:
            return self.rnti_to_imeisv_cache[rnti]

        # Try mapper if available
        if self.imeisv_mapper:
            imeisv = self.imeisv_mapper.get_imeisv_for_rnti(rnti)
            if imeisv:
                self.rnti_to_imeisv_cache[rnti] = imeisv
                return imeisv

        return None

    def _update_imeisv_cache(self, rnti: int, imeisv: int):
        """Update local IMEISV cache."""
        self.rnti_to_imeisv_cache[rnti] = imeisv

    def _clear_rnti_from_cache(self, rnti: int):
        """Remove RNTI from cache when UE disconnects."""
        if rnti in self.rnti_to_imeisv_cache:
            del self.rnti_to_imeisv_cache[rnti]

    def update_cell_metrics(self, cell_metrics: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update cell-level metrics to InfluxDB."""
        influx_points = []

        try:
            # Handle basic cell metrics
            for field in ["average_latency", "error_indication_count", "max_latency", "nof_failed_pdcch_allocs",
                          "nof_failed_uci_allocs"]:
                value = safe_numeric(cell_metrics.get(field), field)
                if value is not None:
                    point = Point("cell_metrics").field(field, value).tag("component", "cell")
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
                            .tag("component", "cell")
                        if timestamp_dt:
                            point = point.time(timestamp_dt)
                        influx_points.append(point)

            # Write all cell metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating cell metrics: {e}", "error")
            self.parse_error_count += 1

    def handle_ue_lifecycle_events(self, event_list: List[Dict[str, Any]],
                                   timestamp_dt: Optional[datetime] = None) -> None:
        """Enhanced UE lifecycle event handling with IMEISV awareness."""
        influx_points = []
        now = timestamp_dt or datetime.utcnow()

        for event in event_list:
            try:
                # Check if this event has cell_events
                if "cell_events" in event:
                    cell_event = event["cell_events"]

                    # Handle both dict (new format) and list (backward compatibility)
                    if isinstance(cell_event, dict):
                        self._process_single_cell_event_enhanced(cell_event, now, influx_points)
                    elif isinstance(cell_event, list):
                        for single_cell_event in cell_event:
                            self._process_single_cell_event_enhanced(single_cell_event, now, influx_points)
                    else:
                        log_both(f"Unexpected cell_events format: {type(cell_event)}", level="warning")
                else:
                    # Fallback: treat the entire event as a cell_event
                    self._process_single_cell_event_enhanced(event, now, influx_points)

            except Exception as e:
                log_both(f"Exception processing event {event}: {e}", level="error")
                self.parse_error_count += 1

        # Flush all points to InfluxDB
        if influx_points:
            self.exporter.write_to_influx(influx_points)

    def _process_single_cell_event_enhanced(self, cell_event: Dict[str, Any], now: datetime,
                                            influx_points: List) -> None:
        """Enhanced single cell event processing with IMEISV tracking."""
        if not cell_event:
            log_both(f"Empty cell_event found: {cell_event}", level="warning")
            return

        event_type = cell_event.get("event_type")
        rnti = cell_event.get("rnti")
        sfn = cell_event.get("sfn")
        slot_index = cell_event.get("slot_index")

        if not event_type or rnti is None:
            log_both(f"Event missing 'event_type' or 'rnti': {cell_event}", level="warning")
            return

        rnti_str = str(rnti)
        imeisv = self._get_imeisv_for_rnti(rnti)

        # Log additional timing information if available
        timing_info = ""
        if sfn is not None and slot_index is not None:
            timing_info = f" (SFN: {sfn}, Slot: {slot_index})"

        if event_type == "ue_create":
            if rnti_str in self.active_ues:
                # Check if this is a handover rather than duplicate create
                if imeisv and imeisv in self.imeisv_persistent_ues:
                    log_both(f"Potential handover detected: RNTI {rnti_str} for existing IMEISV {imeisv}{timing_info}")
                    self.handover_detected += 1
                else:
                    log_both(f"UE creation event for already active RNTI {rnti_str}{timing_info}", level="warning")
                return

            self.active_ues.add(rnti_str)
            self.ue_last_seen[rnti_str] = now
            self.ue_create += 1

            # Add to persistent tracking if IMEISV available
            if imeisv:
                self.imeisv_persistent_ues.add(imeisv)
                log_both(
                    f"UE created: RNTI {rnti_str}, IMEISV {imeisv}{timing_info} (Total active RNTIs: {len(self.active_ues)}, IMEISVs: {len(self.imeisv_persistent_ues)})")
            else:
                log_both(
                    f"UE created: RNTI {rnti_str} (no IMEISV mapping){timing_info} (Total active: {len(self.active_ues)})")

            # Create enhanced point with IMEISV if available
            point = (
                Point("ue_lifecycle")
                .field("event", self.ue_create)
                .tag("event_type", "create")
                .tag("rnti", rnti_str)
                .tag("component", "cell")
                .time(now)
            )

            if imeisv:
                point = point.tag("imeisv", str(imeisv))
            if sfn is not None:
                point = point.tag("sfn", str(sfn))
            if slot_index is not None:
                point = point.tag("slot_index", str(slot_index))

            influx_points.append(point)

        elif event_type == "ue_rem":
            self.ue_rem += 1

            if rnti_str in self.active_ues:
                self.active_ues.remove(rnti_str)
                self.ue_last_seen.pop(rnti_str, None)
                self._clear_rnti_from_cache(rnti)

                # Only remove from persistent tracking if no other RNTIs for this IMEISV
                if imeisv:
                    # Check if any other active RNTIs map to this IMEISV
                    other_rntis_for_imeisv = [
                        r for r in self.active_ues
                        if self._get_imeisv_for_rnti(int(r)) == imeisv
                    ]

                    if not other_rntis_for_imeisv:
                        self.imeisv_persistent_ues.discard(imeisv)
                        log_both(
                            f"UE removed: RNTI {rnti_str}, IMEISV {imeisv}{timing_info} (Total active RNTIs: {len(self.active_ues)}, IMEISVs: {len(self.imeisv_persistent_ues)})")
                    else:
                        log_both(
                            f"UE removed: RNTI {rnti_str}, IMEISV {imeisv} still has {len(other_rntis_for_imeisv)} other RNTIs{timing_info}")
                else:
                    log_both(
                        f"UE removed: RNTI {rnti_str} (no IMEISV mapping){timing_info} (Total active: {len(self.active_ues)})")

                point = (
                    Point("ue_lifecycle")
                    .field("event", self.ue_rem)
                    .tag("event_type", "remove")
                    .tag("rnti", rnti_str)
                    .tag("component", "cell")
                    .time(now)
                )

                if imeisv:
                    point = point.tag("imeisv", str(imeisv))
                if sfn is not None:
                    point = point.tag("sfn", str(sfn))
                if slot_index is not None:
                    point = point.tag("slot_index", str(slot_index))

                influx_points.append(point)
            else:
                log_both(f"UE removal event for unknown RNTI {rnti_str}{timing_info}", level="warning")

        elif event_type == "ue_reconf":
            if rnti_str not in self.active_ues:
                log_both(f"UE reconfiguration for untracked RNTI {rnti_str}; auto-adding.{timing_info}", level="info")
                self.active_ues.add(rnti_str)
                self.ue_auto_discovered += 1

                # Add to persistent tracking if IMEISV available
                if imeisv:
                    self.imeisv_persistent_ues.add(imeisv)

            self.ue_last_seen[rnti_str] = now

            point = (
                Point("ue_lifecycle")
                .field("event", 1)
                .tag("event_type", "reconf")
                .tag("rnti", rnti_str)
                .tag("component", "cell")
                .time(now)
            )

            if imeisv:
                point = point.tag("imeisv", str(imeisv))
            if sfn is not None:
                point = point.tag("sfn", str(sfn))
            if slot_index is not None:
                point = point.tag("slot_index", str(slot_index))

            influx_points.append(point)

        else:
            log_both(f"Unknown event type: {event_type} for RNTI {rnti_str}{timing_info}", level="warning")
            self.parse_error_count += 1

    def auto_discover_ue(self, rnti_str: str, timestamp_dt: Optional[datetime] = None):
        """Enhanced auto-discovery with IMEISV awareness."""
        if rnti_str not in self.active_ues:
            self.active_ues.add(rnti_str)
            self.ue_last_seen[rnti_str] = timestamp_dt or datetime.utcnow()
            self.ue_auto_discovered += 1

            # Check for IMEISV mapping
            imeisv = self._get_imeisv_for_rnti(int(rnti_str))
            if imeisv:
                self.imeisv_persistent_ues.add(imeisv)
                log_both(
                    f"Auto-discovered UE: RNTI {rnti_str}, IMEISV {imeisv} (Total active RNTIs: {len(self.active_ues)}, IMEISVs: {len(self.imeisv_persistent_ues)})")
            else:
                log_both(
                    f"Auto-discovered UE: RNTI {rnti_str} (no IMEISV mapping) (Total active: {len(self.active_ues)})")

            # Write auto-discovery event to InfluxDB
            point = Point("ue_lifecycle").field("event", self.ue_auto_discovered).tag("event_type",
                                                                                      "auto_discovered").tag("rnti",
                                                                                                             rnti_str).tag(
                "component", "cell")

            if imeisv:
                point = point.tag("imeisv", str(imeisv))
            if timestamp_dt:
                point = point.time(timestamp_dt)

            self.exporter.write_to_influx([point])

    def check_ue_timeouts(self, current_time: Optional[datetime] = None):
        """Enhanced timeout checking with IMEISV persistence."""
        if current_time is None:
            current_time = datetime.utcnow()

        timeout_threshold = timedelta(seconds=self.ue_timeout_seconds)
        timed_out_ues = []
        influx_points = []

        # Find UEs that have timed out
        for rnti_str in list(self.active_ues):
            last_seen = self.ue_last_seen.get(rnti_str)
            if last_seen is None:
                self.ue_last_seen[rnti_str] = current_time
                continue

            time_since_last_seen = current_time - last_seen
            if time_since_last_seen > timeout_threshold:
                timed_out_ues.append(rnti_str)

        # Remove timed out UEs
        for rnti_str in timed_out_ues:
            imeisv = self._get_imeisv_for_rnti(int(rnti_str))

            self.active_ues.remove(rnti_str)
            if rnti_str in self.ue_last_seen:
                del self.ue_last_seen[rnti_str]
            self._clear_rnti_from_cache(int(rnti_str))
            self.ue_auto_disconnected += 1

            # Only remove from persistent tracking if no other RNTIs for this IMEISV
            if imeisv:
                other_rntis_for_imeisv = [
                    r for r in self.active_ues
                    if self._get_imeisv_for_rnti(int(r)) == imeisv
                ]

                if not other_rntis_for_imeisv:
                    self.imeisv_persistent_ues.discard(imeisv)
                    log_both(
                        f"Auto-disconnected UE: RNTI {rnti_str}, IMEISV {imeisv} after {self.ue_timeout_seconds}s timeout (Total active RNTIs: {len(self.active_ues)}, IMEISVs: {len(self.imeisv_persistent_ues)})")
                else:
                    log_both(
                        f"Auto-disconnected UE: RNTI {rnti_str}, IMEISV {imeisv} still has {len(other_rntis_for_imeisv)} other RNTIs after timeout")
            else:
                log_both(
                    f"Auto-disconnected UE: RNTI {rnti_str} (no IMEISV mapping) after {self.ue_timeout_seconds}s timeout (Total active: {len(self.active_ues)})")

            # Write auto-disconnect event to InfluxDB
            point = Point("ue_lifecycle").field("event", self.ue_auto_disconnected).tag("event_type",
                                                                                        "auto_disconnected").tag("rnti",
                                                                                                                 rnti_str).tag(
                "component", "cell").time(current_time)

            if imeisv:
                point = point.tag("imeisv", str(imeisv))

            influx_points.append(point)

        # Write auto-disconnect events to InfluxDB
        if influx_points:
            self.exporter.write_to_influx(influx_points)

        return len(timed_out_ues)

    def update_ue_metrics(self, ue_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Enhanced UE metrics update with IMEISV correlation."""
        influx_points = []

        try:
            # Write UE counts to InfluxDB (both RNTI and IMEISV based)
            point = Point("ue_metrics").field("ue_count_rnti", len(ue_list)).tag("component", "cell")
            if timestamp_dt:
                point = point.time(timestamp_dt)
            influx_points.append(point)

            point = Point("ue_metrics").field("ue_count_imeisv", len(self.imeisv_persistent_ues)).tag("component",
                                                                                                      "cell")
            if timestamp_dt:
                point = point.time(timestamp_dt)
            influx_points.append(point)

            # Track which RNTIs we received data for in this update
            received_rntis = set()

            # Process UEs that have data in this message
            for ue in ue_list:
                try:
                    container = ue.get("ue_container", {})
                    if not container:
                        log_both(f"Empty ue_container found in UE: {ue}", "warning")
                        continue

                    pci = ue.get("pci")
                    rnti = container.get("rnti")

                    if rnti is None:
                        log_both(f"UE container missing RNTI: {container}", "warning")
                        continue

                    rnti_str = str(rnti)
                    received_rntis.add(rnti_str)

                    # Get IMEISV for this RNTI
                    imeisv = self._get_imeisv_for_rnti(rnti)

                    # Auto-discover UE if not already tracked
                    if rnti_str not in self.active_ues:
                        log_both(f"Auto-discovering UE with RNTI {rnti_str} from metrics data", "info")
                        self.auto_discover_ue(rnti_str, timestamp_dt)
                    else:
                        # Update last seen time for existing UE
                        self.ue_last_seen[rnti_str] = timestamp_dt or datetime.utcnow()

                    # Update all UE metrics for this UE
                    for field in self.EXPECTED_UE_FIELDS:
                        if field == "rnti":  # Skip rnti as it's used as a tag
                            continue

                        value = safe_numeric(container.get(field), field)
                        if value is not None:
                            point = Point("ue_metrics").field(field, value).tag("rnti", rnti_str).tag("component",
                                                                                                      "cell")

                            if pci is not None:
                                point = point.tag("pci", str(pci))
                            if imeisv is not None:
                                point = point.tag("imeisv", str(imeisv))
                            if timestamp_dt:
                                point = point.time(timestamp_dt)

                            influx_points.append(point)

                    # Check for unexpected fields
                    unexpected_fields = set(container.keys()) - self.EXPECTED_UE_FIELDS
                    if unexpected_fields:
                        log_both(f"Unexpected UE fields for RNTI {rnti_str}: {unexpected_fields}", "warning")
                        self.parse_error_count += 1

                except Exception as e:
                    log_both(f"Error processing individual UE {ue}: {e}", "error")
                    self.parse_error_count += 1
                    continue

            # Write all UE metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating UE metrics: {e}", "error")
            self.parse_error_count += 1

    def update_event_metrics(self, event_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Update event-related metrics with IMEISV correlation."""
        influx_points = []

        try:
            # Process each event in the event_list
            for event_index, event in enumerate(event_list):
                try:
                    # Handle different event structure formats
                    cell_event = event.get("cell_events", {})
                    if not cell_event:
                        # Try direct access if no cell_events wrapper
                        cell_event = event

                    if not cell_event:
                        log_both(f"Empty event found at index {event_index}: {event}", "warning")
                        continue

                    event_type = cell_event.get("event_type")
                    rnti = cell_event.get("rnti")
                    imeisv = None

                    if rnti is not None:
                        imeisv = self._get_imeisv_for_rnti(rnti)

                    if event_type:
                        self.event_counter[event_type] += 1

                        # Write event count to InfluxDB
                        point = Point("event_metrics").field("event_count", self.event_counter[event_type]).tag(
                            "event_type", event_type).tag("component", "cell")

                        if imeisv is not None:
                            point = point.tag("imeisv", str(imeisv))
                        if timestamp_dt:
                            point = point.time(timestamp_dt)

                        influx_points.append(point)

                        # Track latest event timing info
                        for field in ["sfn", "slot_index"]:
                            value = safe_numeric(cell_event.get(field), field)
                            if value is not None:
                                point = Point("event_timing").field(field, value).tag("event_type", event_type).tag(
                                    "component", "cell")

                                if imeisv is not None:
                                    point = point.tag("imeisv", str(imeisv))
                                if timestamp_dt:
                                    point = point.time(timestamp_dt)

                                influx_points.append(point)

                        # Include RNTI in event timing if available
                        if rnti is not None:
                            point = Point("event_timing").field("rnti", rnti).tag("event_type", event_type).tag(
                                "component", "cell")

                            if imeisv is not None:
                                point = point.tag("imeisv", str(imeisv))
                            if timestamp_dt:
                                point = point.time(timestamp_dt)

                            influx_points.append(point)

                    # Check for unexpected event fields
                    unexpected_fields = set(cell_event.keys()) - self.EXPECTED_EVENT_FIELDS
                    if unexpected_fields:
                        log_both(f"Unexpected event fields in event {event_index}: {unexpected_fields}", "warning")
                        self.parse_error_count += 1

                except Exception as e:
                    log_both(f"Error processing event at index {event_index}: {e}", "error")
                    self.parse_error_count += 1
                    continue

            # Write all event metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating event metrics: {e}", "error")
            self.parse_error_count += 1

    def update_metrics(self, entry: Dict[str, Any]):
        """Enhanced main metrics update function with IMEISV integration."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)
            current_time = timestamp_dt or datetime.utcnow()

            # STEP 0: Check for UE timeouts and auto-disconnect stale UEs
            disconnected_count = self.check_ue_timeouts(current_time)
            if disconnected_count > 0:
                log_both(f"Auto-disconnected {disconnected_count} UEs due to timeout")

            # STEP 1: Process lifecycle events FIRST to update active_ues
            event_list = entry.get("event_list", [])
            if event_list:
                if not isinstance(event_list, list):
                    log_both(f"event_list is not a list: {type(event_list)}", "error")
                    self.parse_error_count += 1
                else:
                    self.handle_ue_lifecycle_events(event_list, timestamp_dt)

            # STEP 2: Update cell metrics
            cell_metrics = entry.get("cell_metrics", {})
            if cell_metrics:
                if not isinstance(cell_metrics, dict):
                    log_both(f"cell_metrics is not a dict: {type(cell_metrics)}", "error")
                    self.parse_error_count += 1
                else:
                    self.update_cell_metrics(cell_metrics, timestamp_dt)

            # STEP 3: Update UE metrics (with auto-discovery and IMEISV correlation)
            ue_list = entry.get("ue_list", [])
            if not isinstance(ue_list, list):
                log_both(f"ue_list is not a list: {type(ue_list)}", "error")
                self.parse_error_count += 1
            else:
                self.update_ue_metrics(ue_list, timestamp_dt)

            # STEP 4: Update event metrics
            if event_list and isinstance(event_list, list):
                self.update_event_metrics(event_list, timestamp_dt)

            # STEP 5: Update enhanced system metrics
            self.message_count += 1
            system_points = []

            if timestamp:
                timestamp_val = safe_numeric(timestamp, "timestamp")
                if timestamp_val is not None:
                    system_points.append(
                        Point("system_metrics").field("last_update_timestamp", timestamp_val).tag("component", "cell")
                    )

            system_points.extend([
                Point("system_metrics").field("total_messages_received", self.message_count).tag("component", "cell"),
                Point("system_metrics").field("total_parse_errors", self.parse_error_count).tag("component", "cell"),
                Point("system_metrics").field("total_ue_creates", self.ue_create).tag("component", "cell"),
                Point("system_metrics").field("total_ue_removes", self.ue_rem).tag("component", "cell"),
                Point("system_metrics").field("total_ue_auto_discovered", self.ue_auto_discovered).tag("component",
                                                                                                       "cell"),
                Point("system_metrics").field("total_ue_auto_disconnected", self.ue_auto_disconnected).tag("component",
                                                                                                           "cell"),
                Point("system_metrics").field("total_handovers_detected", self.handover_detected).tag("component",
                                                                                                      "cell"),
                Point("system_metrics").field("active_ue_count_rnti", len(self.active_ues)).tag("component", "cell"),
                Point("system_metrics").field("active_ue_count_imeisv", len(self.imeisv_persistent_ues)).tag(
                    "component", "cell"),
                Point("system_metrics").field("ue_timeout_seconds", self.ue_timeout_seconds).tag("component", "cell"),
                Point("system_metrics").field("imeisv_cache_size", len(self.rnti_to_imeisv_cache)).tag("component",
                                                                                                       "cell")
            ])

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
            self.parse_error_count += 1

    def get_stats(self):
        """Return enhanced parser statistics with IMEISV tracking."""
        return {
            "message_count": self.message_count,
            "parse_error_count": self.parse_error_count,
            "ue_create": self.ue_create,
            "ue_rem": self.ue_rem,
            "ue_auto_discovered": self.ue_auto_discovered,
            "ue_auto_disconnected": self.ue_auto_disconnected,
            "handover_detected": self.handover_detected,
            "active_ues_count_rnti": len(self.active_ues),
            "active_ues_count_imeisv": len(self.imeisv_persistent_ues),
            "active_ues": list(self.active_ues),
            "active_imeisvs": list(self.imeisv_persistent_ues),
            "ue_timeout_seconds": self.ue_timeout_seconds,
            "ue_last_seen_count": len(self.ue_last_seen),
            "imeisv_cache_size": len(self.rnti_to_imeisv_cache),
            "event_counter": dict(self.event_counter),
            "imeisv_mapper_available": self.imeisv_mapper is not None
        }

    def get_ue_connection_status_enhanced(self):
        """Return enhanced UE connection status with IMEISV correlation."""
        current_time = datetime.utcnow()
        status = {}

        for rnti_str in self.active_ues:
            last_seen = self.ue_last_seen.get(rnti_str)
            imeisv = self._get_imeisv_for_rnti(int(rnti_str))

            if last_seen:
                time_since_last_seen = current_time - last_seen
                status[rnti_str] = {
                    "last_seen": last_seen.isoformat(),
                    "seconds_since_last_seen": time_since_last_seen.total_seconds(),
                    "is_stale": time_since_last_seen.total_seconds() > self.ue_timeout_seconds,
                    "imeisv": imeisv,
                    "has_imeisv_mapping": imeisv is not None
                }
            else:
                status[rnti_str] = {
                    "last_seen": None,
                    "seconds_since_last_seen": None,
                    "is_stale": False,
                    "imeisv": imeisv,
                    "has_imeisv_mapping": imeisv is not None
                }

        return status

    def get_imeisv_summary(self):
        """Get summary of IMEISV mappings and their RNTIs."""
        summary = {}

        for imeisv in self.imeisv_persistent_ues:
            associated_rntis = [
                rnti_str for rnti_str in self.active_ues
                if self._get_imeisv_for_rnti(int(rnti_str)) == imeisv
            ]

            summary[str(imeisv)] = {
                "associated_rntis": associated_rntis,
                "rnti_count": len(associated_rntis),
                "has_active_rntis": len(associated_rntis) > 0
            }

        return summary