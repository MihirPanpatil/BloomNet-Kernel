import json
import subprocess
import os
import time
import requests
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Form
from pydantic import BaseModel
from neo4j import GraphDatabase

app = FastAPI()

from prometheus_fastapi_instrumentator import Instrumentator
Instrumentator().instrument(app).expose(app)

# Database Config
# Since we share the network namespace with tailscale-kernel (and neo4j), localhost is correct.
NEO4J_URI = "bolt://localhost:7687"
NEO4J_AUTH = ("neo4j", "bloomledger123")

# Metrics Files
TARGETS_MINIO_FILE = "targets_minio.json"
TARGETS_NODE_FILE = "targets_node.json"

# Helper to update target files
def update_target_file(filepath: str, alias: str, new_target: Optional[dict] = None):
    """
    Updates a Prometheus file-based SD JSON file.
    - If new_target is provided, it adds/updates the entry for the alias.
    - If new_target is None, it removes the entry for the alias.
    """
    try:
        data = []
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                try:
                    data = json.load(f)
                    if not isinstance(data, list): data = []
                except json.JSONDecodeError:
                    data = []
        
        # Remove existing entry for this alias
        data = [t for t in data if t.get('labels', {}).get('alias') != alias]

        if new_target:
            data.append(new_target)
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Failed to update {filepath}: {e}")

# [FIX] Robust Database Connection
def get_db_session():
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=NEO4J_AUTH)
        driver.verify_connectivity()
        return driver.session()
    except Exception as e:
        print(f"Neo4j Connection Error: {e}")
        raise HTTPException(status_code=503, detail="Ledger is warming up, please wait.")

# [V2] Pydantic Models
class NodeRegister(BaseModel):
    name: str # e.g. "Node-Alpha"
    ip: str   # e.g. "100.x.y.z"



@app.get("/")
def health_check():
    return {"status": "BloomNet Kernel V2 Online", "version": "2.2"}

def check_minio_health(ip: str, port: int) -> bool:
    """Pings the MinIO health endpoint."""
    url = f"http://{ip}:{port}/minio/health/live"
    try:
        # 2 second timeout to avoid hanging
        resp = requests.get(url, timeout=2)
        return resp.status_code == 200
    except Exception as e:
        print(f"Health check failed for {ip}:{port} : {e}")
        return False

@app.get("/health-check-node")
def health_check_node(ip: str, port: int):
    """
    Manually check the health of a specific MinIO instance.
    """
    is_alive = check_minio_health(ip, port)
    if is_alive:
        return {"status": "online", "target": f"{ip}:{port}"}
    else:
        raise HTTPException(status_code=503, detail=f"Node {ip}:{port} is unreachable")

@app.get("/nodes")
def get_nodes():
    """
    List all registered nodes and their MinIO instances.
    """
    nodes = []
    try:
        with get_db_session() as session:
            # Retrieve all nodes from Neo4j
            result = session.run("MATCH (n:MinIONode) RETURN n.name AS name, n.ip AS ip, n.status AS status")
            for record in result:
                ip = record["ip"]
                nodes.append({
                    "name": record["name"],
                    "ip": ip,
                    "status": record["status"],
                    "instances": [
                        {"type": "Instance A", "port": 9001, "api": f"http://{ip}:9001", "console": f"http://{ip}:9002"},
                        {"type": "Instance B", "port": 9003, "api": f"http://{ip}:9003", "console": f"http://{ip}:9004"}
                    ]
                })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Query Failed: {str(e)}")
    
    return nodes

