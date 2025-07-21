# 5G Testbed Metrics Collector

A collection of containerized metrics collectors designed to run in Kubernetes and gather telemetry data from a 5G testbed environment. All collected metrics are stored in InfluxDB for monitoring and analysis.

## Overview

This project provides metrics collection from three key components of the 5G testbed:

- **Core Network Functions** - Collects Prometheus-style metrics from 5G core components (UPF, PCF, AMF, SMF)
- **SRS RAN** - Radio Access Network metrics collection
- **Network Switches** - Infrastructure monitoring

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Core Network │    │    SRS RAN      │    │   Switches      │
│   Functions     │    │                 │    │                 │
└─────────┬───────┘    └─────────┬───────┘    └─────────┬───────┘
          │                      │                      │
          │ Prometheus           │ Custom               │ SNMP
          │ Metrics              │ Metrics              │ Metrics
          │                      │                      │
          ▼                      ▼                      ▼
    ┌─────────────────────────────────────────────────────────────┐
    │                Kubernetes Cluster                          │
    │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
    │  │Core         │  │RAN          │  │Switch       │        │
    │  │Collector    │  │Collector    │  │Collector    │        │
    │  └─────────────┘  └─────────────┘  └─────────────┘        │
    └─────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
                  ┌─────────────────┐
                  │   InfluxDB      │
                  │   Database      │
                  └─────────────────┘
```

## Core Network Collector

### Features

- Scrapes Prometheus-style metrics from 5G core network functions
- Configurable scrape intervals and timeouts
- Automatic metric parsing and InfluxDB point conversion
- Kubernetes-native deployment with ConfigMap support
- Robust error handling and logging

### Configuration

The core collector is configured through environment variables, typically set via Kubernetes ConfigMap:

| Variable | Description | Example |
|----------|-------------|---------|
| `INFLUXDB_URL` | InfluxDB server URL | `http://10.233.52.119:80` |
| `INFLUXDB_TOKEN` | Authentication token | `your-influx-token` |
| `INFLUXDB_ORG` | InfluxDB organization | `your-org` |
| `INFLUXDB_BUCKET` | Target bucket name | `5g-metrics` |
| `SCRAPE_INTERVAL` | Collection interval (seconds) | `1.0` |
| `SCRAPE_TIMEOUT` | Request timeout (seconds) | `0.5` |
| `ENDPOINTS` | JSON array of metric endpoints | See example below |

### Endpoint Configuration

Configure the `ENDPOINTS` environment variable with a JSON array:

```json
[
  {
    "name": "upf",
    "url": "http://10.0.0.1:9097/metrics",
    "component": "upf"
  },
  {
    "name": "amf", 
    "url": "http://10.0.0.2:9095/metrics",
    "component": "amf"
  },
  {
    "name": "smf",
    "url": "http://10.0.0.3:9094/metrics", 
    "component": "smf"
  },
  {
    "name": "pcf",
    "url": "http://10.0.0.4:9103/metrics",
    "component": "pcf"
  }
]
```

### Kubernetes Deployment

1. **Configure the deployment**: Edit `core_collector.yaml` with your environment-specific values
2. **Deploy to cluster**:
   ```bash
   kubectl apply -f core_collector.yaml
   ```
3. **Verify deployment**:
   ```bash
   kubectl get pods -l app=core-metrics-collector
   kubectl logs -f deployment/core-metrics-collector
   ```

## Data Model

Metrics are stored in InfluxDB with the following structure:

- **Measurement**: Component name (e.g., "upf", "amf", "smf", "pcf")
- **Tags**: 
  - `metric_name`: Original Prometheus metric name
  - `source`: Always "core" for core network metrics
- **Fields**:
  - `value`: Metric value (float)
- **Timestamp**: Collection time

## Monitoring and Troubleshooting

### Health Checks

The collector provides detailed logging for monitoring:

```bash
# View recent logs
kubectl logs -f deployment/core-metrics-collector

# Check InfluxDB connectivity
kubectl exec -it deployment/core-metrics-collector -- python -c "
from core_collector import MetricsCollector
collector = MetricsCollector()
print('InfluxDB connection: OK')
"
```

### Common Issues

**Connection Timeouts**
- Increase `SCRAPE_TIMEOUT` value
- Check network connectivity between collector and endpoints

**InfluxDB Write Failures**
- Verify `INFLUXDB_TOKEN` has write permissions
- Check InfluxDB server health and storage capacity

**Missing Metrics**
- Validate endpoint URLs are accessible
- Check Prometheus metrics format compliance

## Development

### Building Container Image

```bash
docker build -t core-collector:latest .
docker tag core-collector your-registry/core-collector:latest
docker push your-registry/core-collector:latest
```

### Upload yaml: 
```bash
scp core-collector.yaml your-kluster
```
### Apply on Kluster:
```
kubectl apply -f core-collector.yaml
```

## Requirements

- Python 3.8+
- Kubernetes cluster
- InfluxDB 2.x
- Network access to 5G core components

  
