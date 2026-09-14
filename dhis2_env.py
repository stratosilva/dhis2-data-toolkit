"""
Credential loading for the toolkit.

Every server URL, username, password and API token lives in the .env file at the
repository root, which is git-ignored. Nothing in this repository should contain
a literal credential.

.env uses one KEY=VALUE per line. Each DHIS2 server is a prefix with three keys:

    NIGERIA_NCD_URL=https://ncd.dhis2nigeria.org.ng/dhis
    NIGERIA_NCD_USERNAME=some_user
    NIGERIA_NCD_PASSWORD=some_password

Usage:

    from dhis2_env import api_from_env
    api = api_from_env('NIGERIA_NCD')

Real environment variables always win over .env, so a scheduled job or CI runner
can override any value without editing the file. Copy .env.example to .env to
get started.
"""

import os

from dhis2 import Api

ENV_FILENAME = '.env'
EXAMPLE_FILENAME = '.env.example'

# Resolve .env next to this module so scripts work from any working directory.
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(REPO_ROOT, ENV_FILENAME)

_loaded = False


def _parse_env_file(path):
    """Parse a KEY=VALUE file. Supports # comments, blank lines and quoting."""
    values = {}

    with open(path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()

            if not line or line.startswith('#'):
                continue

            # Tolerate the "export KEY=VALUE" form.
            if line.startswith('export '):
                line = line[len('export '):].lstrip()

            key, separator, value = line.partition('=')
            if not separator:
                continue

            key = key.strip()
            value = value.strip()

            # Strip one layer of matching quotes. Unquoted values keep any '#'
            # they contain, because passwords legitimately use it.
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]

            if key:
                values[key] = value

    return values


def load_env(path=None, override=False):
    """
    Load .env into os.environ. Idempotent: repeated calls do nothing.

    Existing environment variables are preserved unless override=True.
    """
    global _loaded

    env_path = path or ENV_PATH

    if _loaded and path is None:
        return

    if not os.path.exists(env_path):
        raise FileNotFoundError(
            f"Credentials file not found: {env_path}\n"
            f"Copy {EXAMPLE_FILENAME} to {ENV_FILENAME} and fill in the values."
        )

    for key, value in _parse_env_file(env_path).items():
        if override or key not in os.environ:
            os.environ[key] = value

    if path is None:
        _loaded = True


def env(name, default=None, required=False):
    """Read a single variable, loading .env on first use."""
    try:
        load_env()
    except FileNotFoundError:
        # A variable already present in the real environment does not need .env.
        if name not in os.environ:
            raise

    value = os.environ.get(name, default)

    if required and not value:
        raise KeyError(
            f"Missing required credential '{name}'. "
            f"Add it to {ENV_PATH} (see {EXAMPLE_FILENAME})."
        )

    return value


def credentials(prefix, require_url=True):
    """Return (url, username, password) for a server prefix such as 'EIDSR'."""
    load_env()

    keys = {suffix: f"{prefix}_{suffix}" for suffix in ('URL', 'USERNAME', 'PASSWORD')}
    required = keys.values() if require_url else (keys['USERNAME'], keys['PASSWORD'])
    missing = [key for key in required if not os.environ.get(key)]

    if missing:
        raise KeyError(
            f"Missing credentials for '{prefix}': {', '.join(missing)}. "
            f"Add them to {ENV_PATH} (see {EXAMPLE_FILENAME})."
        )

    return (
        os.environ.get(keys['URL']),
        os.environ[keys['USERNAME']],
        os.environ[keys['PASSWORD']],
    )


def api_from_env(prefix, url=None, **kwargs):
    """
    Build a dhis2.py Api for a server prefix.

    Pass url= to keep credentials from the prefix while targeting a different
    instance on the same server family.
    """
    base_url, username, password = credentials(prefix, require_url=url is None)
    return Api(url or base_url, username, password, **kwargs)
