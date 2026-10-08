"""Run explicit isolated database acceptance; never fall back to the business DB."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from psycopg.conninfo import conninfo_to_dict

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'docker-compose.test.yml'), '-p', 'it-incident-acceptance']
TEST_DSN = 'postgresql://incident_test:incident_test_local@127.0.0.1:5435/incident_test'


def command(args, timeout=120):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=timeout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--start', action='store_true', help='启动专用临时测试库，结束后停止本次创建的容器。')
    args = parser.parse_args()
    dsn = TEST_DSN if args.start else os.getenv('INCIDENT_TEST_POSTGRES_DSN')
    if not dsn:
        print('未配置隔离测试库。设置INCIDENT_TEST_POSTGRES_DSN，或在Docker可用时显式使用--start。')
        return 2
    try:
        if not conninfo_to_dict(dsn).get('dbname', '').endswith('_test'):
            raise ValueError()
    except Exception:
        print('测试DSN必须指向*_test数据库；连接信息不输出。')
        return 2
    created = False
    try:
        if args.start:
            if command(['docker', 'info', '--format', '{{.ServerVersion}}'], 10).returncode:
                print('Docker引擎不可用，未创建容器。')
                return 2
            current = command(COMPOSE + ['ps', '-aq'], 10)
            if current.returncode or current.stdout.strip():
                print('专用测试项目已存在或状态不可读，未覆盖；请核对it-incident-acceptance。')
                return 2
            created = True
            print('启动本项目专用临时PostgreSQL，端口5435。', flush=True)
            if command(COMPOSE + ['up', '-d', '--wait', '--wait-timeout', '90', 'postgres'], 180).returncode:
                print('测试数据库启动失败。')
                return 2
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'app'), INCIDENT_TEST_POSTGRES_DSN=dsn,
                   DASHSCOPE_API_KEY='test-only', PYTHONIOENCODING='utf-8')
        destination = ROOT / 'output/postgres-acceptance.log'
        destination.parent.mkdir(exist_ok=True)
        with destination.open('w', encoding='utf-8') as log:
            result = subprocess.run([sys.executable, '-m', 'unittest', 'tests.storage.test_incident_postgres', '-v'],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120)
        print('数据库验收通过。' if result.returncode == 0 else '数据库验收失败；请查看output/postgres-acceptance.log。')
        return result.returncode
    except (OSError, subprocess.TimeoutExpired):
        print('验收环境不可用或超时，未转用业务数据库。')
        return 2
    finally:
        if created:
            try:
                result = command(COMPOSE + ['down'], 30)
                print('本次临时测试容器已停止。' if result.returncode == 0 else '测试容器清理失败，请核对专用测试项目。')
            except (OSError, subprocess.TimeoutExpired):
                print('测试容器清理未完成，请核对专用测试项目。')


if __name__ == '__main__':
    raise SystemExit(main())
