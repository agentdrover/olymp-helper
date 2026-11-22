"""Model admission status and operator transitions via the app container."""

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

from app.db import connect
from app.model.admission import Admission

EXECUTORS = ("codex", "cursor")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def worker_status(name):
    key = os.environ.get("EXECUTOR_SHARED_KEY")
    if not key:
        return {"state": "unknown", "active": None}
    url = os.environ.get(f"EXECUTOR_{name.upper()}_URL", f"http://executor-{name}:8099")
    try:
        request = urllib.request.Request(
            url.rstrip("/") + "/status",
            headers={"X-Executor-Key": key},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
        with opener.open(request, timeout=2) as response:
            content = response.read(1025)
            if len(content) > 1024:
                return {"state": "unknown", "active": None}
            report = json.loads(content)
    except OSError:
        return {"state": "unavailable", "active": None}
    except (ValueError, TypeError):
        return {"state": "unknown", "active": None}
    if (
        isinstance(report, dict)
        and type(report.get("version")) is int
        and report["version"] == 1
        and report.get("channel") == name
        and type(report.get("active")) is int
        and report["active"] in (0, 1)
        and report.get("state") in ("ready", "faulty")
    ):
        return {"state": report["state"], "active": report["active"]}
    return {"state": "unknown", "active": None}


def workers_ready(proof) -> bool:
    return all(proof.get(name) == {"state": "ready", "active": 0} for name in EXECUTORS)


def status(admission):
    proof = {name: worker_status(name) for name in EXECUTORS}
    local = admission.status()
    uncertain = local.pop("unknown")
    return {
        "version": 1,
        **local,
        "uncertain": uncertain,
        "workers": proof,
        "drained": local["mode"] == "closed" and workers_ready(proof),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pause", "status", "resume"))
    parser.add_argument("--expect-revision", type=int)
    args = parser.parse_args()
    if (args.action == "resume") != (args.expect_revision is not None):
        parser.error("resume requires --expect-revision")
    path = Path(os.environ.get("OLYMP_DB", "/srv/olymp/olymp.db"))
    if not path.exists():
        print("Model admission database missing.", file=sys.stderr)
        return 1
    conn = connect(path)
    try:
        admission = Admission(conn)
        if args.action == "pause":
            admission.pause()
        outcome = status(admission)
        if args.action == "resume":
            if not workers_ready(outcome["workers"]) or not admission.resume(args.expect_revision):
                print(json.dumps(outcome))
                return 1
            outcome = status(admission)
        print(json.dumps(outcome))
        return 0
    except sqlite3.Error:
        print("Model admission unavailable.", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