@app.get("/clusters")
def get_clusters():
    """
    List all formed MinIO clusters and their member nodes.
    """
    clusters = []
    try:
        with get_db_session() as session:
            # Query for Cluster nodes and collect their members
            result = session.run("""
                MATCH (c:MinIOCluster)
                OPTIONAL MATCH (n:MinIONode)-[:MEMBER_OF]->(c)
                RETURN c.name AS cluster_name, c.created_at AS created_at, collect(n) AS members
            """)
            
            for record in result:
                members = []
                for node in record["members"]:
                    if node: # Check if not null
                        members.append({
                            "name": node.get("name"),
                            "ip": node.get("ip"),
                            "status": node.get("status")
                        })
                
                clusters.append({
                    "name": record["cluster_name"],
                    "created_at": record["created_at"],
                    "node_count": len(members),
                    "nodes": members
                })
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Query Failed: {str(e)}")
    
    return clusters

@app.delete("/nodes/{name}")
def delete_node(name: str):
    """
    Unregister a node: Remove from Ledger (Neo4j) and Monitoring (Prometheus).
    """
    # 1. Remove from Neo4j
    try:
        with get_db_session() as session:
            result = session.run("MATCH (n:MinIONode {name: $name}) DETACH DELETE n RETURN count(n) as deleted_count", name=name)
            if result.single()["deleted_count"] == 0:
                 raise HTTPException(status_code=404, detail=f"Node '{name}' not found in registry.")
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Neo4j Delete Failed: {str(e)}")

    # 2. Remove from Prometheus Targets (MinIO and Node)
    update_target_file(TARGETS_MINIO_FILE, name, None)
    update_target_file(TARGETS_NODE_FILE, name, None)

    return {"status": "deleted", "node": name}

from fastapi import FastAPI, HTTPException, Form

# ... imports ...

@app.post("/register-node")
def register_node(name: str = Form(...), ip: str = Form(...), node_port: int = Form(9100)):
    # [V2] Active Health Check
    # We check both Instance A (9001) and Instance B (9003)
    is_a_alive = check_minio_health(ip, 9001)
    is_b_alive = check_minio_health(ip, 9003)

    if not (is_a_alive or is_b_alive):
         print(f"WARNING: Node {name} ({ip}) seems offline or unreachable.")
    
    # 1. Update Neo4j
    try:
        with get_db_session() as session:
            # Store status based on health check
            status = "active" if (is_a_alive and is_b_alive) else "degraded"
            session.run("MERGE (n:MinIONode {ip: $ip}) SET n.name = $name, n.status = $status, n.last_seen = timestamp()", 
                        ip=ip, name=name, status=status)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Neo4j Error: {str(e)}")
    
    # 2. Update Prometheus Targets
    
    # MinIO Targets (Port 9001 and 9003)
    minio_target = {
        "targets": [f"{ip}:9001", f"{ip}:9003"], 
        "labels": {"alias": name}
    }
    update_target_file(TARGETS_MINIO_FILE, name, minio_target)

    # Node Exporter Target (Default 9100)
    node_target = {
        "targets": [f"{ip}:{node_port}"],
        "labels": {"alias": name}
    }
    update_target_file(TARGETS_NODE_FILE, name, node_target)

    return {
        "status": "registered", 
        "node": name, 
        "health": {"instance_a": is_a_alive, "instance_b": is_b_alive},
        "monitoring": {"minio": "enabled", "node": "enabled"}
    }

@app.post("/aliases")
def create_alias(
    alias: str = Form(...),
    ip: str = Form(...),
    port: int = Form(9000),
    user: str = Form("bloomadmin"),
    password: str = Form("bloompassword")
):
    """
    Registers a MinIO alias in the mc configuration.
    Example: mc alias set <alias> http://<ip>:<port> <user> <password>
    """
    # 1. Construct the mc command
    cmd = f"mc alias set {alias} http://{ip}:{port} {user} {password}"
    
    # 2. Execute
    process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    # 3. Handle Result
    if process.returncode != 0:
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to set alias. Error: {process.stderr}"
        )
        
    return {
        "status": "success",
        "alias": alias,
        "target": f"http://{ip}:{port}",
        "message": "Alias configured successfully"
    }

