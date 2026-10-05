#!/usr/bin/env python3
"""Dependency-free helpers for the deploy scripts: .env editing, secrets and password verifiers.

  envtool.py get FILE KEY          print a value (exit 1 if unset)
  envtool.py set FILE              read KEY=VALUE lines on stdin; update or append them in place,
                                   keeping every other line and comment; file mode 600
  envtool.py gen                   print a new 256-bit secret (hex: safe inside URLs and DSNs)
  envtool.py scram                 stdin password -> Postgres SCRAM-SHA-256 verifier
  envtool.py sha256                stdin password -> hex SHA-256 (Redis ACL '#<hash>' form)
  envtool.py dsn-password          stdin DSN -> its password part (empty if none)
  envtool.py libpq-env             stdin DSN -> PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE lines
                                   (for `docker run --env-file <(...)`: no secret in any argv)
  envtool.py split FILE OUTDIR     write OUTDIR/<service>.env from deploy/env/services.toml

Secrets are read from stdin rather than argv so they never show up in `ps`.
"""

import base64
import fnmatch
import hashlib
import hmac
import os
import re
import secrets
import sys
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
SERVICES_TOML = Path(__file__).resolve().parent.parent / "env" / "services.toml"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def parse(path: Path) -> dict[str, str]:
    """KEY -> raw value text (quotes kept, so a copy is byte-identical to the source)."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#"):
            out[m.group(1)] = m.group(2).strip()
    return out


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def cmd_get(path: str, key: str) -> int:
    values = parse(Path(path))
    if key not in values or not _unquote(values[key]):
        return 1
    print(_unquote(values[key]))
    return 0


def cmd_set(path: str) -> int:
    updates: dict[str, str] = {}
    for line in sys.stdin.read().splitlines():
        if not line.strip():
            continue
        m = _LINE.match(line)
        if not m:
            print(f"envtool set: not KEY=VALUE: {line.split('=')[0]!r}", file=sys.stderr)
            return 2
        updates[m.group(1)] = m.group(2).strip()
    p = Path(path)
    lines = p.read_text().splitlines() if p.exists() else []
    seen: set[str] = set()
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m and not line.lstrip().startswith("#") and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            seen.add(m.group(1))
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    _atomic_write(p, "\n".join(lines) + "\n")
    return 0


def scram_verifier(password: str, iterations: int = 4096) -> str:
    """The string Postgres stores for `PASSWORD '...'` under scram-sha-256 (RFC 5802/7677)."""
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def _stdin_secret() -> str:
    value = sys.stdin.read().strip("\n")
    if not value:
        raise SystemExit("envtool: empty secret on stdin")
    return value


def cmd_split(path: str, outdir: str) -> int:
    """One env file per service with only the keys services.toml grants it. Fails closed:
    a key in .env that no section mentions is reported and copied nowhere."""
    source = parse(Path(path))
    spec = tomllib.loads(SERVICES_TOML.read_text())
    out = Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    claimed: set[str] = set()
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    for service, rules in spec.items():
        patterns = rules.get("keys", [])
        mapping: dict[str, str] = rules.get("map", {})
        chosen = {k: v for k, v in source.items() if any(fnmatch.fnmatchcase(k, p) for p in patterns)}
        claimed |= chosen.keys() | set(mapping.values())
        if service == "host":  # documented as host-only: never written to a service file
            continue
        for target, src in mapping.items():
            if src in source:
                chosen[target] = source[src]
            else:
                print(f"split: {service}: {src} not in {path} (needed for {target})", file=sys.stderr)
                return 1
        if "libpq" in rules:  # a DSN key expanded into PGHOST/PGPORT/... for psql-based services
            claimed.add(rules["libpq"])
            if rules["libpq"] not in source:
                print(f"split: {service}: {rules['libpq']} not in {path}", file=sys.stderr)
                return 1
            u = urlsplit(_unquote(source[rules["libpq"]]))
            chosen |= {"PGHOST": u.hostname or "", "PGPORT": str(u.port or 5432),
                       "PGUSER": unquote(u.username or ""), "PGPASSWORD": unquote(u.password or ""),
                       "PGDATABASE": u.path.lstrip("/")}
        body = [f"# Generated by deploy/split-env.sh from {Path(path).name} at {stamp}. Do not edit;",
                "# change .env (or deploy/env/services.toml) and re-run the script.", ""]
        body += [f"{k}={v}" for k, v in sorted(chosen.items())]
        _atomic_write(out / f"{service}.env", "\n".join(body) + "\n")
        print(f"split: wrote {out / f'{service}.env'} ({len(chosen)} keys: {', '.join(sorted(chosen))})")
    unclaimed = sorted(set(source) - claimed)
    if unclaimed:
        print(f"split: WARNING keys in {path} assigned to no service (add them to services.toml "
              f"or [host]): {', '.join(unclaimed)}", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, args = argv[0], argv[1:]
    if cmd == "get" and len(args) == 2:
        return cmd_get(*args)
    if cmd == "set" and len(args) == 1:
        return cmd_set(*args)
    if cmd == "gen" and not args:
        print(secrets.token_hex(32))
        return 0
    if cmd == "scram" and not args:
        print(scram_verifier(_stdin_secret()))
        return 0
    if cmd == "sha256" and not args:
        print(hashlib.sha256(_stdin_secret().encode()).hexdigest())
        return 0
    if cmd == "dsn-password" and not args:
        print(unquote(urlsplit(sys.stdin.read().strip()).password or ""))
        return 0
    if cmd == "libpq-env" and not args:
        u = urlsplit(sys.stdin.read().strip())
        if u.scheme not in ("postgres", "postgresql") or not u.hostname:
            raise SystemExit("envtool libpq-env: not a postgresql:// DSN")
        print(f"PGHOST={u.hostname}\nPGPORT={u.port or 5432}\nPGUSER={unquote(u.username or '')}\n"
              f"PGPASSWORD={unquote(u.password or '')}\nPGDATABASE={u.path.lstrip('/') or 'postgres'}")
        return 0
    if cmd == "split" and len(args) == 2:
        return cmd_split(*args)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
