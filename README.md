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

## Requirements

- Python 3.8+
- Kubernetes cluster
- InfluxDB 2.x
- Network access to 5G core components

  
