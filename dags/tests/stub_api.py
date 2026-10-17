"""A stub of pipeline-api built from contracts/openapi.yaml, with scriptable outcomes.

It implements the endpoints the DAGs use with canned in-memory state. Every request body is validated against
the contract's request schema (HTTP 422 otherwise) and every response body against its response schema;
mismatches are recorded in `Stub.violations` so a test can assert the stub itself stays contract-conformant.

Job lifecycle: POST creates a Job `queued`; each GET /jobs/{id} advances it queued -> running -> terminal.
Outcomes are scripted per (stage, subject) with `Stub.script(...)`; the default is success.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import jsonschema
import yaml

OPENAPI = yaml.safe_load((Path(__file__).resolve().parents[2] / "contracts" / "openapi.yaml").read_text())
SCENE_STAGES = {
    "fetch": ("discovered", "fetched"),
    "detect": ("fetched", "detected"),
    "validate": ("detected", "validated"),
}
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[10.7, 55.0], [11.3, 55.0], [11.3, 55.7], [10.7, 55.7], [10.7, 55.0]]],
}


def validate(schema_name: str, instance: Any) -> list[str]:
    schema = {"$ref": f"#/components/schemas/{schema_name}", "components": OPENAPI["components"]}
    return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(instance)]


def now() -> str:
    return datetime.now(UTC).isoformat()


class Stub:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.regions = [{"region_id": "great-belt", "name": "Great Belt", "polygon": SQUARE}]
        self.scenes: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.polls: dict[str, int] = {}
        self.new_by_region: dict[str, list[str]] = {}  # Scenes the next discover will register
        self.published_days: set[str] = set()  # AIS days available; others fail as "not published"
        self.loaded_days: set[tuple[str, str]] = set()
        self.outcomes: dict[tuple[str, str], list[str]] = {}
        self.submissions: list[tuple[str, dict[str, Any], int]] = []  # (stage, body, http status)
        self.attempts: dict[tuple[str, str], int] = {}  # Jobs created per (stage, subject)
        self.violations: list[str] = []
        self.fail_next_requests = 0  # answer this many requests with HTTP 503

    # -- scripting -------------------------------------------------------------------------------------------
    def add_scene(
        self, scene_id: str, status: str = "discovered", acquired: str = "2026-10-14T05:32:04Z"
    ) -> None:
        self.scenes[scene_id] = {
            "scene_id": scene_id,
            "region_id": "great-belt",
            "acquired_at": acquired,
            "status": status,
            "failed_stage": None,
            "error": None,
            "footprint": SQUARE,
            "raster_key": None,
            "detection_count": None,
            "discovered_at": now(),
            "updated_at": now(),
        }

    def script(self, stage: str, subject: str, *outcomes: str) -> None:
        """Outcomes ('succeed' | 'fail_retryable' | 'fail_permanent') for successive new Jobs."""
        self.outcomes[(stage, subject)] = list(outcomes)

    def attempts_of(self, stage: str, subject: str) -> int:
        return self.attempts.get((stage, subject), 0)

    def status_codes(self, stage: str) -> list[int]:
        return [code for s, _, code in self.submissions if s == stage]

    # -- behaviour -------------------------------------------------------------------------------------------
    @staticmethod
    def subject(stage: str, body: dict[str, Any]) -> str:
        if stage == "ais-load":
            return f"{body['region_id']}/{body['date']}"
        return str(body.get("scene_id") or body.get("region_id"))

    def post(self, stage: str, body: dict[str, Any]) -> tuple[int, Any]:
        problems = validate(
            {
                "discover": "DiscoverRequest",
                "fetch": "SceneStageRequest",
                "detect": "DetectRequest",
                "ais-load": "AisLoadRequest",
                "validate": "SceneStageRequest",
            }[stage],
            body,
        )
        if problems:
            return 422, {"title": "Invalid", "status": 422, "detail": "; ".join(problems)}
        subject = self.subject(stage, body)
        if stage in ("discover", "ais-load") and body["region_id"] not in {
            r["region_id"] for r in self.regions
        }:
            return 404, {"title": "Unknown region", "status": 404}
        if stage in SCENE_STAGES and body["scene_id"] not in self.scenes:
            return 404, {"title": "Unknown scene", "status": 404}
        for job in self.jobs.values():
            if job["stage"] == stage and job["input"] == body and job["status"] != "failed":
                return 200, job
        if stage in SCENE_STAGES:
            scene = self.scenes[body["scene_id"]]
            if scene["status"] not in (SCENE_STAGES[stage][0], "failed"):
                return 409, {"title": "Wrong status", "status": 409, "detail": f"scene is {scene['status']}"}
        plan = self.outcomes.get((stage, subject), [])
        outcome = plan.pop(0) if plan else "succeed"
        self.attempts[(stage, subject)] = self.attempts_of(stage, subject) + 1
        job = {
            "id": str(uuid.uuid4()),
            "stage": stage,
            "input": body,
            "status": "queued",
            "created_at": now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "error_retryable": None,
            "result": None,
            "_outcome": outcome,
        }
        self.jobs[job["id"]] = job
        self.polls[job["id"]] = 0
        return 202, job

    def advance(self, job: dict[str, Any]) -> None:
        self.polls[job["id"]] += 1
        if job["status"] == "queued":
            job.update(status="running", started_at=now())
        elif job["status"] == "running":
            self.finish(job)

    def finish(self, job: dict[str, Any]) -> None:
        stage, body, outcome = job["stage"], job["input"], job["_outcome"]
        job["finished_at"] = now()
        if stage == "ais-load" and body["date"] not in self.published_days and outcome == "succeed":
            outcome = "fail_retryable"
            job["error"] = f"AIS day {body['date']} not published yet"
        if outcome != "succeed":
            job.update(
                status="failed",
                error=job["error"] or f"scripted {outcome}",
                error_retryable=outcome == "fail_retryable",
            )
            if stage in SCENE_STAGES:
                self.scenes[body["scene_id"]].update(status="failed", failed_stage=stage, error=job["error"])
            return
        job["status"] = "succeeded"
        if stage == "discover":
            ids = self.new_by_region.pop(body["region_id"], [])
            known = len(self.scenes)
            for sid in ids:
                if sid not in self.scenes:  # a Scene the API already knows keeps its status
                    self.add_scene(sid)
            job["result"] = {"new_scene_ids": ids, "known_scene_count": known}
        elif stage == "fetch":
            self.set_scene(body["scene_id"], "fetched", raster_key=f"scenes/{body['scene_id']}/vv.tif")
            job["result"] = {"raster_key": f"scenes/{body['scene_id']}/vv.tif", "bytes_downloaded": 1234}
        elif stage == "detect":
            self.set_scene(body["scene_id"], "detected", detection_count=3)
            job["result"] = {"detection_count": 3, "detector_name": "ca-cfar", "detector_version": "1"}
        elif stage == "validate":
            self.set_scene(body["scene_id"], "validated")
            job["result"] = {"matched": 2, "unmatched": 1, "ambiguous": 0}
        elif stage == "ais-load":
            self.loaded_days.add((body["region_id"], body["date"]))
            job["result"] = {"positions_loaded": 42}

    def set_scene(self, scene_id: str, status: str, **extra: Any) -> None:
        self.scenes[scene_id].update(status=status, failed_stage=None, error=None, updated_at=now(), **extra)

    def list_scenes(self, query: dict[str, list[str]]) -> dict[str, Any]:
        def ready(scene: dict[str, Any]) -> bool:
            return (scene["region_id"], scene["acquired_at"][:10]) in self.loaded_days

        items = sorted(self.scenes.values(), key=lambda s: s["acquired_at"], reverse=True)
        if "status" in query:
            items = [s for s in items if s["status"] == query["status"][0]]
        if "region_id" in query:
            items = [s for s in items if s["region_id"] == query["region_id"][0]]
        if "ais_ready" in query:
            want = query["ais_ready"][0] == "true"
            items = [s for s in items if ready(s) == want]
        offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["100"])[0])
        return {"items": items[offset : offset + limit], "total": len(items)}


def public(job: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in job.items() if not k.startswith("_")}


class Handler(BaseHTTPRequestHandler):
    stub: Stub

    def log_message(self, *args: Any) -> None:
        pass

    def reply(self, code: int, body: Any, schema: str | None = None) -> None:
        if schema and 200 <= code < 300:
            self.stub.violations += [f"{self.path}: {m}" for m in validate(schema, body)]
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        stub = self.stub
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        with stub.lock:
            if stub.fail_next_requests > 0:
                stub.fail_next_requests -= 1
                return self.reply(503, {"title": "unavailable", "status": 503})
            stage = urlparse(self.path).path.removeprefix("/v1/stages/")
            if stage not in {"discover", "fetch", "detect", "ais-load", "validate"}:
                return self.reply(404, {"title": "no such stage", "status": 404})
            code, payload = stub.post(stage, body)
            stub.submissions.append((stage, body, code))
            self.reply(code, public(payload) if code < 300 else payload, "Job" if code < 300 else None)

    def do_GET(self) -> None:
        stub = self.stub
        url = urlparse(self.path)
        path, query = url.path.removeprefix("/v1"), parse_qs(url.query)
        with stub.lock:
            if stub.fail_next_requests > 0:
                stub.fail_next_requests -= 1
                return self.reply(503, {"title": "unavailable", "status": 503})
            if path == "/regions":
                return self.reply(200, stub.regions)
            if path == "/scenes":
                return self.reply(200, stub.list_scenes(query))
            if path.startswith("/jobs/"):
                job = stub.jobs.get(path.removeprefix("/jobs/"))
                if job is None:
                    return self.reply(404, {"title": "no such job", "status": 404})
                stub.advance(job)
                return self.reply(200, public(job), "Job")
            self.reply(404, {"title": "not found", "status": 404})


class StubServer:
    def __init__(self) -> None:
        self.stub = Stub()
        handler = type("BoundHandler", (Handler,), {"stub": self.stub})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self) -> StubServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
