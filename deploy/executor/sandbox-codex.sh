#!/usr/bin/env bash
# Локальный smoke отдельного Codex-исполнителя: без секретов и без сети.
set -euo pipefail
cd "$(dirname "$0")/../.."
img=olymp-executor-codex:ci
name="olymp-codex-sandbox-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker build -f deploy/executor/Containerfile.codex -t "$img" .
want=$(sed -n 's/^ARG CODEX_VERSION=//p' deploy/executor/Containerfile.codex)
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL -e HOME=/tmp --entrypoint codex "$img" --version | grep -F "$want"
docker run -d --init --network none --ipc none --name "$name" --read-only \
  --sysctl kernel.msgmni=0 --sysctl 'kernel.sem=0 0 0 0' --sysctl kernel.shmmni=0 \
  --sysctl kernel.shm_rmid_forced=1 --sysctl fs.mqueue.queues_max=0 \
  --tmpfs /work:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0711 \
  --cap-drop ALL --cap-add SETUID --cap-add SETGID --pids-limit 256 \
  -e EXECUTOR_CHANNEL=codex -e EXECUTOR_SHARED_KEY=ci-placeholder-shared-key "$img" >/dev/null
for _ in $(seq 1 30); do
  if docker exec "$name" python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8099/healthz",timeout=2)' 2>/dev/null; then break; fi
  sleep 1
done
docker exec "$name" python3 -c 'import json,urllib.request; b=json.dumps({"channel":"codex","model":"gpt-6.1-sol","system":"s","messages":[{"role":"user","content":"test"}]}).encode(); req=urllib.request.Request("http://127.0.0.1:8099/run",data=b,headers={"Content-Type":"application/json","X-Executor-Key":"ci-placeholder-shared-key"}); r=json.load(urllib.request.urlopen(req,timeout=30)); assert r.get("error_kind")=="codex_auth_unavailable"'
