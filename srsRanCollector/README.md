# sRS RAN Metrics Collector

A Kubernetes-native metrics collection service that receives JSON-formatted metrics from sRS RAN via UDP and exports them to InfluxDB for monitoring and visualization.

## Overview

The collector operates as a persistent UDP server that:
- Listens for JSON-encoded metrics on port 55555
- Categorizes incoming data by metric type
- Parses and transforms metrics using specialized parsers
- Exports structured data points to InfluxDB with consistent tagging

## Features

- **Multi-component Support**: Handles metrics from cell, RU, DU, CU-UP, RLC, and application monitoring
- **Robust UE Tracking**: Tracks user equipment with PCI and RNTI identifiers
- **Automatic Parsing**: Smart categorization and parsing of different metric types
- **Kubernetes Native**: Service/pod architecture for efficient deployment and updates
- **Error Resilience**: Comprehensive error handling and logging
- **Scalable Configuration**: Easy reconfiguration through YAML manifests

## Supported Metric Types

| Component | Description |
|-----------|-------------|
| `cell_metrics` | Cell-level performance metrics |
| `du` | Distributed Unit metrics |
| `ru` | Radio Unit metrics |
| `cu-up` | Centralized Unit User Plane metrics |
| `rlc_metrics` | Radio Link Control metrics |
| `app_resource_usage` | Application resource monitoring |
| `imeisv` | International Mobile Equipment Identity |

## Data Model

All InfluxDB points follow a consistent tagging structure:

### Required Fields
- `_measurement`: Metric category identifier
- `_field`: Specific metric value
- `time`: Epoch timestamp (auto-managed)

### Required Tags
- `component`: Source subsystem (`app_monitor`, `cell`, `ru`, `du`, `cu_up`, `rlc`)
- `source`: System identifier (currently `srs_ran`)

### Conditional Tags (UE-specific metrics)
- `pci`: Physical Cell Identity
- `rnti`: Radio Network Temporary Identifier

## Configuration

Configure the collector through environment variables in `collector-pods.yaml`:

| Variable | Description | Example                  |
|----------|-------------|--------------------------|
| `CELL_ID` | Unique cell identifier | `cell-0`                 |
| `CELL_NAME` | Human-readable cell name | `Downtown Site A`        |
| `METRICS_PORT` | InfluxDB endpoint port | `8086`                   |
| `METRICS_ADDR` | InfluxDB server address | `http://255.255.255.255` |

## Quick Start

### Prerequisites
- Kubernetes cluster
- Docker registry access
- InfluxDB instance
- sRS RAN deployment

### 1. Build and Push Docker Image

```bash
# Build the collector image
docker build -t collector-exporter .

# Tag for your registry
docker tag collector-exporter your-registry/collector-exporter:latest

# Push to registry
docker push your-registry/collector-exporter:latest
```

### 2. Deploy to Kubernetes

```bash
# Copy manifests to cluster node
scp collector-services.yaml your-cluster-node:/path/
scp collector-pods.yaml your-cluster-node:/path/

# Apply manifests
kubectl apply -f collector-services.yaml
kubectl apply -f collector-pods.yaml

# Verify deployment
kubectl get pods -n monitoring
kubectl get services -l -n monitoring
```

### 3. Configure sRS RAN

Update your sRS RAN configuration to send metrics to the collector service

## Operations

### Monitoring Deployment

```bash
# Check pod status
kubectl get pods -l app=collector-exporter

# View logs
kubectl logs -l app=collector-exporter -f

# Check service endpoints
kubectl get endpoints collector-service
```

### Updating the Collector

For code updates without service changes:

```bash
# Rebuild and push image
docker build -t collector-exporter .
docker push your-registry/collector-exporter:latest

# Rolling update
kubectl delete -f collector-pods.yaml
kubectl apply -f collector-pods.yaml
```

### Scaling

Horizontal scaling by copying more pods/services in the .yaml files

pods/services will follow a naming convention of collector-pod-#

## Troubleshooting

### Common Issues

**No metrics received**
- Verify sRS RAN is sending to correct IP/port
- Check network connectivity between sRS RAN and collector
- Confirm UDP port 55555 is accessible

**JSON parse errors**
- Check sRS RAN metric format configuration
- Verify UDP packet size limits
- Review collector logs for specific JSON errors

**InfluxDB connection issues**
- Validate `METRICS_ADDR` and `METRICS_PORT` configuration
- Check InfluxDB authentication credentials
- Verify network policies allow collector → InfluxDB communication

### Log Analysis

```bash
# Real-time log monitoring
kubectl logs -l app=collector-exporter -f

# Error-specific logs
kubectl logs -l app=collector-exporter | grep -i error

# Metrics processing stats
kubectl logs -l app=collector-exporter | grep "received and parsed"
```

## Architecture

```
sRS RAN → UDP:55555 → Collector Service → InfluxDB
                    ↓
               [Metric Parsers]
              - cellMetricsParser
              - duMetricsParser  
              - ruMetricsParser
              - cuUpMetricsParser
              - rlcMetricsParser
              - appResourceUsageMetricsParser
              - imeisvParser
```