@app.get("/aliases")
def list_aliases():
    """
    Lists all configured MinIO aliases using 'mc alias list --json'.
    """
    cmd = "mc alias list --json"
    process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to list aliases. Error: {process.stderr}"
        )
        
    # parse the JSON output (mc returns multiple JSON objects, one per line)
    aliases = []
    raw_output = process.stdout.strip()
    
    if raw_output:
        for line in raw_output.split('\n'):
            try:
                aliases.append(json.loads(line))
            except json.JSONDecodeError:
                # If a line isn't valid JSON, we skip or log it
                pass
                
    return {
        "count": len(aliases),
        "aliases": aliases
    }

@app.delete("/aliases/{alias}")
def remove_alias(alias: str):
    """
    Removes a MinIO alias configuration.
    Example: mc alias remove <alias>
    """
    cmd = f"mc alias remove {alias}"
    process = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    
    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to remove alias '{alias}'. Error: {process.stderr}"
        )
        
    return {
        "status": "success",
        "alias": alias,
        "message": "Alias removed successfully"
    }

@app.post("/clusters")
def create_cluster(
    name: str = Form(...),
    aliases: str = Form(...) # Comma-separated list of aliases
):
    """
    Forms a generic MinIO cluster from a list of existing aliases.
    1. Resets replication state on all nodes (Clean Slate).
    2. forms site-replication cluster.
    3. Updates Neo4j.
    """
    # Parse aliases from string "a, b, c" -> ["a", "b", "c"]
    alias_list = [a.strip() for a in aliases.split(',') if a.strip()]
    
    if len(alias_list) < 2:
        raise HTTPException(status_code=400, detail="Cluster requires at least 2 aliases.")

    print(f"DEBUG: forming cluster '{name}' with {alias_list}")

    # 1. Reset State (Force remove existing replication)
    for alias in alias_list:
        cmd_reset = f"mc admin replicate rm --all --force {alias}"
        # We ignore errors here because the node might be clean already
        subprocess.run(cmd_reset, shell=True, capture_output=True, text=True)

    # 2. Form Cluster
    cmd_replicate = f"mc admin replicate add {' '.join(alias_list)}"
    res = subprocess.run(cmd_replicate, shell=True, capture_output=True, text=True)

    if res.returncode != 0:
         # Handle "Already exists" gracefully? 
         if "already" not in res.stderr.lower():
             raise HTTPException(status_code=500, detail=f"Cluster formation failed: {res.stderr}")

    # 3. Resolve IPs for Neo4j
    # We need to know which IP belongs to which alias to link them in the DB.
    # We can get this from 'mc alias list'
    alias_map = {} # alias -> ip
    try:
        proc_list = subprocess.run("mc alias list --json", shell=True, capture_output=True, text=True)
        if proc_list.returncode == 0:
            for line in proc_list.stdout.strip().split('\n'):
                try:
                    data = json.loads(line)
                    # data['alias'] and data['URL'] (e.g. http://10.0.0.1:9000)
                    a_name = data.get('alias')
                    url = data.get('URL', '') or data.get('url', '') # Handle both cases
                    if a_name in alias_list and url:
                        # Extract IP: http://1.2.3.4:9000 -> 1.2.3.4
                        # Python's urllib could do this, or simple split
                        ip_part = url.split("://")[-1].split(":")[0]
                        alias_map[a_name] = ip_part
                except:
                    pass
    except Exception as e:
        print(f"Warning: Failed to resolve alias IPs: {e}")

    # 4. Update Neo4j
    try:
        with get_db_session() as session:
            # We use the IPs we found. If we couldn't find one, we might miss a link.
            found_ips = list(alias_map.values())
            
            # ALWAYS create the cluster node, even if we can't link members yet
            session.run("""
                MERGE (c:MinIOCluster {name: $name})
                SET c.created_at = timestamp()
            """, name=name)

            if found_ips:
                session.run("""
                    MATCH (c:MinIOCluster {name: $name})
                    UNWIND $ips AS ip
                    MATCH (n:MinIONode {ip: ip})
                    MERGE (n)-[:MEMBER_OF]->(c)
                """, name=name, ips=found_ips)
                
    except Exception as e:
        print(f"Neo4j Update Failed: {e}")

    return {
        "status": "success",
        "cluster": name,
        "members": alias_list,
        "resolved_ips": alias_map
    }

