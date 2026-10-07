"""Offline authoring checks. This evaluator alone may read gold; never sent to Agent."""
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def check():
    manifest = json.loads((ROOT / "evals/incident/manifest.json").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / "fixtures/incident/catalog.json").read_text(encoding="utf-8"))
    assert [r["case_id"] for r in catalog] == manifest["cases"]
    for entry in catalog:
        data = json.loads((ROOT / "fixtures/incident" / entry["file"]).read_text(encoding="utf-8"))
        gold = json.loads((ROOT / "evals/incident/gold" / f"{entry['case_id']}.json").read_text(encoding="utf-8"))
        assert data["synthetic"] and gold["synthetic"]
        assert not {"cause", "gold", "correct_answer"}.intersection(data)
        scope = data["ticket"]["scope"]
        available = json.dumps(data, ensure_ascii=False)
        assert all(term in available for term in gold["necessary_checks"])
        for kind in ("metrics", "logs", "changes", "owners", "runbooks", "incidents"):
            rows = data[kind]
            assert len({r["id"] for r in rows}) == len(rows)
            for row in rows:
                assert row["service"] in {scope["service"], *scope["allowed_dependencies"]}
                assert row["environment"] == scope["environment"]
                assert row["data_version"] == entry.get("data_version", manifest["dataset_version"])
                assert datetime.fromisoformat(row["timestamp"]).tzinfo is not None
        for row in data["metrics"]:
            assert datetime.fromisoformat(scope["start"]) <= datetime.fromisoformat(row["timestamp"]) <= datetime.fromisoformat(scope["end"])
            assert row["unit"] in {"ratio", "percent", "ms", "requests/s"}
            if row["unit"] == "ratio":
                assert 0 <= row["value"] <= 1
            if row["unit"] == "percent":
                assert 0 <= row["value"] <= 100
        latest = {r["metric"]: r["value"] for r in sorted(data["metrics"], key=lambda r: r["timestamp"])}
        if entry["case_id"] == "case_001":
            assert latest["dependency_error_rate"] > .9 and latest["pool_usage"] < .5
        elif entry["case_id"] == "case_002":
            assert latest["dependency_error_rate"] == 0
            assert data["changes"][0]["config_summary"]["auth_audience"] in data["logs"][1]["message"]
        elif entry["case_id"] == "case_003":
            assert latest["pool_usage"] == 1 and latest["pool_wait"] == 1400 and latest["db_cpu"] < 30
        elif entry["case_id"] == "case_004":
            assert 0 < latest["dependency_error_rate"] < .5 and latest["pool_usage"] < .5
            error = next(r for r in data["logs"] if r["error_code"] == "UPSTREAM_READ_TIMEOUT")
            assert error["timestamp"] < data["changes"][0]["timestamp"]
            assert any(r["error_code"] == "UPSTREAM_HEALTH_OK" for r in data["logs"])
            assert data["changes"][0]["config_summary"]["timeout_ms"] == 1500
        elif entry["case_id"] == "case_005":
            assert latest["dependency_error_rate"] == 0 and latest["request_error_rate"] > .6
            config = data["changes"][0]["config_summary"]
            assert config["auth_mode"] == "anonymous" and config["auth_audience"] == "warehouse-staging"
            assert all(value in data["logs"][0]["message"] for value in config.values())
        elif entry["case_id"] == "case_006":
            assert latest["pool_usage"] == 1 and latest["pool_wait"] >= 1600 and latest["db_cpu"] < 30
            load = [r["value"] for r in data["metrics"] if r["metric"] == "request_rate"]
            assert max(load) - min(load) <= 2
            assert data["changes"][0]["category"] == "configuration"
            assert data["changes"][0]["config_summary"]["pool_max"] == 8
    return len(catalog)


if __name__ == "__main__":
    print(f"{check()} synthetic development fixtures: timeline, units, counterexamples and isolated gold checked. No model quality score.")
