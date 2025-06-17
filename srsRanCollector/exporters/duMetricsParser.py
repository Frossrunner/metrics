from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Optional
from influxdb_client import Point

from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time


class duMetricsParser:
    def __init__(self, main_exporter):
        # make an exporter
        self.exporter = main_exporter
        # Counters and tracking
        self.message_count = 0
        self.parse_error_count = 0
        self.active_cells = set()  # Track which PCIs are currently active

        # Expected field sets for validation - DU HIGH
        self.EXPECTED_DU_HIGH_CELL_FIELDS = {
            'pci', 'average_latency_us', 'min_latency_us', 'max_latency_us', 'cpu_usage_percent'
        }
        self.EXPECTED_MAC_DL_FIELDS = {'cell'}
        self.EXPECTED_MAC_FIELDS = {'dl'}
        self.EXPECTED_DU_HIGH_FIELDS = {'mac'}

        # Expected field sets for validation - DU LOW
        self.EXPECTED_DU_LOW_CELL_FIELDS = {'pci', 'dl', 'ul'}
        self.EXPECTED_UPPER_PHY_FIELDS = {'cell'}
        self.EXPECTED_DU_LOW_FIELDS = {'upper_phy'}

        # DL/UL component fields
        self.EXPECTED_DL_FIELDS = {
            'average_latency_us', 'max_latency_us', 'max_latency_slot', 'average_throughput_Mbps',
            'cpu_usage_percent', 'ldpc_encoder', 'ldpc_rate_matcher', 'scrambling',
            'modulation_mapper', 'precoding_layer_mapping', 'fec'
        }
        self.EXPECTED_UL_FIELDS = {
            'average_latency_us', 'max_latency_us', 'max_latency_slot', 'average_throughput_Mbps',
            'cpu_usage_percent', 'ldpc_decoder', 'ldpc_rate_dematcher', 'descrambling',
            'demodulation_mapper', 'channel_estimation', 'transform_precoder', 'fec', 'algo_efficiency'
        }

        # Component-specific fields
        self.EXPECTED_LDPC_ENCODER_FIELDS = {
            'average_cb_size_bits', 'average_latency_us', 'min_latency_us', 'max_latency_us',
            'average_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_LDPC_RATE_MATCHER_FIELDS = {
            'average_latency_us', 'min_latency_us', 'max_latency_us', 'average_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_SCRAMBLING_FIELDS = {'cpu_usage_percent'}
        self.EXPECTED_MODULATION_MAPPER_FIELDS = {
            'qpsk_mod_throughput_Mbps', 'qam16_mod_throughput_Mbps', 'qam64_mod_throughput_Mbps',
            'qam256_mod_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_PRECODING_LAYER_MAPPING_FIELDS = {
            'average_latency_us', 'throughput_per_nof_layers_MREsps', 'cpu_usage_percent'
        }
        self.EXPECTED_FEC_FIELDS = {'average_throughput_Mbps', 'cpu_usage_percent'}

        # UL-specific component fields
        self.EXPECTED_LDPC_DECODER_FIELDS = {
            'average_cb_size_bits', 'average_latency_us', 'min_latency_us', 'max_latency_us',
            'average_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_LDPC_RATE_DEMATCHER_FIELDS = {
            'average_latency_us', 'min_latency_us', 'max_latency_us', 'average_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_DESCRAMBLING_FIELDS = {'cpu_usage_percent'}
        self.EXPECTED_DEMODULATION_MAPPER_FIELDS = {
            'qpsk_mod_throughput_Mbps', 'qam16_mod_throughput_Mbps', 'qam64_mod_throughput_Mbps',
            'qam256_mod_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_CHANNEL_ESTIMATION_FIELDS = {
            'average_latency_us', 'min_latency_us', 'max_latency_us', 'average_throughput_Mbps', 'cpu_usage_percent'
        }
        self.EXPECTED_TRANSFORM_PRECODER_FIELDS = {
            'average_latency_us', 'average_throughput_MREps', 'cpu_usage_percent'
        }
        self.EXPECTED_ALGO_EFFICIENCY_FIELDS = {'bler', 'evm', 'sinr_db'}

        self.EXPECTED_DU_FIELDS = {'du_high', 'du_low'}
        self.EXPECTED_TOP_FIELDS = {'timestamp', 'du'}

    def update_component_metrics(self, component_data: Dict[str, Any], component_name: str,
                                 pci_str: str, direction: str, timestamp_dt: Optional[datetime] = None):
        """Update metrics for a specific processing component."""
        influx_points = []

        try:
            # Get expected fields for this component
            expected_fields_map = {
                'ldpc_encoder': self.EXPECTED_LDPC_ENCODER_FIELDS,
                'ldpc_rate_matcher': self.EXPECTED_LDPC_RATE_MATCHER_FIELDS,
                'scrambling': self.EXPECTED_SCRAMBLING_FIELDS,
                'modulation_mapper': self.EXPECTED_MODULATION_MAPPER_FIELDS,
                'precoding_layer_mapping': self.EXPECTED_PRECODING_LAYER_MAPPING_FIELDS,
                'fec': self.EXPECTED_FEC_FIELDS,
                'ldpc_decoder': self.EXPECTED_LDPC_DECODER_FIELDS,
                'ldpc_rate_dematcher': self.EXPECTED_LDPC_RATE_DEMATCHER_FIELDS,
                'descrambling': self.EXPECTED_DESCRAMBLING_FIELDS,
                'demodulation_mapper': self.EXPECTED_DEMODULATION_MAPPER_FIELDS,
                'channel_estimation': self.EXPECTED_CHANNEL_ESTIMATION_FIELDS,
                'transform_precoder': self.EXPECTED_TRANSFORM_PRECODER_FIELDS,
                'algo_efficiency': self.EXPECTED_ALGO_EFFICIENCY_FIELDS
            }

            expected_fields = expected_fields_map.get(component_name, set())

            # Process regular fields
            for field in expected_fields:
                if field == 'throughput_per_nof_layers_MREsps':
                    # Handle array field specially
                    array_value = component_data.get(field)
                    if isinstance(array_value, list):
                        for i, val in enumerate(array_value):
                            safe_val = safe_numeric(val, f"{field}[{i}]")
                            if safe_val is not None:
                                point = Point("du_component_metrics") \
                                    .field(f"{field}_layer_{i}", safe_val) \
                                    .tag("pci", pci_str) \
                                    .tag("direction", direction) \
                                    .tag("component", component_name) \
                                    .tag("source", "srs_du")
                                if timestamp_dt:
                                    point = point.time(timestamp_dt)
                                influx_points.append(point)
                    continue

                value = safe_numeric(component_data.get(field), field)
                if value is not None:
                    point = Point("du_component_metrics") \
                        .field(field, value) \
                        .tag("pci", pci_str) \
                        .tag("direction", direction) \
                        .tag("component", component_name) \
                        .tag("source", "srs_du")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Check for unexpected fields
            unexpected_fields = set(component_data.keys()) - expected_fields
            if unexpected_fields:
                log_both(f"Unexpected {component_name} fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Write component metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating {component_name} metrics for PCI {pci_str}: {e}", "error")

    def update_direction_metrics(self, direction_data: Dict[str, Any], direction: str,
                                 pci_str: str, timestamp_dt: Optional[datetime] = None):
        """Update metrics for DL or UL direction."""
        influx_points = []

        try:
            expected_fields = self.EXPECTED_DL_FIELDS if direction == 'dl' else self.EXPECTED_UL_FIELDS

            # Process top-level direction metrics
            for field in ['average_latency_us', 'max_latency_us', 'max_latency_slot',
                          'average_throughput_Mbps', 'cpu_usage_percent']:
                value = safe_numeric(direction_data.get(field), field)
                if value is not None:
                    point = Point("du_direction_metrics") \
                        .field(field, value) \
                        .tag("pci", pci_str) \
                        .tag("direction", direction) \
                        .tag("source", "srs_du")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Write direction metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

            # Process component metrics
            component_fields = expected_fields - {'average_latency_us', 'max_latency_us', 'max_latency_slot',
                                                  'average_throughput_Mbps', 'cpu_usage_percent'}

            for component_name in component_fields:
                component_data = direction_data.get(component_name, {})
                if component_data:
                    self.update_component_metrics(component_data, component_name, pci_str, direction, timestamp_dt)

            # Check for unexpected fields
            unexpected_fields = set(direction_data.keys()) - expected_fields
            if unexpected_fields:
                log_both(f"Unexpected {direction} fields for PCI {pci_str}: {unexpected_fields}", "warning")

        except Exception as e:
            log_both(f"Error updating {direction} metrics for PCI {pci_str}: {e}", "error")

    def update_du_low_cell_metrics(self, cell_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update DU low cell-level metrics."""
        try:
            pci = cell_data.get('pci')
            if pci is None:
                log_both("DU low cell data missing PCI, skipping metrics update", "warning")
                return

            pci_str = str(pci)

            # Add PCI to active cells
            if pci_str not in self.active_cells:
                self.active_cells.add(pci_str)
                log_both(f"New cell discovered in DU low: PCI {pci_str}")

            # Check for unexpected fields
            unexpected_fields = set(cell_data.keys()) - self.EXPECTED_DU_LOW_CELL_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected DU low cell fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Process DL metrics
            dl_data = cell_data.get('dl', {})
            if dl_data:
                self.update_direction_metrics(dl_data, 'dl', pci_str, timestamp_dt)

            # Process UL metrics
            ul_data = cell_data.get('ul', {})
            if ul_data:
                self.update_direction_metrics(ul_data, 'ul', pci_str, timestamp_dt)

        except Exception as e:
            log_both(f"Error updating DU low cell metrics: {e}", "error")

    def update_du_high_cell_metrics(self, cell_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update DU high cell-level metrics."""
        influx_points = []

        try:
            pci = cell_data.get('pci')
            if pci is None:
                log_both("DU high cell data missing PCI, skipping metrics update", "warning")
                return

            pci_str = str(pci)

            # Add PCI to active cells
            if pci_str not in self.active_cells:
                self.active_cells.add(pci_str)
                log_both(f"New cell discovered in DU high: PCI {pci_str}")

            # Handle all numeric cell metrics
            for field in self.EXPECTED_DU_HIGH_CELL_FIELDS:
                if field == 'pci':  # Skip PCI as it's used as a tag
                    continue

                value = safe_numeric(cell_data.get(field), field)
                if value is not None:
                    point = Point("du_high_cell_metrics").field(field, value).tag("pci", pci_str).tag("source",
                                                                                                      "srs_du")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Check for unexpected fields
            unexpected_fields = set(cell_data.keys()) - self.EXPECTED_DU_HIGH_CELL_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected DU high cell fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Write all cell metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating DU high cell metrics: {e}", "error")

    def update_upper_phy_metrics(self, upper_phy_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Update upper PHY metrics for all cells."""
        try:
            # Track which PCIs we received data for in this update
            received_pcis = set()

            for phy_entry in upper_phy_list:
                # Validate upper PHY entry structure
                unexpected_phy_fields = set(phy_entry.keys()) - self.EXPECTED_UPPER_PHY_FIELDS
                if unexpected_phy_fields:
                    log_both(f"Unexpected upper PHY fields: {unexpected_phy_fields}", "warning")

                cell_data = phy_entry.get('cell', {})
                if not cell_data:
                    log_both("Upper PHY entry missing cell data", "warning")
                    continue

                pci = cell_data.get('pci')
                if pci is not None:
                    received_pcis.add(str(pci))

                # Update DU low cell metrics
                self.update_du_low_cell_metrics(cell_data, timestamp_dt)

            # Log missing data cells
            missing_data_pcis = self.active_cells - received_pcis
            if missing_data_pcis:
                log_both(f"Missing data for active cells in DU low: {missing_data_pcis}")

            log_both(
                f"Upper PHY metrics update complete. Active cells: {len(self.active_cells)}, "
                f"Received data: {len(received_pcis)}, Missing data: {len(missing_data_pcis)}"
            )

        except Exception as e:
            log_both(f"Error updating upper PHY metrics: {e}", "error")

    def update_mac_dl_metrics(self, dl_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Update MAC downlink metrics for all cells."""
        try:
            # Track which PCIs we received data for in this update
            received_pcis = set()

            for dl_entry in dl_list:
                # Validate DL entry structure
                unexpected_dl_fields = set(dl_entry.keys()) - self.EXPECTED_MAC_DL_FIELDS
                if unexpected_dl_fields:
                    log_both(f"Unexpected MAC DL fields: {unexpected_dl_fields}", "warning")

                cell_data = dl_entry.get('cell', {})
                if not cell_data:
                    log_both("MAC DL entry missing cell data", "warning")
                    continue

                pci = cell_data.get('pci')
                if pci is not None:
                    received_pcis.add(str(pci))

                # Update DU high cell metrics
                self.update_du_high_cell_metrics(cell_data, timestamp_dt)

            # Log missing data cells
            missing_data_pcis = self.active_cells - received_pcis
            if missing_data_pcis:
                log_both(f"Missing data for active cells in DU high: {missing_data_pcis}")

            log_both(
                f"MAC DL metrics update complete. Active cells: {len(self.active_cells)}, "
                f"Received data: {len(received_pcis)}, Missing data: {len(missing_data_pcis)}"
            )

        except Exception as e:
            log_both(f"Error updating MAC DL metrics: {e}", "error")

    def update_mac_metrics(self, mac_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update MAC-level metrics."""
        try:
            # Validate MAC structure
            unexpected_mac_fields = set(mac_data.keys()) - self.EXPECTED_MAC_FIELDS
            if unexpected_mac_fields:
                log_both(f"Unexpected MAC fields: {unexpected_mac_fields}", "warning")

            # Process downlink data
            dl_list = mac_data.get('dl', [])
            if isinstance(dl_list, list) and dl_list:
                self.update_mac_dl_metrics(dl_list, timestamp_dt)
            elif dl_list:
                log_both("MAC DL data is not a list or is empty", "warning")

        except Exception as e:
            log_both(f"Error updating MAC metrics: {e}", "error")

    def update_du_high_metrics(self, du_high_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update DU high-level metrics."""
        try:
            # Validate DU high structure
            unexpected_du_high_fields = set(du_high_data.keys()) - self.EXPECTED_DU_HIGH_FIELDS
            if unexpected_du_high_fields:
                log_both(f"Unexpected DU high fields: {unexpected_du_high_fields}", "warning")
                log_both(f"unexpected data: {du_high_data}")
            # Process MAC data
            mac_data = du_high_data.get('mac', {})
            if mac_data:
                self.update_mac_metrics(mac_data, timestamp_dt)
            else:
                log_both("DU high missing MAC data", "warning")

        except Exception as e:
            log_both(f"Error updating DU high metrics: {e}", "error")

    def update_du_low_metrics(self, du_low_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update DU low-level metrics."""
        try:
            # Validate DU low structure
            unexpected_du_low_fields = set(du_low_data.keys()) - self.EXPECTED_DU_LOW_FIELDS
            if unexpected_du_low_fields:
                log_both(f"Unexpected DU low fields: {unexpected_du_low_fields}", "warning")
                log_both(f"unexpected data: {du_low_data}")

            # Process upper PHY data
            upper_phy_list = du_low_data.get('upper_phy', [])
            if isinstance(upper_phy_list, list) and upper_phy_list:
                self.update_upper_phy_metrics(upper_phy_list, timestamp_dt)
            elif upper_phy_list:
                log_both("DU low upper_phy data is not a list or is empty", "warning")

        except Exception as e:
            log_both(f"Error updating DU low metrics: {e}", "error")

    def update_du_metrics(self, du_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update DU-level metrics."""
        influx_points = []

        try:
            # Validate DU structure
            unexpected_du_fields = set(du_data.keys()) - self.EXPECTED_DU_FIELDS
            if unexpected_du_fields:
                log_both(f"Unexpected DU fields: {unexpected_du_fields}", "warning")
                log_both(f"unexpected data with unknown fields: {du_data}", "warning")

            # Write DU status metrics
            point = Point("du_metrics").field("active_cells_count", len(self.active_cells)).tag("source", "srs_du")
            if timestamp_dt:
                point = point.time(timestamp_dt)
            influx_points.append(point)

            # Write DU metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

            # Process DU high data
            try:
                du_high_data = du_data.get('du_high', {})
                if du_high_data:
                    self.update_du_high_metrics(du_high_data, timestamp_dt)
            except Exception as e:
                pass

            # Process DU low data
            try:
                du_low_data = du_data.get('du_low', {})
                if du_low_data:
                    self.update_du_low_metrics(du_low_data, timestamp_dt)
            except Exception as e:
                pass

            # Log if neither high nor low data is present
            if not du_high_data and not du_low_data:
                log_both("DU missing both du_high and du_low data", "warning")

        except Exception as e:
            log_both(f"Error updating DU metrics: {e}", "error")

    def update_metrics(self, entry: Dict[str, Any]):
        """Main metrics update function."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)

            # STEP 1: Update DU metrics
            du_data = entry.get("du", {})
            if du_data:
                self.update_du_metrics(du_data, timestamp_dt)
            else:
                log_both("Message missing DU data", "warning")

            # STEP 2: Update system metrics
            self.message_count += 1
            system_points = []

            if timestamp:
                timestamp_val = safe_numeric(timestamp, "timestamp")
                if timestamp_val is not None:
                    system_points.append(
                        Point("du_system_metrics").field("last_update_timestamp", timestamp_val).tag("source", "srs_du")
                    )

            system_points.append(
                Point("du_system_metrics").field("total_messages_received", self.message_count).tag("source", "srs_du")
            )

            system_points.append(
                Point("du_system_metrics").field("total_parse_errors", self.parse_error_count).tag("source", "srs_du")
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