@app.delete("/clusters/{name}")
def delete_cluster(name: str):
    """
    Deletes a MinIO cluster.
    1. Finds cluster members in Neo4j.
    2. Resolves IPs to local aliases.
    3. Resets replication on all members.
    4. Deletes Cluster entity from Neo4j.
    """
    # 1. Get Cluster Members from Neo4j
    cluster_ips = []
    try:
        with get_db_session() as session:
            # Check if cluster exists explicitly first
            check = session.run("MATCH (c:MinIOCluster {name: $name}) RETURN c", name=name)
            if not check.single():
                raise HTTPException(status_code=404, detail=f"Cluster '{name}' not found.")

            result = session.run("""
                MATCH (c:MinIOCluster {name: $name})<-[:MEMBER_OF]-(n:MinIONode)
                RETURN n.ip as ip
            """, name=name)
            cluster_ips = [record["ip"] for record in result]
                     
    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Query Failed: {e}")

    # 2. Resolve IPs to Aliases
    # We need to run `mc alias list` and find which aliases match the IPs we found.
    alias_map = {} # IP -> Alias
    try:
        proc = subprocess.run("mc alias list --json", shell=True, capture_output=True, text=True)
        if proc.returncode == 0:
            for line in proc.stdout.strip().split('\n'):
                try:
                    data = json.loads(line)
                    alias = data.get('alias')
                    url = data.get('URL', '') or data.get('url', '')
                    if url:
                        # Extract IP: http://1.2.3.4:9000 -> 1.2.3.4
                        # IP is host
                        ip = url.split("://")[-1].split(":")[0]
                        alias_map[ip] = alias
                except:
                    pass
    except Exception as e:
        print(f"Alias resolution failed: {e}")
    
    # 3. Dismantle Cluster (Reset Replication)
    logs = []
    for ip in cluster_ips:
        alias = alias_map.get(ip)
        if alias:
            # Force remove replication config
            cmd = f"mc admin replicate rm --all --force {alias}"
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            logs.append(f"Reset alias '{alias}' (IP {ip}): Return Code {proc.returncode}")
        else:
            logs.append(f"Warning: No local alias found for IP {ip}. Valid manual reset required.")

    # 4. Delete from Neo4j
    try:
         with get_db_session() as session:
            session.run("MATCH (c:MinIOCluster {name: $name}) DETACH DELETE c", name=name)
    except Exception as e:
        print(f"Neo4j Delete Failed: {e}")

    return {
        "status": "deleted", 
        "cluster": name, 
        "member_count": len(cluster_ips),
        "logs": logs
    }

@app.post("/wipe-alias")
def wipe_alias(alias: str = Form(...)):
    """
    Forcefully wipes all data (buckets) from a specific node/alias.
    Use with CAUTION.
    """
    logs = []
    try:
        # Simply run mc rb --force --dangerous on the alias root to wipe all buckets
        cmd_wipe = f"mc rb --force --dangerous {alias}"
        
        proc_wipe = subprocess.run(cmd_wipe, shell=True, capture_output=True, text=True)
        
        if proc_wipe.returncode == 0:
            logs.append(f"Success: {proc_wipe.stdout.strip()}")
        else:
            # If stderr contains "does not exist", it might mean no buckets, which is fine?
            # Or it might mean alias invalid.
            logs.append(f"Result: {proc_wipe.stdout.strip()} {proc_wipe.stderr.strip()}")
            if proc_wipe.returncode != 0 and "does not exist" not in proc_wipe.stderr:
                 raise HTTPException(status_code=500, detail=f"Wipe failed: {proc_wipe.stderr}")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Wipe failed: {str(e)}")

    return {
        "status": "wiped",
        "alias": alias,
        "logs": logs
    }
