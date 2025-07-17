from collections import defaultdict
from datetime import datetime
from typing import Dict, Any, List, Optional
from influxdb_client import Point

from exporters.helper_functions import log_both, safe_numeric, timestamp_to_influx_time


class ruMetricsParser:
    def __init__(self, main_exporter):
        # make an exporter
        self.exporter = main_exporter
        # Counters and tracking
        self.message_count = 0
        self.parse_error_count = 0
        self.active_cells = set()  # Track which PCIs are currently active

        # Expected field sets for validation - RU OFH
        self.EXPECTED_RU_CELL_FIELDS = {'pci', 'ul', 'dl'}
        self.EXPECTED_OFH_FIELDS = {'cell'}
        self.EXPECTED_RU_FIELDS = {'ofh'}

        # UL/DL direction fields
        self.EXPECTED_UL_FIELDS = {'received_packets', 'ethernet_receiver', 'message_decoder'}
        self.EXPECTED_DL_FIELDS = {'ethernet_transmitter', 'message_encoder', 'transmitter_stats'}

        # UL component fields
        self.EXPECTED_RECEIVED_PACKETS_FIELDS = {'total', 'early', 'on_time', 'late'}
        self.EXPECTED_ETHERNET_RECEIVER_FIELDS = {
            'average_throughput_Mbps', 'average_latency_us', 'max_latency_us', 'cpu_usage_percent'
        }
        self.EXPECTED_MESSAGE_DECODER_FIELDS = {'prach', 'data'}
        self.EXPECTED_PRACH_FIELDS = {'average_latency_us', 'max_latency_us', 'cpu_usage_percent'}
        self.EXPECTED_DECODER_DATA_FIELDS = {'average_latency_us', 'max_latency_us', 'cpu_usage_percent'}

        # DL component fields
        self.EXPECTED_ETHERNET_TRANSMITTER_FIELDS = {
            'average_throughput_Mbps', 'average_latency_us', 'max_latency_us', 'cpu_usage_percent'
        }
        self.EXPECTED_MESSAGE_ENCODER_FIELDS = {'dl_cp', 'ul_cp', 'dl_up'}
        self.EXPECTED_DL_CP_FIELDS = {'average_latency_us', 'max_latency_us', 'cpu_usage_percent'}
        self.EXPECTED_UL_CP_FIELDS = {'average_latency_us', 'max_latency_us', 'cpu_usage_percent'}
        self.EXPECTED_DL_UP_FIELDS = {'average_latency_us', 'max_latency_us', 'cpu_usage_percent'}
        self.EXPECTED_TRANSMITTER_STATS_FIELDS = {'late_dl_grids', 'late_ul_requests'}

        self.EXPECTED_TOP_FIELDS = {'timestamp', 'ru'}

    def update_ul_received_packets_metrics(self, packets_data: Dict[str, Any], pci_str: str,
                                           timestamp_dt: Optional[datetime] = None):
        """Update UL received packets metrics."""
        influx_points = []

        try:
            # Process all packet statistics
            for field in self.EXPECTED_RECEIVED_PACKETS_FIELDS:
                value = safe_numeric(packets_data.get(field), field)
                if value is not None:
                    point = Point("ru_packet_stats") \
                        .field(f"received_packets_{field}", value) \
                        .tag("pci", pci_str) \
                        .tag("direction", "ul") \
                        .tag("component", "ru")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Calculate packet timing percentages if total > 0
            total_packets = safe_numeric(packets_data.get('total'), 'total')
            if total_packets and total_packets > 0:
                for timing_type in ['early', 'on_time', 'late']:
                    count = safe_numeric(packets_data.get(timing_type), timing_type)
                    if count is not None:
                        percentage = (count / total_packets) * 100
                        point = Point("ru_packet_stats") \
                            .field(f"received_packets_{timing_type}_percent", percentage) \
                            .tag("pci", pci_str) \
                            .tag("direction", "ul") \
                            .tag("component", "ru")
                        if timestamp_dt:
                            point = point.time(timestamp_dt)
                        influx_points.append(point)

            # Check for unexpected fields
            unexpected_fields = set(packets_data.keys()) - self.EXPECTED_RECEIVED_PACKETS_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected received_packets fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Write packet metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating received packets metrics for PCI {pci_str}: {e}", "error")

    def update_ethernet_component_metrics(self, component_data: Dict[str, Any], component_name: str,
                                          pci_str: str, direction: str, timestamp_dt: Optional[datetime] = None):
        """Update ethernet receiver/transmitter metrics."""
        influx_points = []

        try:
            expected_fields = (self.EXPECTED_ETHERNET_RECEIVER_FIELDS if component_name == 'ethernet_receiver'
                               else self.EXPECTED_ETHERNET_TRANSMITTER_FIELDS)

            # Process all ethernet component metrics
            for field in expected_fields:
                value = safe_numeric(component_data.get(field), field)
                if value is not None:
                    point = Point("ru_ethernet_metrics") \
                        .field(field, value) \
                        .tag("pci", pci_str) \
                        .tag("direction", direction) \
                        .tag("component", component_name) \
                        .tag("component", "ru")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Check for unexpected fields
            unexpected_fields = set(component_data.keys()) - expected_fields
            if unexpected_fields:
                log_both(f"Unexpected {component_name} fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Write ethernet metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating {component_name} metrics for PCI {pci_str}: {e}", "error")

    def update_message_processing_metrics(self, processing_data: Dict[str, Any], processing_type: str,
                                          component_name: str, pci_str: str, direction: str,
                                          timestamp_dt: Optional[datetime] = None):
        """Update message decoder/encoder sub-component metrics."""
        influx_points = []

        try:
            # Get expected fields based on processing type
            expected_fields_map = {
                'prach': self.EXPECTED_PRACH_FIELDS,
                'data': self.EXPECTED_DECODER_DATA_FIELDS,
                'dl_cp': self.EXPECTED_DL_CP_FIELDS,
                'ul_cp': self.EXPECTED_UL_CP_FIELDS,
                'dl_up': self.EXPECTED_DL_UP_FIELDS
            }

            expected_fields = expected_fields_map.get(processing_type, set())

            # Process all processing metrics
            for field in expected_fields:
                value = safe_numeric(processing_data.get(field), field)
                if value is not None:
                    point = Point("ru_message_processing") \
                        .field(field, value) \
                        .tag("pci", pci_str) \
                        .tag("direction", direction) \
                        .tag("component", component_name) \
                        .tag("processing_type", processing_type) \
                        .tag("component", "ru")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Check for unexpected fields
            unexpected_fields = set(processing_data.keys()) - expected_fields
            if unexpected_fields:
                log_both(f"Unexpected {processing_type} fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Write processing metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating {processing_type} metrics for PCI {pci_str}: {e}", "error")

    def update_transmitter_stats_metrics(self, stats_data: Dict[str, Any], pci_str: str,
                                         timestamp_dt: Optional[datetime] = None):
        """Update DL transmitter statistics metrics."""
        influx_points = []

        try:
            # Process all transmitter statistics
            for field in self.EXPECTED_TRANSMITTER_STATS_FIELDS:
                value = safe_numeric(stats_data.get(field), field)
                if value is not None:
                    point = Point("ru_transmitter_stats") \
                        .field(field, value) \
                        .tag("pci", pci_str) \
                        .tag("direction", "dl") \
                        .tag("component", "ru")
                    if timestamp_dt:
                        point = point.time(timestamp_dt)
                    influx_points.append(point)

            # Check for unexpected fields
            unexpected_fields = set(stats_data.keys()) - self.EXPECTED_TRANSMITTER_STATS_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected transmitter_stats fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Write transmitter stats to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

        except Exception as e:
            log_both(f"Error updating transmitter stats metrics for PCI {pci_str}: {e}", "error")

    def update_ul_metrics(self, ul_data: Dict[str, Any], pci_str: str, timestamp_dt: Optional[datetime] = None):
        """Update UL direction metrics."""
        try:
            # Check for unexpected UL fields
            unexpected_fields = set(ul_data.keys()) - self.EXPECTED_UL_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected UL fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Process received packets
            received_packets = ul_data.get('received_packets', {})
            if received_packets:
                self.update_ul_received_packets_metrics(received_packets, pci_str, timestamp_dt)

            # Process ethernet receiver
            ethernet_receiver = ul_data.get('ethernet_receiver', {})
            if ethernet_receiver:
                self.update_ethernet_component_metrics(ethernet_receiver, 'ethernet_receiver',
                                                       pci_str, 'ul', timestamp_dt)

            # Process message decoder
            message_decoder = ul_data.get('message_decoder', {})
            if message_decoder:
                # Validate message decoder structure
                unexpected_decoder_fields = set(message_decoder.keys()) - self.EXPECTED_MESSAGE_DECODER_FIELDS
                if unexpected_decoder_fields:
                    log_both(f"Unexpected message_decoder fields for PCI {pci_str}: {unexpected_decoder_fields}",
                             "warning")

                # Process PRACH decoder
                prach_data = message_decoder.get('prach', {})
                if prach_data:
                    self.update_message_processing_metrics(prach_data, 'prach', 'message_decoder',
                                                           pci_str, 'ul', timestamp_dt)

                # Process data decoder
                data_decoder = message_decoder.get('data', {})
                if data_decoder:
                    self.update_message_processing_metrics(data_decoder, 'data', 'message_decoder',
                                                           pci_str, 'ul', timestamp_dt)

        except Exception as e:
            log_both(f"Error updating UL metrics for PCI {pci_str}: {e}", "error")

    def update_dl_metrics(self, dl_data: Dict[str, Any], pci_str: str, timestamp_dt: Optional[datetime] = None):
        """Update DL direction metrics."""
        try:
            # Check for unexpected DL fields
            unexpected_fields = set(dl_data.keys()) - self.EXPECTED_DL_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected DL fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Process ethernet transmitter
            ethernet_transmitter = dl_data.get('ethernet_transmitter', {})
            if ethernet_transmitter:
                self.update_ethernet_component_metrics(ethernet_transmitter, 'ethernet_transmitter',
                                                       pci_str, 'dl', timestamp_dt)

            # Process message encoder
            message_encoder = dl_data.get('message_encoder', {})
            if message_encoder:
                # Validate message encoder structure
                unexpected_encoder_fields = set(message_encoder.keys()) - self.EXPECTED_MESSAGE_ENCODER_FIELDS
                if unexpected_encoder_fields:
                    log_both(f"Unexpected message_encoder fields for PCI {pci_str}: {unexpected_encoder_fields}",
                             "warning")

                # Process DL control plane
                dl_cp_data = message_encoder.get('dl_cp', {})
                if dl_cp_data:
                    self.update_message_processing_metrics(dl_cp_data, 'dl_cp', 'message_encoder',
                                                           pci_str, 'dl', timestamp_dt)

                # Process UL control plane
                ul_cp_data = message_encoder.get('ul_cp', {})
                if ul_cp_data:
                    self.update_message_processing_metrics(ul_cp_data, 'ul_cp', 'message_encoder',
                                                           pci_str, 'dl', timestamp_dt)

                # Process DL user plane
                dl_up_data = message_encoder.get('dl_up', {})
                if dl_up_data:
                    self.update_message_processing_metrics(dl_up_data, 'dl_up', 'message_encoder',
                                                           pci_str, 'dl', timestamp_dt)

            # Process transmitter statistics
            transmitter_stats = dl_data.get('transmitter_stats', {})
            if transmitter_stats:
                self.update_transmitter_stats_metrics(transmitter_stats, pci_str, timestamp_dt)

        except Exception as e:
            log_both(f"Error updating DL metrics for PCI {pci_str}: {e}", "error")

    def update_ru_cell_metrics(self, cell_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update RU cell-level metrics."""
        try:
            pci = cell_data.get('pci')
            if pci is None:
                log_both("RU cell data missing PCI, skipping metrics update", "warning")
                return

            pci_str = str(pci)

            # Add PCI to active cells
            if pci_str not in self.active_cells:
                self.active_cells.add(pci_str)
                log_both(f"New cell discovered in RU: PCI {pci_str}")

            # Check for unexpected cell fields
            unexpected_fields = set(cell_data.keys()) - self.EXPECTED_RU_CELL_FIELDS
            if unexpected_fields:
                log_both(f"Unexpected RU cell fields for PCI {pci_str}: {unexpected_fields}", "warning")

            # Process UL metrics
            ul_data = cell_data.get('ul', {})
            if ul_data:
                self.update_ul_metrics(ul_data, pci_str, timestamp_dt)

            # Process DL metrics
            dl_data = cell_data.get('dl', {})
            if dl_data:
                self.update_dl_metrics(dl_data, pci_str, timestamp_dt)

        except Exception as e:
            log_both(f"Error updating RU cell metrics: {e}", "error")

    def update_ofh_metrics(self, ofh_list: List[Dict[str, Any]], timestamp_dt: Optional[datetime] = None):
        """Update OFH (Open Fronthaul) metrics for all cells."""
        try:
            # Track which PCIs we received data for in this update
            received_pcis = set()

            for ofh_entry in ofh_list:
                # Validate OFH entry structure
                unexpected_ofh_fields = set(ofh_entry.keys()) - self.EXPECTED_OFH_FIELDS
                if unexpected_ofh_fields:
                    log_both(f"Unexpected OFH fields: {unexpected_ofh_fields}", "warning")

                cell_data = ofh_entry.get('cell', {})
                if not cell_data:
                    log_both("OFH entry missing cell data", "warning")
                    continue

                pci = cell_data.get('pci')
                if pci is not None:
                    received_pcis.add(str(pci))

                # Update RU cell metrics
                self.update_ru_cell_metrics(cell_data, timestamp_dt)

            # Log missing data cells
            missing_data_pcis = self.active_cells - received_pcis
            if missing_data_pcis:
                log_both(f"Missing data for active cells in RU: {missing_data_pcis}")

            log_both(
                f"OFH metrics update complete. Active cells: {len(self.active_cells)}, "
                f"Received data: {len(received_pcis)}, Missing data: {len(missing_data_pcis)}"
            )

        except Exception as e:
            log_both(f"Error updating OFH metrics: {e}", "error")

    def update_ru_metrics(self, ru_data: Dict[str, Any], timestamp_dt: Optional[datetime] = None):
        """Update RU-level metrics."""
        influx_points = []

        try:
            # Validate RU structure
            unexpected_ru_fields = set(ru_data.keys()) - self.EXPECTED_RU_FIELDS
            if unexpected_ru_fields:
                log_both(f"Unexpected RU fields: {unexpected_ru_fields}", "warning")

            # Write RU status metrics
            point = Point("ru_metrics").field("active_cells_count", len(self.active_cells)).tag("component", "ru")
            if timestamp_dt:
                point = point.time(timestamp_dt)
            influx_points.append(point)

            # Write RU metrics to InfluxDB
            if influx_points:
                self.exporter.write_to_influx(influx_points)

            # Process OFH data
            ofh_list = ru_data.get('ofh', [])
            if isinstance(ofh_list, list) and ofh_list:
                self.update_ofh_metrics(ofh_list, timestamp_dt)
            elif ofh_list:
                log_both("RU OFH data is not a list or is empty", "warning")
            else:
                log_both("RU missing OFH data", "warning")

        except Exception as e:
            log_both(f"Error updating RU metrics: {e}", "error")

    def update_metrics(self, entry: Dict[str, Any]):
        """Main metrics update function."""

        if not entry:
            log_both("Empty JSON entry received", "error")
            return

        try:
            timestamp = entry.get("timestamp")
            timestamp_dt = timestamp_to_influx_time(timestamp)

            # STEP 1: Update RU metrics
            ru_data = entry.get("ru", {})
            if ru_data:
                self.update_ru_metrics(ru_data, timestamp_dt)
            else:
                log_both("Message missing RU data", "warning")

            # STEP 2: Update system metrics
            self.message_count += 1
            system_points = []

            if timestamp:
                timestamp_val = safe_numeric(timestamp, "timestamp")
                if timestamp_val is not None:
                    system_points.append(
                        Point("ru_system_metrics").field("last_update_timestamp", timestamp_val).tag("component", "ru")
                    )

            system_points.append(
                Point("ru_system_metrics").field("total_messages_received", self.message_count).tag("component", "ru")
            )

            system_points.append(
                Point("ru_system_metrics").field("total_parse_errors", self.parse_error_count).tag("component", "ru")
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
