"""Local demo safety helpers shared by replay and live verification."""
from urllib.parse import urlsplit

TOKEN = 'frontend-acceptance-local-operator'


def validate_dsn(dsn):
    from psycopg.conninfo import conninfo_to_dict
    try:
        fields = conninfo_to_dict(dsn)
        valid = (fields.get('dbname', '').endswith('_test')
            and fields.get('host') in {'127.0.0.1', 'localhost', '::1'})
    except Exception:
        valid = False
    if not valid:
        raise ValueError('An explicit loopback *_test database is required; connection details are never printed.')


def validate_origin(value):
    origin = urlsplit(value)
    if (origin.scheme != 'http' or origin.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or origin.username or origin.password or origin.path not in {'', '/'} or origin.query or origin.fragment):
        raise ValueError('Only an explicit loopback HTTP origin is allowed.')
