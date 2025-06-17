import os
import time
from datetime import datetime

import requests
import logging
from typing import Dict, List, Optional, Any
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MetricsCollector:
    def __init__(self):
        # InfluxDB configuration from environment variables
        self.influx_url = os.getenv('INFLUXDB_URL', 'http://10.233.50.123:80')
        self.influx_token = os.getenv('INFLUXDB_TOKEN', 'my-super-secret-token')
        self.influx_org = os.getenv('INFLUXDB_ORG', 'influxdata')
        self.influx_bucket = os.getenv('INFLUXDB_BUCKET', 'metrics')

        # Scrape configuration
        self.scrape_interval = float(os.getenv('SCRAPE_INTERVAL', '1'))  # seconds
        self.scrape_timeout = float(os.getenv('SCRAPE_TIMEOUT', '0.1'))  # seconds

        # 5G Network Functions configuration based on your ServiceMonitor
        self.endpoints = [
            {
                'name': 'upf',
                'url': 'http://10.0.0.1:9097/metrics?fallback_scrape_protocol=text/plain',
                'component': 'upf'
            },
            {
                'name': 'pcf',
                'url': 'http://10.0.0.1:9103/metrics',
                'component': 'pcf'
            },
            {
                'name': 'amf',
                'url': 'http://10.0.0.1:9095/metrics',
                'component': 'amf'
            },
            {
                'name': 'smf',
                'url': 'http://10.0.0.1:9094/metrics',
                'component': 'smf'
            }
        ]

        # Initialize InfluxDB client
        self.influx_client = None
        self.write_api = None
        self._init_influxdb()

    def _init_influxdb(self):
        """Initialize InfluxDB client and write API"""
        try:
            self.influx_client = InfluxDBClient(
                url=self.influx_url,
                token=self.influx_token,
                org=self.influx_org
            )
            self.write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)

            # Test connection
            self.influx_client.ping()
            logger.info(f"Connected to InfluxDB at {self.influx_url}")

        except Exception as e:
            logger.error(f"Failed to connect to InfluxDB: {e}")
            raise

    def parse_prometheus_metrics(self, metrics_text: str, endpoint: dict) -> List[Point]:
        """Parse Prometheus metrics format and convert to InfluxDB Points"""
        points = []
        current_time = time.time()
        current_time_influx = self.timestamp_to_influx_time(current_time)

        for line in metrics_text.strip().split('\n'):
            line = line.strip()

            # Skip comments and empty lines
            if not line or line.startswith('#'):
                continue

            # Parse metric line: metric_name{labels} value [timestamp]
            try:
                # Split metric name and value
                if '{' in line:
                    # Metric with labels
                    metric_part, value_part = line.rsplit(' ', 1)
                    metric_name = metric_part.split('{')[0]
                    labels_part = metric_part[metric_part.find('{'):metric_part.rfind('}') + 1]
                    labels = self._parse_labels(labels_part)
                else:
                    # Simple metric without labels
                    parts = line.rsplit(' ', 1)
                    if len(parts) != 2:
                        continue
                    metric_name, value_part = parts
                    labels = {}

                # Convert value to float
                try:
                    value = float(value_part)
                except ValueError:
                    logger.warning(f"Could not parse value '{value_part}' for metric {metric_name}")
                    continue

                # Create InfluxDB point
                point = Point(endpoint['component']) \
                    .tag("metric_name", metric_name) \
                    .field("value", value) \
                    .tag("source", "core") \
                    .time(current_time_influx)

                # Add labels as tags
                # for label_key, label_value in labels.items():
                #     point = point.tag(label_key, label_value)

                points.append(point)

            except Exception as e:
                logger.warning(f"Failed to parse metric line '{line}': {e}")
                continue

        return points

    def _parse_labels(self, labels_str: str) -> Dict[str, str]:
        """Parse Prometheus labels format {key1="value1",key2="value2"}"""
        labels = {}
        if not labels_str or labels_str == '{}':
            return labels

        # Remove outer braces
        labels_content = labels_str.strip('{}')

        # Split by comma, but handle quoted values
        label_pairs = []
        current_pair = ""
        in_quotes = False

        for char in labels_content:
            if char == '"':
                in_quotes = not in_quotes
            elif char == ',' and not in_quotes:
                if current_pair.strip():
                    label_pairs.append(current_pair.strip())
                current_pair = ""
                continue
            current_pair += char

        if current_pair.strip():
            label_pairs.append(current_pair.strip())

        # Parse each key=value pair
        for pair in label_pairs:
            if '=' in pair:
                key, value = pair.split('=', 1)
                key = key.strip()
                value = value.strip().strip('"')
                labels[key] = value

        return labels

    def scrape_endpoint(self, endpoint: Dict) -> Optional[List[Point]]:
        """Scrape metrics from a single endpoint"""
        try:
            # logger.info(f"Scraping {endpoint['name']} from {endpoint['url']}")

            response = requests.get(
                endpoint['url'],
                timeout=self.scrape_timeout,
                headers={'Accept': 'text/plain'}
            )
            response.raise_for_status()

            # Parse metrics and create InfluxDB points
            points = self.parse_prometheus_metrics(response.text, endpoint)
            # logger.info(f"Parsed {len(points)} metrics from {endpoint['name']}")

            return points

        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to scrape {endpoint['name']}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error scraping {endpoint['name']}: {e}")
            return None

    def timestamp_to_influx_time(self, timestamp_value: Any) -> Optional[datetime]:
        """Convert timestamp to InfluxDB-compatible datetime object."""
        if timestamp_value is None:
            return None

        try:
            # Assume timestamp is in seconds (Unix timestamp)
            timestamp_float = float(timestamp_value)
            return datetime.fromtimestamp(timestamp_float)
        except (ValueError, TypeError) as e:
            logger.error(f"Error converting timestamp {timestamp_value} to datetime: {e}", "warning")
            return None

    def write_to_influx(self, points: List[Point]):
        """Write points to InfluxDB"""
        if not self.write_api:
            logger.info("InfluxDB write API not available, skipping write", "warning")
            return
        try:

            self.write_api.write(
                bucket=self.influx_bucket,
                org=self.influx_org,
                record=points
            )
            logger.info(f"Successfully wrote {len(points)} points to InfluxDB")

        except Exception as e:
            logger.error(f"Failed to write to InfluxDB: {e}")
            raise

    def collect_and_send_metrics(self):
        """Collect metrics from all endpoints and send to InfluxDB"""
        all_points = []

        for endpoint in self.endpoints:
            points = self.scrape_endpoint(endpoint)
            if points:
                all_points.extend(points)

        if all_points:
            self.write_to_influx(all_points)
            # logger.info(f"Collection cycle completed. Total points: {len(all_points)}")
        else:
            logger.warning("No metrics collected in this cycle")

    def run(self):
        """Main collection loop"""
        logger.info("Starting 5G metrics collector...")
        logger.info(f"Scrape interval: {self.scrape_interval}s")
        logger.info(f"Endpoints: {[ep['name'] for ep in self.endpoints]}")
        logger.info(f"InfluxDB: {self.influx_url}/{self.influx_bucket}")

        while True:
            try:
                start_time = time.time()
                self.collect_and_send_metrics()

                # Calculate sleep time to maintain consistent interval
                elapsed = time.time() - start_time
                sleep_time = float(self.scrape_interval - elapsed)
                if sleep_time > 0:
                    logger.debug(f"Sleeping for {sleep_time:.2f}s")
                    time.sleep(sleep_time)
                else:
                    logger.warning(f"Collection took {elapsed:.2f}s, longer than interval {self.scrape_interval}s")

            except KeyboardInterrupt:
                logger.info("Received interrupt signal, shutting down...")
                break
            except Exception as e:
                logger.error(f"Unexpected error in main loop: {e}")

        # Cleanup
        if self.influx_client:
            self.influx_client.close()
        logger.info("Metrics collector stopped")


def main():
    """Entry point"""
    collector = MetricsCollector()
    collector.run()


if __name__ == "__main__":
    main()