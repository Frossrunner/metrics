import socket
import json
from typing import Dict, Any

from exporters.appResourceUsageMetricsParser import appResourceUsageMetricsParser
from exporters.cellMetricsParser import cellMetricsParser
from exporters.duMetricsParser import duMetricsParser
from exporters.exporter import exporter
from exporters.helper_functions import log_both
from exporters.ruMetricsParser import ruMetricsParser
from exporters.cuUpMetricsParser import cuUpMetricsParser
from exporters.rlcMetricsParser import rlcMetricsParser


class collector:
    def __init__(self):
        log_both("=== STARTING METRICS COLLECTOR - INFLUXDB ===")

        # Setup UDP socket
        self.server_socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(("0.0.0.0", 55555))

        # exporter
        self.exporter = exporter()

        # cell_metrics_parser
        self.cellMetricsParser = cellMetricsParser(self.exporter)
        self.duMetricsParser = duMetricsParser(self.exporter)
        self.ruMetricsParser = ruMetricsParser(self.exporter)
        self.appResourceUsageMetricsParser = appResourceUsageMetricsParser(self.exporter)
        self.cuUpMetricsParser = cuUpMetricsParser(self.exporter)
        self.rlcMetricsParser = rlcMetricsParser(self.exporter)

    def categorise_and_parse(self, entry: Dict[str, Any]):
        try:
            if entry.get('cell_metrics'):
                self.cellMetricsParser.update_metrics(entry)
                log_both("cell metrics received")
            elif entry.get('du'):
                self.duMetricsParser.update_metrics(entry)
                log_both("du metrics received")
            elif entry.get('ru'):
                self.ruMetricsParser.update_metrics(entry)
                log_both("ru metrics received")
            elif entry.get('app_resource_usage'):
                self.appResourceUsageMetricsParser.update_metrics(entry)
                log_both("app_resource metrics received")
            elif entry.get('cu-up'):
                self.cuUpMetricsParser.update_metrics(entry)
                log_both("cu_up received")
            elif entry.get('rlc_metrics'):
                self.rlcMetricsParser.update_metrics(entry)
                log_both("rlc metrics received")
            else:
                log_both(f"unknown entry: {entry}")
                pass

        except Exception as e:
            log_both(f"error categorising data: {entry}", "warning")

    def run(self):
        # Main loop
        log_both("Starting main collection loop - this is the right one")

        while True:
            try:
                line = self.server_socket.recv(1024 ** 2).decode('utf-8', errors='replace')
                # log_both(f"Received message: {line}")

                try:
                    entry = json.loads(line)
                    self.categorise_and_parse(entry)
                except json.JSONDecodeError as e:
                    # parse_error_count += 1
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
