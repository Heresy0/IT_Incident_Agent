"""One explicit restart smoke test of this project's disposable acceptance container only."""
import json
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from repairs.executors import DockerCLIExecutor

ROOT = Path(__file__).resolve().parents[1]


def main():
    selected = subprocess.run(['docker', 'context', 'show'], capture_output=True, text=True, timeout=10)
    if selected.returncode:
        raise RuntimeError('Docker context不可用。')
    context = selected.stdout.strip()
    result = subprocess.run(['docker', '--context', context, 'compose', '-f', str(ROOT / 'docker-compose.test.yml'),
        '-p', 'it-incident-acceptance', 'ps', '-q', 'postgres'], capture_output=True, text=True, timeout=10)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError('本项目专用临时测试容器未启动。')
    executor = DockerCLIExecutor()
    target = {'container_id': result.stdout.strip(), 'context': context}
    data = executor._request(target, 'GET', '/json')
    if data.get('Config', {}).get('Labels', {}).get('com.docker.compose.project') != 'it-incident-acceptance':
        raise RuntimeError('目标不是本项目临时测试环境，拒绝重启。')
    before = executor.preflight(target, 'restart_service', {})
    executor.execute(target, 'restart_service', {})
    check = None
    for attempt in range(6):
        check = executor.verify(target, 'restart_service', {}, before)
        if check['passed']:
            break
        time.sleep(2)
    summary = {'evaluation_type': 'platform_control_only', 'paid_calls': 0,
        'scope': 'it-incident-acceptance', 'restart_attempts': 1, 'verification': check}
    output = ROOT / 'output/docker-executor-acceptance.json'
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if check and check['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
