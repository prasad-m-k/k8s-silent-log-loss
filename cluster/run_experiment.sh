#!/usr/bin/env bash
# run_experiment.sh - one cluster run of the evaluation plan (Section 11).
# NOT YET EXECUTED by the author. Requires docker, kind >= 0.23, kubectl.
#
#   ./run_experiment.sh <rate_bytes_per_s> <seconds> [scenario]
#   scenario: rotation (default) | eviction | burst
set -euo pipefail
RATE=${1:-8000000}; SECS=${2:-300}; SCEN=${3:-rotation}
HERE=$(cd "$(dirname "$0")" && pwd); OUT="$HERE/../results/cluster/$(date +%Y%m%dT%H%M%S)-$SCEN-$RATE"
mkdir -p "$OUT"
kind get clusters | grep -q '^sll$' || kind create cluster --name sll --config "$HERE/kind-config.yaml"
kubectl create namespace sll --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sll create configmap loggen-code   --from-file="$HERE/loggen.py"            --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sll create configmap receiver-code --from-file="$HERE/receiver.py"          --dry-run=client -o yaml | kubectl apply -f -
kubectl -n sll create configmap logguard-code --from-file="$HERE/../agent/logguard.py" --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f "$HERE/receiver.yaml" -f "$HERE/fluent-bit.yaml" -f "$HERE/logguard.yaml"
kubectl -n sll rollout status ds/fluent-bit ds/logguard deploy/receiver --timeout=180s
sed "s/value: \"8000000\"/value: \"$RATE\"/" "$HERE/loggen.yaml" | kubectl apply -f -
kubectl -n sll rollout status deploy/loggen --timeout=120s
POD=$(kubectl -n sll get pod -l app=loggen -o jsonpath='{.items[0].metadata.name}')
if [ "$SCEN" = "eviction" ]; then
  # fill nodefs on the worker until the kubelet crosses nodefs.available<10%
  sleep 30
  docker exec sll-worker sh -c 'fallocate -l $(( $(df --output=avail -B1 / | tail -1) - 2*1024*1024*1024 )) /var/fill.bin' || true
fi
sleep "$SECS"
HWM=$(kubectl -n sll exec "$POD" -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://localhost:9102/metrics').read().decode().split()[-1])" || echo "")
kubectl -n sll scale deploy/loggen --replicas=0; sleep 30
RCV=$(kubectl -n sll get pod -l app=receiver -o jsonpath='{.items[0].metadata.name}')
kubectl -n sll cp "$RCV:/data/records.jsonl" "$OUT/records.jsonl"
kubectl -n sll logs ds/logguard > "$OUT/logguard.jsonl"
docker exec sll-worker journalctl -u kubelet --no-pager > "$OUT/kubelet.log" || true
kubectl get events -A -o json > "$OUT/events.json"
docker exec sll-worker rm -f /var/fill.bin || true
python3 "$HERE/analyze_backend.py" "$OUT/records.jsonl" "$OUT/logguard.jsonl" $HWM | tee "$OUT/summary.json"
