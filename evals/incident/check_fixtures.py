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
                assert row["data_version"] == manifest["dataset_version"]
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
        else:
            assert latest["pool_usage"] == 1 and latest["pool_wait"] == 1400 and latest["db_cpu"] < 30
    return len(catalog)


if __name__ == "__main__":
    print(f"{check()} synthetic development fixtures: timeline, units, counterexamples and isolated gold checked. No model quality score.")
