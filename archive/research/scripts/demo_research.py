"""Explicit paid demos and read-only replay through the existing research API."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import urllib.request
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
def authorized_request(url, **kwargs):
    token = os.getenv("RESEARCH_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set RESEARCH_ACCESS_TOKEN to your own demo token before making requests")
    headers = kwargs.pop("headers", {})
    return urllib.request.Request(url, headers={**headers, "Authorization": "Bearer " + token}, **kwargs)


CASES = {
    "official-docker": "请联网研究：Dify 是否提供 Docker Compose 自部署方式？只核实这一项，以官方文档或官方 GitHub 为依据，给出来源引用；不要扩展到功能、性能或采购推荐。",
    "local-deploy": "仅使用本地知识库：对比 Dify 与 FastGPT 是否有 Docker Compose 自部署资料。只核实这一项，给出本地笔记依据，保留版本与适用范围限制；不要扩展到成本、性能或采购推荐。",
    "cost-gap": "仅使用本地知识库：请核实 Dify 自部署的整体月运行成本，并判断是否满足每月 2000 元预算。只核实这一项；费用缺失时明确列出，不推测金额，不把开源许可等同于免费运行。",
}


def get_json(url):
    with urllib.request.urlopen(authorized_request(url), timeout=15) as response:
        return json.load(response)


def read_events(response):
    events = []
    for line in response:
        text = line.decode("utf-8").strip()
        if text.startswith("data: "):
            events.append(json.loads(text[6:]))
    return events


def verify_replay(base, run_id, live_events=None):
    before = get_json(f"{base}/runs/{run_id}")
    if before["status"] == "running":
        raise RuntimeError("Replay verification requires a finished run")
    with urllib.request.urlopen(authorized_request(f"{base}/runs/{run_id}/events"), timeout=30) as response:
        replay = read_events(response)
    after = get_json(f"{base}/runs/{run_id}")
    assert replay and replay[-1]["type"] == "final", "missing terminal event"
    assert [event["seq"] for event in replay] == list(range(1, before["next_seq"] + 1)), "event sequence mismatch"
    for key, value in before["result"].items():
        assert replay[-1][key] == value, f"terminal result mismatch: {key}"
    assert after["next_seq"] == before["next_seq"], "replay created new events"
    assert after["result"] == before["result"], "replay changed result or usage"
    if live_events is not None:
        assert live_events == replay, "live and replay events differ"
    return before["result"], replay


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173", help="Frontend proxy or backend URL")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--list", action="store_true", help="Show demo questions; makes no requests")
    actions.add_argument("--case", choices=CASES, help="Create one new run; requires --live")
    actions.add_argument("--run-id", type=UUID, help="Read and verify an existing finished run; GET only")
    parser.add_argument("--live", action="store_true", help="Allow a new run using paid external APIs")
    args = parser.parse_args()
    if args.case and not args.live:
        parser.error("--case requires --live; a new run consumes model/search/embedding credits")
    if args.live and not args.case:
        parser.error("--live is only used with --case")
    if args.list:
        print(json.dumps(CASES, ensure_ascii=False, indent=2))
        return
    base = args.base_url.rstrip("/") + "/api/v1/research"
    payload, events = None, None
    if args.case:
        payload = {"query": CASES[args.case],
                   "thread_id": f"demo-{args.case}", "max_iterations": 0, "enable_memory": False}
        request = authorized_request(base + "/stream", data=json.dumps(payload, ensure_ascii=False).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=240) as response:
            run_id = response.headers.get("X-Research-Run-ID")
            print(json.dumps({"run_id": run_id, "case": args.case}, ensure_ascii=False), flush=True)
            events = read_events(response)
        if not run_id:
            raise RuntimeError("API returned no run ID; do not automatically resubmit")
    else:
        run_id = str(args.run_id)
    result, replay = verify_replay(base, run_id, events)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = ROOT / "output" / "release-demos"
    output.mkdir(parents=True, exist_ok=True)
    label = args.case or "replay"
    prefix = output / f"{stamp}-{label}-{run_id}"
    prefix.with_suffix(".json").write_text(json.dumps({"request": payload, "result": result, "events": replay,
                                                      "replay_verified": True}, ensure_ascii=False, indent=2), encoding="utf-8")
    prefix.with_suffix(".md").write_text(result["final"], encoding="utf-8")
    print(json.dumps({"run_id": run_id, "status": result["status"], "summary": result["run_summary"],
                      "replay_verified": True, "record": str(prefix.with_suffix(".json"))}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
