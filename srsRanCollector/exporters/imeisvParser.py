from typing import Dict, Any, Optional, Set
from datetime import datetime, timedelta
from collections import defaultdict
from influxdb_client import Point
from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time
from exporters.exporter import exporter


class imeisvParser:
    """
    Parser for IMEISV-to-RNTI mapping messages.

    Maintains bidirectional mappings between IMEISV and RNTI values,
    tracks UE handovers, and provides mapping history for analytics.

    MODIFIED: active_imeisvs now only removed when no RNTIs associated (no timeout)
    """

    def __init__(self, main_exporter: exporter, mapping_timeout_seconds: int = 300,
                 imeisv_persistent_mode: bool = True):
        self.exporter = main_exporter

        # Core mapping storage
        self.imeisv_to_rnti: Dict[int, int] = {}  # imeisv -> current_rnti
        self.rnti_to_imeisv: Dict[int, int] = {}  # rnti -> imeisv

        # Historical tracking
        self.imeisv_rnti_history: Dict[int, list] = defaultdict(list)  # imeisv -> [(rnti, timestamp), ...]
        self.last_mapping_update: Dict[int, datetime] = {}  # imeisv -> last_update_time

        # Statistics
        self.message_count = 0
        self.mapping_updates = 0
        self.handover_detected = 0
        self.new_ue_detected = 0
        self.mapping_timeouts = 0
        self.parse_error_count = 0
        self.imeisv_removed_no_rnti = 0  # NEW: track removals due to no RNTIs

        # Configuration
        self.mapping_timeout_seconds = mapping_timeout_seconds
        self.imeisv_persistent_mode = imeisv_persistent_mode  # NEW: enable persistent mode

        # Active tracking
        self.active_imeisvs: Set[int] = set()

    def update_mapping(self, imeisv: int, new_rnti: int, timestamp_dt: Optional[datetime] = None) -> bool:
        """
        Update IMEISV-to-RNTI mapping and detect handovers.

        Returns:
            bool: True if this was a handover (RNTI change), False if new mapping
        """
        if timestamp_dt is None:
            timestamp_dt = datetime.utcnow()

        is_handover = False
        old_rnti = None

        # Check if this IMEISV already has a mapping
        if imeisv in self.imeisv_to_rnti:
            old_rnti = self.imeisv_to_rnti[imeisv]

            # If RNTI changed, this is a handover
            if old_rnti != new_rnti:
                is_handover = True
                self.handover_detected += 1

                # Clean up old reverse mapping
                if old_rnti in self.rnti_to_imeisv:
                    del self.rnti_to_imeisv[old_rnti]

                log_both(f"Handover detected: IMEISV {imeisv} changed from RNTI {old_rnti} to {new_rnti}")
            else:
                # Same mapping, just update timestamp
                self.last_mapping_update[imeisv] = timestamp_dt
                return False
        else:
            # New UE
            self.new_ue_detected += 1
            self.active_imeisvs.add(imeisv)
            log_both(f"New UE detected: IMEISV {imeisv} mapped to RNTI {new_rnti}")

        # Update mappings
        self.imeisv_to_rnti[imeisv] = new_rnti
        self.rnti_to_imeisv[new_rnti] = imeisv
        self.last_mapping_update[imeisv] = timestamp_dt

        # Ensure IMEISV is in active set (for handovers)
        self.active_imeisvs.add(imeisv)

        # Add to history
        self.imeisv_rnti_history[imeisv].append((new_rnti, timestamp_dt))

        # Keep only last 10 mappings per IMEISV to prevent unbounded growth
        if len(self.imeisv_rnti_history[imeisv]) > 10:
            self.imeisv_rnti_history[imeisv] = self.imeisv_rnti_history[imeisv][-10:]

        self.mapping_updates += 1

        # Write mapping event to InfluxDB
        self._write_mapping_event(imeisv, new_rnti, old_rnti, is_handover, timestamp_dt)

        return is_handover

    def remove_rnti_mapping(self, rnti: int, reason: str = "explicit_removal") -> Optional[int]:
        """
        NEW METHOD: Remove a specific RNTI mapping and check if IMEISV should be removed.

        Args:
            rnti: The RNTI to remove
            reason: Reason for removal (for logging)

        Returns:
            The IMEISV that was associated with this RNTI, or None if not found
        """
        if rnti not in self.rnti_to_imeisv:
            return None

        imeisv = self.rnti_to_imeisv[rnti]

        # Remove the RNTI mapping
        del self.rnti_to_imeisv[rnti]

        # If this was the current RNTI for this IMEISV, remove that mapping too
        if imeisv in self.imeisv_to_rnti and self.imeisv_to_rnti[imeisv] == rnti:
            del self.imeisv_to_rnti[imeisv]

        # In persistent mode, only remove IMEISV if no other RNTIs map to it
        if self.imeisv_persistent_mode:
            # Check if any other RNTIs still map to this IMEISV
            other_rntis = [r for r, i in self.rnti_to_imeisv.items() if i == imeisv]

            if not other_rntis:
                # No other RNTIs for this IMEISV, safe to remove
                self.active_imeisvs.discard(imeisv)
                if imeisv in self.last_mapping_update:
                    del self.last_mapping_update[imeisv]
                self.imeisv_removed_no_rnti += 1

                log_both(f"IMEISV {imeisv} removed from active set - no associated RNTIs (reason: {reason})")

                # Write removal event to InfluxDB
                self._write_imeisv_removal_event(imeisv, reason)
            else:
                log_both(f"IMEISV {imeisv} still has {len(other_rntis)} other RNTIs after removing RNTI {rnti}")
        else:
            # Non-persistent mode: remove IMEISV immediately
            self.active_imeisvs.discard(imeisv)
            if imeisv in self.last_mapping_update:
                del self.last_mapping_update[imeisv]

        return imeisv

    def _write_imeisv_removal_event(self, imeisv: int, reason: str):
        """Write IMEISV removal event to InfluxDB."""
        point = (Point("imeisv_mapping")
                 .field("removal_event", 1)
                 .tag("imeisv", str(imeisv))
                 .tag("event_type", "imeisv_removed")
                 .tag("reason", reason)
                 .tag("component", "imeisv_mapper")
                 .time(datetime.utcnow()))

        self.exporter.write_to_influx([point])

    def check_mapping_timeouts(self, current_time: Optional[datetime] = None) -> int:
        """
        MODIFIED: Remove stale mappings, but respect persistent mode for active_imeisvs.

        In persistent mode: Only removes RNTI mappings, not IMEISVs unless no RNTIs remain.
        """
        if current_time is None:
            current_time = datetime.utcnow()

        timeout_threshold = timedelta(seconds=self.mapping_timeout_seconds)
        timed_out_imeisvs = []

        # Find timed out mappings
        for imeisv, last_update in self.last_mapping_update.items():
            if current_time - last_update > timeout_threshold:
                timed_out_imeisvs.append(imeisv)

        # Remove timed out mappings
        removed_count = 0
        for imeisv in timed_out_imeisvs:
            old_rnti = self.imeisv_to_rnti.get(imeisv)

            if old_rnti:
                # Remove this specific RNTI mapping
                removed_imeisv = self.remove_rnti_mapping(old_rnti, "timeout")
                if removed_imeisv:
                    removed_count += 1
                    self.mapping_timeouts += 1
                    log_both(
                        f"Mapping timeout: IMEISV {imeisv} (RNTI {old_rnti}) removed after {self.mapping_timeout_seconds}s")

        return removed_count

    def _write_mapping_event(self, imeisv: int, new_rnti: int, old_rnti: Optional[int],
                             is_handover: bool, timestamp_dt: datetime):
        """Write mapping event to InfluxDB."""
        influx_points = []

        event_type = "handover" if is_handover else "new_mapping"

        # Main mapping event
        point = (Point("imeisv_mapping")
                 .field("_measurement", "imeisv_event")
                 .field("rnti", new_rnti)
                 .tag("imeisv", str(imeisv))
                 .tag("event_type", event_type)
                 .tag("component", "imeisv_mapper")
                 .time(timestamp_dt))

        if old_rnti is not None:
            point = point.tag("old_rnti", str(old_rnti))

        influx_points.append(point)

        # Statistics
        stats_points = [
            Point("imeisv_stats").field("_measurement", "imeisv_event").field("total_mappings",
                                                                              self.mapping_updates).tag("component",
                                                                                                        "imeisv_mapper").time(
                timestamp_dt),
            Point("imeisv_stats").field("_measurement", "imeisv_event").field("total_handovers",
                                                                              self.handover_detected).tag("component",
                                                                                                          "imeisv_mapper").time(
                timestamp_dt),
            Point("imeisv_stats").field("_measurement", "imeisv_event").field("total_new_ues",
                                                                              self.new_ue_detected).tag("component",
                                                                                                        "imeisv_mapper").time(
                timestamp_dt),
            Point("imeisv_stats").field("_measurement", "imeisv_event").field("active_imeisvs",
                                                                              len(self.active_imeisvs)).tag("component",
                                                                                                            "imeisv_mapper").time(
                timestamp_dt),
            Point("imeisv_stats").field("_measurement", "imeisv_event").field("imeisv_removed_no_rnti",
                                                                              self.imeisv_removed_no_rnti).tag(
                "component", "imeisv_mapper").time(timestamp_dt)
        ]

        influx_points.extend(stats_points)
        self.exporter.write_to_influx(influx_points)

    def get_rnti_for_imeisv(self, imeisv: int) -> Optional[int]:
        """Get current RNTI for given IMEISV."""
        return self.imeisv_to_rnti.get(imeisv)

    def get_imeisv_for_rnti(self, rnti: int) -> Optional[int]:
        """Get IMEISV for given RNTI."""
        return self.rnti_to_imeisv.get(rnti)

    def get_mapping_age(self, imeisv: int) -> Optional[float]:
        """Get age of mapping in seconds."""
        if imeisv not in self.last_mapping_update:
            return None

        return (datetime.utcnow() - self.last_mapping_update[imeisv]).total_seconds()

    def get_active_rntis_for_imeisv(self, imeisv: int) -> list:
        """NEW METHOD: Get all RNTIs currently associated with an IMEISV."""
        return [rnti for rnti, mapped_imeisv in self.rnti_to_imeisv.items() if mapped_imeisv == imeisv]

    def update_metrics(self, entry: Dict[str, Any]):
        """Main entry point for processing IMEISV mapping messages."""
        self.message_count += 1

        try:
            # Extract timestamp
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)
            current_time = timestamp_dt or datetime.utcnow()

            # Check for timeouts (now respects persistent mode)
            timeout_count = self.check_mapping_timeouts(current_time)
            if timeout_count > 0:
                log_both(f"Processed {timeout_count} timed-out IMEISV mappings")

            # Extract required fields
            imeisv = entry.get("imeisv")
            rnti = entry.get("rnti")

            if imeisv is None or rnti is None:
                log_both(f"Missing required fields in IMEISV mapping: imeisv={imeisv}, rnti={rnti}", "error")
                self.parse_error_count += 1
                return

            # Convert to integers if they're not already
            try:
                imeisv = int(imeisv)
                rnti = int(rnti)
            except (ValueError, TypeError) as e:
                log_both(f"Invalid IMEISV or RNTI values: imeisv={imeisv}, rnti={rnti}, error={e}", "error")
                self.parse_error_count += 1
                return

            # Update mapping
            is_handover = self.update_mapping(imeisv, rnti, timestamp_dt)

            # Log additional metrics if present (PCI, measurement data, etc.)
            self._log_additional_metrics(entry, imeisv, rnti, timestamp_dt)

        except Exception as e:
            log_both(f"Error processing IMEISV mapping message: {e}", "error")
            self.parse_error_count += 1

    def _log_additional_metrics(self, entry: Dict[str, Any], imeisv: int, rnti: int,
                                timestamp_dt: Optional[datetime]):
        """Log additional measurement data from the IMEISV message."""
        influx_points = []

        try:
            # Log PCI if present
            pci = entry.get("pci")
            if pci is not None:
                point = (Point("ue_measurements")
                         .field("pci", safe_numeric(pci, "pci"))
                         .tag("imeisv", str(imeisv))
                         .tag("rnti", str(rnti))
                         .tag("component", "imeisv_mapper"))

                if timestamp_dt:
                    point = point.time(timestamp_dt)
                influx_points.append(point)

            # Log serving cell measurements
            serving_mo_list = entry.get("serving_mo_list", [])
            for serving_cell in serving_mo_list:
                serving_data = serving_cell.get("serving_cell", {})
                ssb_cell = serving_data.get("ssb_cell", {})

                for metric in ["rsrp", "rsrq", "sinr"]:
                    value = safe_numeric(ssb_cell.get(metric), metric)
                    if value is not None:
                        point = (Point("ue_measurements")
                                 .field(f"serving_{metric}", value)
                                 .tag("imeisv", str(imeisv))
                                 .tag("rnti", str(rnti))
                                 .tag("measurement_type", "serving")
                                 .tag("component", "imeisv_mapper"))

                        if timestamp_dt:
                            point = point.time(timestamp_dt)
                        influx_points.append(point)

            # Log neighbor cell measurements
            neighbor_cells = entry.get("neighbor_cells", [])
            for neighbor in neighbor_cells:
                neighbor_pci = neighbor.get("pci")
                neighbor_ssb = neighbor.get("ssb_cell", {})

                for metric in ["rsrp", "rsrq", "sinr"]:
                    value = safe_numeric(neighbor_ssb.get(metric), metric)
                    if value is not None:
                        point = (Point("ue_measurements")
                                 .field(f"neighbor_{metric}", value)
                                 .tag("imeisv", str(imeisv))
                                 .tag("rnti", str(rnti))
                                 .tag("neighbor_pci", str(neighbor_pci) if neighbor_pci else "unknown")
                                 .tag("measurement_type", "neighbor")
                                 .tag("component", "imeisv_mapper"))

                        if timestamp_dt:
                            point = point.time(timestamp_dt)
                        influx_points.append(point)

            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error logging additional IMEISV metrics: {e}", "warning")

    def get_handover_history(self, imeisv: int) -> list:
        """Get handover history for a specific IMEISV."""
        return self.imeisv_rnti_history.get(imeisv, [])

    def get_stats(self) -> Dict[str, Any]:
        """Get parser statistics."""
        return {
            "message_count": self.message_count,
            "mapping_updates": self.mapping_updates,
            "handover_detected": self.handover_detected,
            "new_ue_detected": self.new_ue_detected,
            "mapping_timeouts": self.mapping_timeouts,
            "imeisv_removed_no_rnti": self.imeisv_removed_no_rnti,  # NEW
            "parse_error_count": self.parse_error_count,
            "active_mappings": len(self.imeisv_to_rnti),
            "active_imeisvs": len(self.active_imeisvs),
            "mapping_timeout_seconds": self.mapping_timeout_seconds,
            "imeisv_persistent_mode": self.imeisv_persistent_mode  # NEW
        }

    def get_all_mappings(self) -> Dict[str, Dict[str, Any]]:
        """Get all current mappings with metadata."""
        result = {}
        current_time = datetime.utcnow()

        for imeisv, rnti in self.imeisv_to_rnti.items():
            last_update = self.last_mapping_update.get(imeisv)
            age_seconds = None
            if last_update:
                age_seconds = (current_time - last_update).total_seconds()

            # Get all RNTIs for this IMEISV
            all_rntis = self.get_active_rntis_for_imeisv(imeisv)

            result[str(imeisv)] = {
                "current_rnti": rnti,
                "all_rntis": all_rntis,
                "rnti_count": len(all_rntis),
                "last_update": last_update.isoformat() if last_update else None,
                "age_seconds": age_seconds,
                "handover_count": len(self.imeisv_rnti_history.get(imeisv, [])),
                "is_stale": age_seconds > self.mapping_timeout_seconds if age_seconds else False,
                "is_active": imeisv in self.active_imeisvs
            }

        return result