import socket
import json
from typing import Dict, Any
import os

from exporters.appResourceUsageMetricsParser import appResourceUsageMetricsParser
from exporters.cellMetricsParser import cellMetricsParser
from exporters.duMetricsParser import duMetricsParser
from exporters.exporter import exporter
from exporters.helper_functions import log_both
from exporters.imeisvParser import imeisvParser
from exporters.ruMetricsParser import ruMetricsParser
from exporters.cuUpMetricsParser import cuUpMetricsParser
from exporters.rlcMetricsParser import rlcMetricsParser

"""
# -- Main Collector Class --

This class is responsible for receiving, categorizing, and exporting incoming metrics data.

1. The `run()` method:
   - Starts a persistent loop that listens on a UDP socket for incoming JSON-encoded messages.
   - Each message is passed to the `categorise_and_parse()` function for further processing.

2. The `categorise_and_parse()` method:
   - Inspects top-level JSON headers to determine the type or source of the metric.
   - Dispatches the JSON payload to the appropriate metrics parser.

3. Metrics parsers:
   - A specific `*MetricsParser` class is selected based on the JSON category.
   - These parser classes extract and format relevant fields from the JSON and export the results

-- InfluxDB Point Organization Strategy --

All points adhere to the following tagging and structure conventions:

1. Each point includes:
   - A `_measurement` field defining the metric category.
   - A `_field` field representing the specific metric value.

2. Required tags:
   - `component`: identifies the source subsystem of the metric. Valid values:
     - 'app_monitor', 'cell', 'ru', 'du', 'cu_up', 'rlc'.
   - `source`: identifies the broader system. Currently always 'srs_ran'.
   - `time`: stored as an epoch timestamp (automatically managed by Influx).

3. Conditional tags (for UE-specific metrics only):
   - `pci`: Physical Cell Identity.
   - `rnti`: Radio Network Temporary Identifier.

Note: This tagging scheme ensures consistent partitioning and filtering for 
efficient querying, monitoring, and visualization across system components.
"""


class collector:
    def __init__(self):
        log_both("=== STARTING METRICS COLLECTOR - INFLUXDB ===")

        # Setup UDP socket
        self.server_socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("0.0.0.0", 55555))

        # Setup cell_collector_id
        self.cell_id = os.getenv('CELL_ID', 'unknown')
        self.cell_name = os.getenv('CELL_NAME', 'Unknown Cell')

        # exporter
        self.exporter = exporter(self.cell_id, self.cell_id)

        # cell_metrics_parser
        self.cellMetricsParser = cellMetricsParser(self.exporter)
        self.duMetricsParser = duMetricsParser(self.exporter)
        self.ruMetricsParser = ruMetricsParser(self.exporter)
        self.appResourceUsageMetricsParser = appResourceUsageMetricsParser(self.exporter)
        self.cuUpMetricsParser = cuUpMetricsParser(self.exporter)
        self.rlcMetricsParser = rlcMetricsParser(self.exporter)
        self.imeisvParser = imeisvParser(self.exporter)

    def categorise_and_parse(self, entry: Dict[str, Any]):
        try:
            if entry.get('cell_metrics') is not None:
                self.cellMetricsParser.update_metrics(entry)
                # log_both("cell metrics received and parsed")
            elif entry.get('du') is not None:
                self.duMetricsParser.update_metrics(entry)
                # log_both("du metrics received and parsed")
            elif entry.get('ru') is not None:
                self.ruMetricsParser.update_metrics(entry)
                # log_both("ru metrics received and parsed")
            elif entry.get('app_resource_usage') is not None:
                self.appResourceUsageMetricsParser.update_metrics(entry)
                # log_both("app_resource metrics received and parsed")
            elif entry.get('cu-up') is not None:
                self.cuUpMetricsParser.update_metrics(entry)
                # log_both("cu_up received and parsed")
            elif entry.get('rlc_metrics') is not None:
                self.rlcMetricsParser.update_metrics(entry)
                # log_both("rlc metrics received and parsed")
            elif entry.get('imeisv') is not None:
                self.imeisvParser.update_metrics(entry)
                # log_both(f"Entry details: {entry}")
            else:
                log_both(f"unknown entry: {entry}", "error")
                pass

        except Exception as e:
            log_both(f"error categorising data: {entry}", "warning")

    def run(self):
        # Main loop
        log_both("Starting main collection loop - this is the right one")

        while True:
            try:
                line = self.server_socket.recv(1024 ** 2).decode('utf-8', errors='replace')
                # log_both(line)
                try:
                    entry = json.loads(line)
                    self.categorise_and_parse(entry)
                except json.JSONDecodeError as e:
                    log_both(f"JSON parse error (total: parse_error_count): {e}", "error")
                except Exception as e:
                    log_both(f"Unexpected error processing message: {e}", "error")

            except KeyboardInterrupt:
                log_both("Shutdown requested")
                break
            except Exception as e:
                log_both(f"Socket error: {e}", "error")


if __name__ == "__main__":
    collector().run()
