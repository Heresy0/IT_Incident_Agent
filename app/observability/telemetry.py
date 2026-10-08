import json
import logging
import os
import re
from collections import defaultdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Lock


class Metrics:
    """Process-local metrics; labels are bounded operation/status names only."""

    def __init__(self):
        self.lock = Lock()
        self.counts = defaultdict(float)
        self.times = defaultdict(list)
        self.active = 0
        self.run_counts = defaultdict(int)
        self.known_tokens = {"input": 0, "output": 0}
        self.errors = defaultdict(int)
        self.repairs = defaultdict(int)
        self.retries = defaultdict(int)

    def finish_run(self, status, summary):
        with self.lock:
            self.run_counts[status] += 1
            usage = summary.get("token_usage", {})
            self.known_tokens["input"] += usage.get("input_tokens", 0)
            self.known_tokens["output"] += usage.get("output_tokens", 0)

    def event(self, event):
        kind = event.get("type", "")
        with self.lock:
            if kind == "warning":
                self.errors[(event.get("code", "INTERNAL_ERROR"), event.get("provider", "workflow"))] += 1
            elif kind == "validation_repair":
                self.repairs[event.get("node", "unknown")] += 1
            elif kind == "retry":
                self.retries[event.get("provider", "unknown")] += 1
            if kind == "call_end":
                labels = (event.get("kind", "node"), event.get("name", "unknown"), event.get("status", "ok"))
                self.counts[labels] += 1
                duration = float(event.get("duration_ms", 0)) / 1000
                # Aggregate, never retain one sample per request indefinitely.
                entry = self.times[labels]
                if not entry:
                    entry.extend([0, 0.0] + [0] * 7)
                entry[0] += 1
                entry[1] += duration
                for i, bound in enumerate((.1, .5, 1, 5, 15, 30, 180)):
                    entry[i + 2] += duration <= bound

    def render(self):
        lines = ["# TYPE it_incident_active_runs gauge", f"it_incident_active_runs {self.active}",
                 "# TYPE it_incident_calls_total counter", "# TYPE it_incident_call_seconds histogram"]
        with self.lock:
            lines.append("# TYPE it_incident_errors_total counter")
            for (code, provider), count in sorted(self.errors.items()):
                lines.append(f'it_incident_errors_total{{code="{code}",provider="{provider}"}} {count}')
            lines.append("# TYPE it_incident_validation_repairs_total counter")
            for node, count in sorted(self.repairs.items()):
                lines.append(f'it_incident_validation_repairs_total{{node="{node}"}} {count}')
            lines.append("# TYPE it_incident_retries_total counter")
            for provider, count in sorted(self.retries.items()):
                lines.append(f'it_incident_retries_total{{provider="{provider}"}} {count}')
            lines.append("# TYPE it_incident_runs_total counter")
            for status, count in sorted(self.run_counts.items()):
                lines.append(f'it_incident_runs_total{{status="{status}"}} {count}')
            lines.append("# TYPE it_incident_known_tokens_total counter")
            for direction, count in self.known_tokens.items():
                lines.append(f'it_incident_known_tokens_total{{direction="{direction}"}} {count}')
            for (kind, name, status), count in sorted(self.counts.items()):
                labels = f'kind="{kind}",name="{name}",status="{status}"'
                lines.append(f"it_incident_calls_total{{{labels}}} {count}")
                entry = self.times[(kind, name, status)]
                for i, bound in enumerate((.1, .5, 1, 5, 15, 30, 180)):
                    lines.append(f'it_incident_call_seconds_bucket{{{labels},le="{bound}"}} {entry[i + 2]}')
                lines.extend([f'it_incident_call_seconds_bucket{{{labels},le="+Inf"}} {entry[0]}',
                              f"it_incident_call_seconds_count{{{labels}}} {entry[0]}",
                              f"it_incident_call_seconds_sum{{{labels}}} {entry[1]}"])
        return "\n".join(lines) + "\n"


METRICS = Metrics()


class JsonFormatter(logging.Formatter):
    def format(self, record):
        message = record.getMessage()
        message = re.sub(r"\x1b\[[0-9;]*m", "", message)
        message = re.sub(r"\bsk-[\w-]+|Bearer\s+\S+", "[redacted]", message)
        # Third-party exception text can contain connection strings.
        message = re.sub(r"(postgres(?:ql)?://)[^@\s]+@", r"\1[redacted]@", message)
        data = {"time": self.formatTime(record), "level": record.levelname, "logger": record.name,
                "message": message[:1200]}
        if hasattr(record, "execution_event"):
            data["event"] = record.execution_event
        if record.exc_info:
            data["exception_type"] = record.exc_info[0].__name__
        return json.dumps(data, ensure_ascii=False, default=str)


def configure_logging():
    root = logging.getLogger()
    if getattr(root, "_it_incident_configured", False):
        return
    log_dir = Path(os.getenv("INCIDENT_LOG_DIR", "logs"))
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = JsonFormatter()
    console = logging.StreamHandler()
    file = RotatingFileHandler(log_dir / "incident.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    for handler in (console, file):
        handler.setFormatter(formatter)
    root.handlers[:] = [console, file]
    root.setLevel(logging.INFO)
    root._it_incident_configured = True


def record_event(event):
    METRICS.event(event)
    logging.getLogger("it_incident.execution").info("execution", extra={"execution_event": event})
