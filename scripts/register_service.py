"""Generate a separate read-only registration template, or check configuration offline."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))

from providers.configuration import read_configuration, validate_configuration, ServiceConfigurationError


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, '命令参数无效；请使用 --help 查看用法，参数值不会输出。\n')


def report(issues):
    errors = sum(item['level'] == 'error' for item in issues)
    warnings = len(issues) - errors
    return {'status': 'invalid' if errors else 'valid_with_warnings' if warnings else 'valid',
            'errors': errors, 'warnings': warnings, 'issues': issues,
            'network_checked': False, 'model_calls': 0, 'repairs_performed': False}


def display(result, as_json=False):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"配置预检：{'无效' if result['errors'] else '结构有效'}；错误 {result['errors']}，提示 {result['warnings']}。")
        for item in result['issues']:
            label = '错误' if item['level'] == 'error' else '提示'
            print(f"- [{label}] {item['path']}: {item['message']} ({item['code']})")
        print('本次仅离线检查；未验证连通性、查询语义或服务健康，未调用模型或执行修复。')


def template(args):
    path = ROOT / 'examples' / ('services.enterprise.example.json' if args.profile == 'enterprise'
                                else 'services.example.json')
    data = deepcopy(read_configuration(path))
    row = data['services'][0]
    row.update(tenant_id=args.tenant, users=args.user, service=args.service,
               environment=args.environment, version=args.version)
    # Repair authority is never copied from the example or enabled by template generation.
    row['repairs'] = {}
    for field in ('metrics', 'log_queries'):
        for name, value in row['observations'][field].items():
            if field == 'log_queries':
                row['observations'][field][name] = value.replace('"example-api"', json.dumps(args.service))\
                    .replace('"staging"', json.dumps(args.environment))
            else:
                for query in ('query', 'freshness_query'):
                    if query in value:
                        value[query] = value[query].replace('"example-api"', json.dumps(args.service))\
                            .replace('"staging"', json.dumps(args.environment))
    for source in ('prometheus', 'loki'):
        url = getattr(args, source + '_url')
        if url is not None:
            row['observations'][source] = {'url': url}
    if args.owner_team:
        row['observations']['owners'] = [{'timestamp': datetime.now(timezone.utc).isoformat(),
                                         'team': args.owner_team, 'aliases': [args.service]}]
    return data


def main(argv=None):
    parser = SafeArgumentParser(description='服务登记模板与免费预检；不连接外部系统，不输出配置值或凭据。')
    sub = parser.add_subparsers(dest='command', required=True)
    generate = sub.add_parser('template', help='生成独立的新文件，默认只读，不覆盖已有登记')
    generate.add_argument('--profile', choices=('generic', 'enterprise'), default='generic')
    generate.add_argument('--service', required=True)
    generate.add_argument('--tenant', required=True)
    generate.add_argument('--user', action='append', required=True, help='允许访问的用户，可重复传入')
    generate.add_argument('--environment', choices=('staging', 'production'), default='staging')
    generate.add_argument('--version', required=True)
    generate.add_argument('--owner-team', help='实际维护团队；省略则保留负责人缺口')
    generate.add_argument('--prometheus-url')
    generate.add_argument('--loki-url')
    generate.add_argument('--output', type=Path,
        default=ROOT / 'output/service-registration/services.template.json',
        help='新输出文件，缺省为 output/service-registration/services.template.json；拒绝覆盖')
    check = sub.add_parser('check', help='只读预检指定登记文件')
    check.add_argument('--file', type=Path, help='省略时使用本项目环境配置或 services.local.json')
    check.add_argument('--json', action='store_true', help='输出不含配置值的结构化检查报告')
    args = parser.parse_args(argv)
    try:
        if args.command == 'template':
            data = template(args)
        else:
            path = args.file
            if path is None:
                from dotenv import load_dotenv
                load_dotenv(ROOT / '.env.local', override=False)
                load_dotenv(ROOT / '.env', override=False)
                path = Path(os.getenv('INCIDENT_SERVICES_FILE') or ROOT / 'services.local.json')
                if not path.is_absolute():
                    path = ROOT / path
            data = read_configuration(path)
        result = report(validate_configuration(data))
        if args.command == 'check':
            display(result, args.json)
            return 1 if result['errors'] else 0
        if result['errors']:
            display(result)
            return 1
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8', newline='\n') as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write('\n')
        print('模板已写入指定新文件，未覆盖已有登记，未登记修复动作。')
        print('请调整实际固定查询后预检，再使用 probe_registered_service.py 显式只读试连。')
        display(result)
        return 0
    except FileExistsError:
        print('输出文件已存在，未覆盖；请指定另一个 --output 文件。')
        return 1
    except ServiceConfigurationError as exc:
        display(report(exc.issues), getattr(args, 'json', False))
        return 1
    except OSError:
        print('输出文件无法创建或写入，未输出路径或配置内容。')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
