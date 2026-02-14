
# Bloom Kernel

**Bloom Kernel** is the central nervous system for **BloomNet**, a distributed, Tailscale-overlayed MinIO storage network. It serves as a control plane that unifies node management, cluster formation, and observability into a single, cohesive stack.

## 🚀 Overview

The Kernel is designed to run on a primary management node (likely your laptop or a dedicated server) inside your Tailnet. It provides:

*   **Node Registry**: A Neo4j-backed ledger of all active MinIO nodes.
*   **Dynamic Monitoring**: Auto-configuring Prometheus targets for new nodes.
*   **Cluster Orchestration**: A REST API to drive `mc` (MinIO Client) for site-replication.
*   **Visual Observability**: Pre-built Grafana dashboards for health, traffic, and capacity.

## 🏗 Architecture

The system uses a **Sidecar Networking** pattern where all services share the network namespace of the `tailscale-kernel` container. This allows services to communicate over `localhost` while securely exposing interfaces to the Tailnet.

```mermaid
graph TD
    subgraph "Tailscale Kernel Pod"
        TS[tailscale-kernel <br/> (Network Gateway)]
        
        API[Backend API <br/> :8000]
        DB[(Neo4j Ledger <br/> :7687)]
        PROM[Prometheus <br/> :9090]
        GRAF[Grafana <br/> :3000]
        
        TS --- API
        TS --- DB
        TS --- PROM
        TS --- GRAF
        
        API -->|Query/Update| DB
        API -->|Configure| MC[MinIO Client]
        PROM -->|Scrape| API
        PROM -->|Scrape| Nodes[Remote MinIO Nodes]
    end
    
    Nodes -->|Tailscale Tunnel| TS
```

### Components
| Service | Port | Description |
| :--- | :--- | :--- |
| **Backend** | `8000` | FastAPI control plane. Manages state and orchestration. |
| **Grafana** | `3000` | Visualization UI. Login: `bloomadmin` / `bloompassword`. |
| **Neo4j** | `7474` | Graph Database Browser. Auth: `neo4j` / `bloomledger123`. |
| **Prometheus**| `9090` | Time-series metrics engine. |

## 🛠 Prerequisites

1.  **Docker & Docker Compose**: Ensure you have a recent version installed.
2.  **Tailscale Auth Key**: Generate a **Reusable**, **Ephemeral** (optional but recommended for dev), **Tag-enabled** key from your [Tailscale Admin Console](https://login.tailscale.com/admin/settings/keys).
    *   *Note: Tags (e.g., `tag:bloom-kernel`) are recommended to manage ACLs.*

## ⚡️ Quick Start

### 1. Configure Environment
Create a `.env` file in the root directory:
```bash
TAILSCALE_AUTHKEY=tskey-auth-xxxxxx-xxxxxx
```

### 2. Start the Kernel
Run the stack in detached mode:
```bash
docker-compose up -d
```

### 3. Verify Connectivity
Check the logs of the tailscale container to ensure it authenticated:
```bash
docker-compose logs -f tailscale-kernel
```
Once connected, you should see `bloom-kernel` appear in your Tailscale machines list.

## 📖 Usage Guide

### Accessing Interfaces
Since the stack runs on the Tailnet, you can access services via the hostname `bloom-kernel` (if MagicDNS is enabled) or its Tailscale IP.

*   **Grafana**: `http://bloom-kernel:3000`
*   **API Docs**: `http://bloom-kernel:8000/docs`
*   **Neo4j Browser**: `http://bloom-kernel:7474`

### Registering a Node
To add a MinIO node to the network, send a POST request to the API. The Kernel will validate the node and add it to monitoring.

```bash
curl -X POST "http://bloom-kernel:8000/register-node" \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "name=node-01" \
     -d "ip=100.x.y.z" \
     -d "node_port=9100"
```

### Forming a Cluster
Once you have multiple nodes registered, you can link them into a site-replication cluster. ensuring data redundancy.

```bash
# Combine node-01 and node-02 into a cluster named "us-east-cluster"
curl -X POST "http://bloom-kernel:8000/cluster" \
     -d "name=us-east-cluster" \
     -d "aliases=node-01,node-02"
```

## 📂 Project Structure

*   `backend/`: The Python API application.
*   `grafana/`: Dashboard JSON models and provisioning configs.
*   `neo4j/`: Database storage directory.
*   `prometheus/`: Scrape configurations and target files.
*   `tailscale-data/`: Persisted state for the Tailscale socket.

## 🛡 Security Note
This project exposes services directly to your Tailnet. Ensure your Tailscale ACLs restrict access to `tag:bloom-kernel` to trusted users/devices only.
