import os
from typing import List
from influxdb_client import Point, InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS
from exporters.helper_functions import log_both


class exporter:

    def __init__(self, cell_id, cell_name):
        # InfluxDB Configuration
        self.INFLUX_URL = os.getenv("INFLUX_URL", "http://10.233.52.119:80")
        self.INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "my-super-secret-token")
        self.INFLUX_ORG = os.getenv("INFLUX_ORG", "influxdata")
        self.INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "metrics")
        self.cell_id = cell_id
        self.cell_name = cell_name

        try:
            self.influx_client = InfluxDBClient(url=self.INFLUX_URL, token=self.INFLUX_TOKEN, org=self.INFLUX_ORG)
            self.influx_write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)
            log_both("InfluxDB client initialized successfully")
        except Exception as e:
            log_both(f"Failed to initialize InfluxDB client: {e}", "error")
            self.influx_client = None
            self.influx_write_api = None

    def write_to_influx(self, points: List[Point]):
        """Write points to InfluxDB with error handling."""
        if not self.influx_write_api:
            log_both("InfluxDB write API not available, skipping write", "warning")
            return

        try:
            for point in points:
                point.tag("source", "srs_ran")
            self.influx_write_api.write(bucket=self.INFLUX_BUCKET, org=self.INFLUX_ORG, record=points)
            log_both(f"Successfully wrote {len(points)} points to InfluxDB", "debug")
        except Exception as e:
            log_both(f"Failed to write to InfluxDB: {e}", "error")
