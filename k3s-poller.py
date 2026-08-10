#!/usr/bin/env python3
"""
Cass HUD — k3s Node Health Poller

Reads node status + metrics from the k8s API using a read-only service account token.
Pushes results to the cass-display socket as a `k3s` command.

Config file: /etc/cass-hud/k3s.conf  (or ./k3s.conf next to this script)
  [k3s]
  token_file = /etc/cass-hud/k3s-token
  kubeconfig = /home/ian/.kube/config   # optional; reads server URL from here
  api_server = https://192.168.0.250:6443  # override if not using kubeconfig

Usage:
  python3 k3s-poller.py            # run once and push to socket
  python3 k3s-poller.py --dry-run  # print JSON without pushing
"""

import json
import os
import socket
import ssl
import sys
import urllib.request
import configparser
import yaml  # pip install pyyaml

SOCKET_PATH   = "/tmp/cass-display.sock"
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATHS  = ["/etc/cass-hud/k3s.conf", os.path.join(SCRIPT_DIR, "k3s.conf")]

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    cfg = configparser.ConfigParser()
    for path in CONFIG_PATHS:
        if os.path.exists(path):
            cfg.read(path)
            break

    section = cfg["k3s"] if "k3s" in cfg else {}

    # Token
    token_file = section.get("token_file", "/etc/cass-hud/k3s-token")
    if not os.path.exists(token_file):
        sys.exit(f"ERROR: token file not found: {token_file}\n"
                  "Run: kubectl -n cass-hud get secret cass-hud-token "
                  "-o jsonpath='{.data.token}' | base64 -d > " + token_file)
    with open(token_file) as f:
        token = f.read().strip()

    # API server — prefer explicit override, then kubeconfig
    api_server = section.get("api_server", "")
    if not api_server:
        kubeconfig_path = section.get(
            "kubeconfig",
            os.environ.get("KUBECONFIG", os.path.expanduser("~/.kube/config"))
        )
        api_server = read_server_from_kubeconfig(kubeconfig_path)

    if not api_server:
        sys.exit("ERROR: could not determine API server. Set api_server in k3s.conf "
                 "or ensure a kubeconfig is present.")

    return token, api_server.rstrip("/")


def read_server_from_kubeconfig(path):
    """Extract the current-context cluster server URL from a kubeconfig."""
    if not os.path.exists(path):
        return None
    with open(path) as f:
        kc = yaml.safe_load(f)

    current_context = kc.get("current-context")
    if not current_context:
        return None

    # Find the cluster name for the current context
    cluster_name = None
    for ctx in kc.get("contexts", []):
        if ctx["name"] == current_context:
            cluster_name = ctx["context"]["cluster"]
            break

    if not cluster_name:
        return None

    for cluster in kc.get("clusters", []):
        if cluster["name"] == cluster_name:
            return cluster["cluster"]["server"]

    return None

# ── k8s API ───────────────────────────────────────────────────────────────────

def k8s_get(api_server, token, path):
    url = f"{api_server}{path}"
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # SA token auth; cluster CA not bundled here

    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
        return json.loads(resp.read())


def parse_quantity(q):
    """Convert k8s resource quantity string to a plain number (bytes or millicores)."""
    if q is None:
        return 0
    q = str(q).strip()
    # Memory suffixes (longest first to avoid prefix matches)
    suffixes = [("Ki", 1024), ("Mi", 1024**2), ("Gi", 1024**3), ("Ti", 1024**4),
                ("K", 1000), ("M", 1000**2), ("G", 1000**3), ("T", 1000**4)]
    for suffix, mult in suffixes:
        if q.endswith(suffix):
            return int(q[:-len(suffix)]) * mult
    # CPU suffixes
    if q.endswith("n"):       # nanocores → millicores
        return int(q[:-1]) // 1_000_000
    if q.endswith("u"):       # microcores → millicores
        return int(q[:-1]) // 1_000
    if q.endswith("m"):       # millicores
        return int(q[:-1])
    # Plain integer: whole cores → millicores, or whole bytes
    return int(q) * 1000


def collect_nodes(api_server, token):
    nodes_raw = k8s_get(api_server, token, "/api/v1/nodes")

    # Try metrics; gracefully degrade if metrics-server not available
    metrics_by_name = {}
    metrics_stale = False
    try:
        metrics_raw = k8s_get(api_server, token, "/apis/metrics.k8s.io/v1beta1/nodes")
        for item in metrics_raw.get("items", []):
            name = item["metadata"]["name"]
            metrics_by_name[name] = item["usage"]
    except Exception as e:
        print(f"WARN: metrics-server unavailable ({e}); marking nodes as stale", file=sys.stderr)
        metrics_stale = True

    nodes = []
    for item in nodes_raw.get("items", []):
        name = item["metadata"]["name"]
        labels = item["metadata"].get("labels", {})

        # Node role
        role = "worker"
        for label in labels:
            if "master" in label or "control-plane" in label:
                role = "server"
                break

        # Ready condition
        status = "unknown"
        for cond in item["status"].get("conditions", []):
            if cond["type"] == "Ready":
                status = "ready" if cond["status"] == "True" else "notready"
                break

        # Allocatable resources
        alloc = item["status"].get("allocatable", {})
        alloc_mem   = parse_quantity(alloc.get("memory"))
        alloc_cpu   = parse_quantity(alloc.get("cpu"))

        # Used resources from metrics
        used = metrics_by_name.get(name, {})
        used_mem  = parse_quantity(used.get("memory"))
        used_cpu  = parse_quantity(used.get("cpu"))

        mem_pct = round((used_mem / alloc_mem * 100) if alloc_mem else 0)
        cpu_pct = round((used_cpu / alloc_cpu * 100) if alloc_cpu else 0)

        # Per-node stale flag: metrics fetch failed entirely, or this node is missing
        node_stale = metrics_stale or (name not in metrics_by_name and not metrics_stale)

        # Shorten name for display (strip domain suffix if present)
        short_name = name.split(".")[0].upper()

        nodes.append({
            "name":    short_name,
            "role":    role,
            "status":  status,
            "mem_pct": mem_pct,
            "cpu_pct": cpu_pct,
            "stale":   node_stale,
        })

    # Sort: servers first, then workers
    nodes.sort(key=lambda n: (0 if n["role"] == "server" else 1, n["name"]))
    return nodes


# ── Socket push ───────────────────────────────────────────────────────────────

def push_to_socket(payload):
    if not os.path.exists(SOCKET_PATH):
        print(f"WARN: socket not found at {SOCKET_PATH}", file=sys.stderr)
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(SOCKET_PATH)
        s.sendall((json.dumps(payload) + "\n").encode())
        resp = s.recv(256)
        return json.loads(resp)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dry_run = "--dry-run" in sys.argv

    token, api_server = load_config()

    try:
        nodes = collect_nodes(api_server, token)
    except Exception as e:
        # Push an error state so the HUD shows something went wrong
        err_payload = {"cmd": "k3s_error", "text": str(e)}
        if dry_run:
            print(json.dumps(err_payload, indent=2))
        else:
            push_to_socket(err_payload)
        sys.exit(1)

    payload = {"cmd": "k3s", "nodes": nodes}

    if dry_run:
        print(json.dumps(payload, indent=2))
    else:
        result = push_to_socket(payload)
        print(f"Socket response: {result}")


if __name__ == "__main__":
    main()
