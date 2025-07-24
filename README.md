# 5G Testbed Metrics Collector
A collection of containerized metrics collectors designed to run in Kubernetes and gather telemetry data from a 5G testbed environment. All collected metrics are stored in InfluxDB for monitoring and analysis.

## Overview
This project provides metrics collection from three key components of the 5G testbed:
- **Core Network Functions** - Collects Prometheus-style metrics from 5G core components (UPF, PCF, AMF, SMF)
- **SRS RAN** - Radio Access Network metrics collection
- **Network Switches** - Infrastructure monitoring via SNMP

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
- SNMP access to network switches

## SNMP Configuration for Switch Monitoring

### Installation and Setup

The switch collector uses Telegraf with SNMP input plugin to gather metrics from network switches. Follow these steps to configure SNMP monitoring:

#### 1. Install Required Packages

```bash
# Add InfluxData repository
wget -qO- https://repos.influxdata.com/influxdata-archive_compat.key | sudo tee /etc/apt/keyrings/influxdata.asc
echo "deb https://repos.influxdata.com/ubuntu $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/influxdata.list

# Update package list and install
sudo apt update
sudo apt install telegraf
sudo apt install snmp
```

#### 2. Test SNMP Connectivity

Before configuring Telegraf, verify SNMP connectivity to your switches:

```bash
# Test SNMP walk to verify connectivity
# Replace 10.15.15.1 with your switch IP and campus5g-mgmt with your community string
snmpwalk -v2c -c campus5g-mgmt 10.15.15.1 1.3.6.1.2.1.1
```

#### 3. Configure SNMP MIBs

Configure SNMP to load all available MIBs for better OID resolution:

```bash
sudo nano /etc/snmp/snmp.conf
```

Add or ensure these lines are present:

```
mibs +ALL
mibdirs /var/lib/mibs/ietf:/usr/share/snmp/mibs/ietf:/usr/share/snmp/mibs/iana:/usr/share/snmp/mibs
```

#### 4. Create Telegraf SNMP Configuration

Create a dedicated configuration file for SNMP switch monitoring:

```bash
sudo nano /etc/telegraf/telegraf.d/snmp_switch.conf
```

Add the SNMP configuration as provided

#### 5. Test Configuration

Test your Telegraf configuration before enabling the service:

```bash
sudo telegraf --config /etc/telegraf/telegraf.d/snmp_switch.conf --test
```

#### 6. Configure Main Telegraf Service

The main Telegraf configuration file is located at `/etc/telegraf/telegraf.conf`. Ensure it includes the following line to automatically load all configuration files from the telegraf.d directory:

```toml
# Include all files in telegraf.d directory
[[include]]
  files = ["/etc/telegraf/telegraf.d/*.conf"]
```

#### 7. Start Telegraf Service

```bash
sudo systemctl enable telegraf
sudo systemctl start telegraf
sudo systemctl status telegraf
```

### Configuration Notes

- **Community Strings**: Replace `campus5g-mgmt` with your actual SNMP community string
- **Switch IPs**: Update the `agents` list with your switch IP addresses
- **OIDs**: Customize the Object Identifiers (OIDs) based on your specific switch models and required metrics
- **Security**: Consider using SNMPv3 for production environments with authentication and encryption

### Troubleshooting

- Check Telegraf logs: `sudo journalctl -u telegraf -f`
- Verify SNMP connectivity: `snmpwalk -v2c -c <community> <switch_ip> 1.3.6.1.2.1.1`
- Test specific OIDs: `snmpget -v2c -c <community> <switch_ip> <oid>`

### Future TODO
- make influx run as a service
- add the influx credentials as a config map to the yaml for easy updating
