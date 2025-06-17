import fileinput
import os
from datetime import datetime
from typing import List

from influxdb_client import Point, InfluxDBClient
from influxdb_client.client.write_api import SYNCHRONOUS


class exporter:

    def __init__(self):
        # InfluxDB Configuration
        self.INFLUX_URL = os.getenv("INFLUX_URL", "http://10.233.50.123:80")
        self.INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "my-super-secret-token")
        self.INFLUX_ORG = os.getenv("INFLUX_ORG", "influxdata")
        self.INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "metrics")

        try:
            self.influx_client = InfluxDBClient(url=self.INFLUX_URL, token=self.INFLUX_TOKEN, org=self.INFLUX_ORG)
            self.influx_write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)
        except Exception as e:
            self.influx_client = None
            self.influx_write_api = None

    def write_to_influx(self, points: List[Point]):
        """Write points to InfluxDB with error handling."""
        try:
            self.influx_write_api.write(bucket=self.INFLUX_BUCKET, org=self.INFLUX_ORG, record=points)
        except Exception as e:
            print(f"Failed to write to InfluxDB: {e}", "error")


field_names = ["rx_total", "rx_on_time", "rx_early", "rx_late", "rx_on_time_c", "rx_early_c", "rx_late_c", "tx_total"]
exporter = exporter()

for line in fileinput.input():
    line = line.strip()
    values = [v.strip() for v in line.strip().split('|')]
    time, rx_total, rx_on_time, rx_early, rx_late, rx_on_time_c, rx_early_c, rx_late_c, tx_total = values
    print(time, rx_total, rx_on_time, rx_early, rx_late, rx_on_time_c, rx_early_c, rx_late_c, tx_total)

    #   initialise a list of points
    points = []

    #   parse time
    timestamp_float = float(time)
    timestamp_dt = datetime.fromtimestamp(timestamp_float)

    # for every field, create a point, append to points
    for field, value in zip(field_names, values[1:]):
        point = Point("ru") \
            .field("rx", field) \
            .tag("source", "ru")
        if timestamp_dt:
            point = point.time(timestamp_dt)
        points.append(point)

#   send to influx
    exporter.write_to_influx(points)
