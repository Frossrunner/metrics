import os
import time
import json
from datetime import datetime
import requests
import logging
from typing import Dict, List, Optional, Any
from influxdb_client import InfluxDBClient, Point
from influxdb_client.rest import ApiException
from influxdb_client.client.write_api import SYNCHRONOUS

# Configure logging with detailed formatting
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('/tmp/core_collector.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)

# Set third-party library log levels to reduce noise
logging.getLogger('urllib3.connectionpool').setLevel(logging.WARNING)
logging.getLogger('influxdb_client').setLevel(logging.WARNING)


class MetricsCollector:
    def __init__(self):
        logger.info("Initializing MetricsCollector...")

        # InfluxDB configuration from environment variables
        self.influx_url = os.getenv('INFLUXDB_URL', 'http://10.233.71.54:80')
        self.influx_token = os.getenv('INFLUXDB_TOKEN', 'my-super-secret-token')
        self.influx_org = os.getenv('INFLUXDB_ORG', 'influxdata')
        self.influx_bucket = os.getenv('INFLUXDB_BUCKET', 'metrics')

        logger.info(f"InfluxDB Configuration:")
        logger.info(f"  URL: {self.influx_url}")
        logger.info(f"  Organization: {self.influx_org}")
        logger.info(f"  Bucket: {self.influx_bucket}")
        logger.info(
            f"  Token: {'*' * (len(self.influx_token) - 4) + self.influx_token[-4:] if len(self.influx_token) > 4 else '****'}")

        # Scrape configuration
        self.scrape_interval = float(os.getenv('SCRAPE_INTERVAL', '1'))
        self.scrape_timeout = float(os.getenv('SCRAPE_TIMEOUT', '0.5'))

        logger.info(f"Scrape Configuration:")
        logger.info(f"  Interval: {self.scrape_interval}s")
        logger.info(f"  Timeout: {self.scrape_timeout}s")

        # Parse endpoints from environment variable
        self.endpoints = self._parse_endpoints()

        if not self.endpoints:
            logger.error("No endpoints configured! Please set the ENDPOINTS environment variable.")
            raise ValueError("No endpoints configured")

        logger.info(f"Configured {len(self.endpoints)} endpoints:")
        for i, endpoint in enumerate(self.endpoints, 1):
            logger.info(f"  {i}. {endpoint['name']} ({endpoint['component']}) -> {endpoint['url']}")

        # Initialize InfluxDB client
        self.influx_client = None
        self.write_api = None
        self._init_influxdb()

        # Statistics tracking
        self.stats = {
            'total_scrapes': 0,
            'successful_scrapes': 0,
            'failed_scrapes': 0,
            'total_points_written': 0,
            'influx_write_failures': 0,
            'start_time': time.time()
        }

        logger.info("MetricsCollector initialization completed successfully")

    def _parse_endpoints(self) -> List[Dict]:
        """Parse endpoints from environment variable"""
        endpoints_env = os.getenv('ENDPOINTS', '[]')
        logger.debug(f"Raw ENDPOINTS env var: {endpoints_env}")

        try:
            if isinstance(endpoints_env, str):
                endpoints = json.loads(endpoints_env)
            else:
                endpoints = endpoints_env

            if not isinstance(endpoints, list):
                logger.error(f"ENDPOINTS must be a list, got {type(endpoints)}")
                return []

            # Validate endpoint structure
            validated_endpoints = []
            for i, endpoint in enumerate(endpoints):
                if not isinstance(endpoint, dict):
                    logger.warning(f"Endpoint {i} is not a dictionary, skipping")
                    continue

                required_fields = ['name', 'url', 'component']
                missing_fields = [field for field in required_fields if field not in endpoint]

                if missing_fields:
                    logger.warning(f"Endpoint {i} missing required fields {missing_fields}, skipping")
                    continue

                validated_endpoints.append(endpoint)
                logger.debug(f"Validated endpoint: {endpoint['name']}")

            return validated_endpoints

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse ENDPOINTS JSON: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error parsing endpoints: {e}")
            return []

    def _init_influxdb(self):
        """Initialize InfluxDB client and write API"""
        logger.info("Initializing InfluxDB connection...")

        try:
            self.influx_client = InfluxDBClient(
                url=self.influx_url,
                token=self.influx_token,
                org=self.influx_org
            )
            self.write_api = self.influx_client.write_api(write_options=SYNCHRONOUS)

            # Test connection
            logger.debug("Testing InfluxDB connection...")
            health = self.influx_client.health()
            logger.info(f"InfluxDB health check: {health.status}")

            # Test bucket accessibility
            try:
                buckets_api = self.influx_client.buckets_api()
                bucket = buckets_api.find_bucket_by_name(self.influx_bucket)
                if bucket:
                    logger.info(f"Successfully connected to bucket '{self.influx_bucket}'")
                else:
                    logger.warning(f"Bucket '{self.influx_bucket}' not found, but connection is valid")
            except Exception as e:
                logger.warning(f"Could not verify bucket access: {e}")

            logger.info("InfluxDB initialization completed successfully")

        except Exception as e:
            logger.error(f"Failed to initialize InfluxDB connection: {e}")
            logger.error("This will prevent metric storage. Check your InfluxDB configuration.")
            raise

    def parse_prometheus_metrics(self, metrics_text: str, endpoint: dict) -> List[Point]:
        """Parse Prometheus metrics format and convert to InfluxDB Points"""
        logger.debug(f"Parsing metrics from {endpoint['name']}, text length: {len(metrics_text)} chars")

        points = []
        lines_processed = 0
        lines_skipped = 0
        parsing_errors = 0
        current_time = time.time()
        current_time_influx = self.timestamp_to_influx_time(current_time)

        for line_num, line in enumerate(metrics_text.strip().split('\n'), 1):
            line = line.strip()
            lines_processed += 1

            # Skip comments and empty lines
            if not line or line.startswith('#'):
                lines_skipped += 1
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
                        lines_skipped += 1
                        logger.debug(f"Skipping malformed line {line_num}: {line[:50]}...")
                        continue
                    metric_name, value_part = parts
                    labels = {}

                # Convert value to float
                try:
                    value = float(value_part)
                except ValueError:
                    parsing_errors += 1
                    logger.debug(f"Could not parse value '{value_part}' for metric {metric_name} on line {line_num}")
                    continue

                # Create InfluxDB point
                point = Point(endpoint['component']) \
                    .tag("_measurement", metric_name) \
                    .field("value", value) \
                    .tag("source", "core") \
                    .tag("endpoint", endpoint['name']) \
                    .time(current_time_influx)

                # Add labels as tags (commented out in original, keeping for reference)
                # for label_key, label_value in labels.items():
                #     point = point.tag(label_key, label_value)

                points.append(point)

                # Log first few metrics for debugging
                if len(points) <= 5:
                    logger.debug(f"Created point: {metric_name}={value} for {endpoint['component']}")

            except Exception as e:
                parsing_errors += 1
                logger.debug(f"Failed to parse metric line {line_num} '{line[:50]}...': {e}")
                continue

        # Log parsing summary
        logger.debug(f"Parsing summary for {endpoint['name']}:")
        logger.debug(f"  Lines processed: {lines_processed}")
        logger.debug(f"  Lines skipped: {lines_skipped}")
        logger.debug(f"  Parsing errors: {parsing_errors}")
        logger.debug(f"  Points created: {len(points)}")

        if parsing_errors > 0:
            logger.warning(f"Encountered {parsing_errors} parsing errors for {endpoint['name']}")

        return points

    def _parse_labels(self, labels_str: str) -> Dict[str, str]:
        """Parse Prometheus labels format {key1="value1",key2="value2"}"""
        logger.debug(f"Parsing labels: {labels_str}")

        labels = {}
        if not labels_str or labels_str == '{}':
            return labels

        try:
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

            logger.debug(f"Parsed {len(labels)} labels: {list(labels.keys())}")

        except Exception as e:
            logger.warning(f"Failed to parse labels '{labels_str}': {e}")

        return labels

    def scrape_endpoint(self, endpoint: Dict) -> Optional[List[Point]]:
        """Scrape metrics from a single endpoint"""
        start_time = time.time()
        endpoint_name = endpoint['name']

        logger.debug(f"Starting scrape of {endpoint_name} from {endpoint['url']}")

        try:
            self.stats['total_scrapes'] += 1

            response = requests.get(
                endpoint['url'],
                timeout=self.scrape_timeout,
                headers={'Accept': 'text/plain'}
            )

            elapsed_time = time.time() - start_time
            logger.debug(f"HTTP request to {endpoint_name} completed in {elapsed_time:.3f}s")

            response.raise_for_status()

            if not response.text:
                logger.warning(f"Empty response from {endpoint_name}")
                return []

            # Parse metrics and create InfluxDB points
            points = self.parse_prometheus_metrics(response.text, endpoint)

            self.stats['successful_scrapes'] += 1

            logger.info(f"Successfully scraped {len(points)} metrics from {endpoint_name} "
                        f"(HTTP {response.status_code}, {len(response.text)} chars, {elapsed_time:.3f}s)")

            return points

        except requests.exceptions.Timeout:
            self.stats['failed_scrapes'] += 1
            logger.error(f"Timeout scraping {endpoint_name} after {self.scrape_timeout}s")
            return None

        except requests.exceptions.ConnectionError:
            self.stats['failed_scrapes'] += 1
            logger.error(f"Connection error scraping {endpoint_name}: endpoint unreachable")
            return None

        except requests.exceptions.HTTPError as e:
            self.stats['failed_scrapes'] += 1
            logger.error(
                f"HTTP error scraping {endpoint_name}: {e} (status: {e.response.status_code if e.response else 'unknown'})")
            return None

        except requests.exceptions.RequestException as e:
            self.stats['failed_scrapes'] += 1
            logger.error(f"Request error scraping {endpoint_name}: {e}")
            return None

        except Exception as e:
            self.stats['failed_scrapes'] += 1
            logger.error(f"Unexpected error scraping {endpoint_name}: {e}")
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
            logger.warning(f"Error converting timestamp {timestamp_value} to datetime: {e}")
            return None

    def write_to_influx(self, points: List[Point]):
        """Write points to InfluxDB"""
        if not self.write_api:
            logger.error("InfluxDB write API not available, cannot write metrics")
            return False

        if not points:
            logger.debug("No points to write to InfluxDB")
            return True

        start_time = time.time()

        try:
            logger.debug(f"Writing {len(points)} points to InfluxDB bucket '{self.influx_bucket}'")

            self.write_api.write(
                bucket=self.influx_bucket,
                org=self.influx_org,
                record=points
            )

            elapsed_time = time.time() - start_time
            self.stats['total_points_written'] += len(points)

            logger.info(f"Successfully wrote {len(points)} points to InfluxDB in {elapsed_time:.3f}s")
            return True

        except ApiException as e:
            self.stats['influx_write_failures'] += 1
            logger.error(f"InfluxDB API error writing {len(points)} points: {e}")
            logger.error(f"  Status: {e.status}")
            logger.error(f"  Reason: {e.reason}")
            return False

        except Exception as e:
            self.stats['influx_write_failures'] += 1
            logger.error(f"Unexpected error writing {len(points)} points to InfluxDB: {e}")
            return False

    def collect_and_send_metrics(self):
        """Collect metrics from all endpoints and send to InfluxDB"""
        cycle_start_time = time.time()
        all_points = []
        successful_endpoints = 0
        failed_endpoints = 0

        logger.debug(f"Starting collection cycle for {len(self.endpoints)} endpoints")

        for endpoint in self.endpoints:
            points = self.scrape_endpoint(endpoint)
            if points:
                all_points.extend(points)
                successful_endpoints += 1
            else:
                failed_endpoints += 1

        cycle_elapsed = time.time() - cycle_start_time

        if all_points:
            write_success = self.write_to_influx(all_points)
            status = "completed successfully" if write_success else "completed with write errors"
        else:
            write_success = True
            status = "completed with no metrics collected"

        logger.info(f"Collection cycle {status}:")
        logger.info(f"  Duration: {cycle_elapsed:.3f}s")
        logger.info(f"  Endpoints: {successful_endpoints} successful, {failed_endpoints} failed")
        logger.info(f"  Total points: {len(all_points)}")

        if failed_endpoints > 0:
            logger.warning(f"Failed to collect from {failed_endpoints}/{len(self.endpoints)} endpoints")

    def log_statistics(self):
        """Log collection statistics"""
        uptime = time.time() - self.stats['start_time']
        success_rate = (self.stats['successful_scrapes'] / max(1, self.stats['total_scrapes'])) * 100

        logger.info("=== COLLECTION STATISTICS ===")
        logger.info(f"Uptime: {uptime:.1f}s ({uptime / 3600:.1f}h)")
        logger.info(f"Total scrapes: {self.stats['total_scrapes']}")
        logger.info(f"Successful scrapes: {self.stats['successful_scrapes']} ({success_rate:.1f}%)")
        logger.info(f"Failed scrapes: {self.stats['failed_scrapes']}")
        logger.info(f"Points written: {self.stats['total_points_written']}")
        logger.info(f"InfluxDB write failures: {self.stats['influx_write_failures']}")
        logger.info("=============================")

    def run(self):
        """Main collection loop"""
        logger.info("=" * 60)
        logger.info("Starting 5G Core Network Metrics Collector")
        logger.info("=" * 60)
        logger.info(f"Configuration Summary:")
        logger.info(f"  Scrape interval: {self.scrape_interval}s")
        logger.info(f"  Scrape timeout: {self.scrape_timeout}s")
        logger.info(f"  Endpoints: {len(self.endpoints)}")
        logger.info(f"  InfluxDB: {self.influx_url}")
        logger.info(f"  Bucket: {self.influx_bucket}")
        logger.info("=" * 60)

        # Log statistics every 10 minutes
        last_stats_log = time.time()
        stats_interval = 600  # 10 minutes

        try:
            while True:
                loop_start_time = time.time()

                # Collect and send metrics
                self.collect_and_send_metrics()

                # Log statistics periodically
                current_time = time.time()
                if current_time - last_stats_log >= stats_interval:
                    self.log_statistics()
                    last_stats_log = current_time

                # Calculate sleep time to maintain consistent interval
                elapsed = time.time() - loop_start_time
                sleep_time = max(0, self.scrape_interval - elapsed)

                if sleep_time > 0:
                    logger.debug(f"Cycle completed in {elapsed:.3f}s, sleeping for {sleep_time:.3f}s")
                    time.sleep(sleep_time)
                else:
                    logger.warning(
                        f"Collection cycle took {elapsed:.3f}s, exceeding interval of {self.scrape_interval}s by {elapsed - self.scrape_interval:.3f}s")

        except KeyboardInterrupt:
            logger.info("Received interrupt signal (Ctrl+C), initiating graceful shutdown...")

        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}", exc_info=True)
            raise

        finally:
            # Final statistics and cleanup
            logger.info("Shutting down metrics collector...")
            self.log_statistics()

            if self.influx_client:
                logger.info("Closing InfluxDB connection...")
                try:
                    self.influx_client.close()
                    logger.info("InfluxDB connection closed successfully")
                except Exception as e:
                    logger.error(f"Error closing InfluxDB connection: {e}")

            logger.info("5G Core Network Metrics Collector shutdown complete")
            logger.info("=" * 60)


def main():
    """Entry point with enhanced error handling"""
    try:
        collector = MetricsCollector()
        collector.run()
    except KeyboardInterrupt:
        logger.info("Application terminated by user")
    except Exception as e:
        logger.error(f"Fatal error during startup: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()