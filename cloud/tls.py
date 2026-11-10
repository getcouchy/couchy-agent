"""TLS mode for direct ports (Vast.ai): read Caddy's adapted JSON config on
stdin and write it to stdout serving HTTPS with exactly the certificate in
COUCHY_TLS_CERT and COUCHY_TLS_KEY (PEM).

start.sh runs it when either variable is set, and pipes the result straight
into Caddy, so the key is never written to disk. A half pair, a certificate
or key that doesn't parse, or a key that doesn't match its certificate exits
64 with one line on stderr, and the container doesn't start: it never falls
back to plain HTTP. The messages never quote the env.

    caddy adapt --config Caddyfile | python3 -I tls.py | caddy run --config /dev/stdin
"""

import json
import os
import ssl
import sys

CERT_ENV = "COUCHY_TLS_CERT"
KEY_ENV = "COUCHY_TLS_KEY"
TAG = "couchy-session"


class Refused(Exception):
    pass


def _pem(name, env):
    value = env.get(name)
    if value is None or not value.strip():
        other = KEY_ENV if name == CERT_ENV else CERT_ENV
        raise Refused(f"{name} is missing or empty but {other} is set: TLS needs both")
    # A provider's env field may not carry newlines: accept them escaped.
    if "\n" not in value and "\\n" in value:
        value = value.replace("\\n", "\n")
    return value.strip() + "\n"


def _memfile(text):
    fd = os.memfd_create("couchy-tls", os.MFD_CLOEXEC)
    os.write(fd, text.encode())
    return fd


def check_pair(cert, key):
    """Raise Refused unless cert is a PEM certificate and key its private key."""
    if "-----BEGIN CERTIFICATE-----" not in cert:
        raise Refused(f"{CERT_ENV} isn't a PEM certificate")
    if "PRIVATE KEY-----" not in key:
        raise Refused(f"{KEY_ENV} isn't a PEM private key")
    cert_fd, key_fd = _memfile(cert), _memfile(key)
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Load the certificate on its own first, so a bad one is named.
        try:
            ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(cadata=cert)
        except (ssl.SSLError, ValueError):
            raise Refused(f"{CERT_ENV} isn't a PEM certificate") from None
        try:
            # A password callback, so an encrypted key fails rather than
            # prompting on a terminal.
            ctx.load_cert_chain(f"/proc/self/fd/{cert_fd}", f"/proc/self/fd/{key_fd}", password=lambda: b"")
        except (ssl.SSLError, ValueError) as e:
            if "mismatch" in str(e).lower():
                raise Refused(f"{KEY_ENV} isn't the private key of {CERT_ENV}") from None
            raise Refused(f"{KEY_ENV} isn't an unencrypted PEM private key for {CERT_ENV}") from None
    finally:
        os.close(cert_fd)
        os.close(key_fd)


def tls_config(config, cert, key):
    """Caddy's JSON config with every server on TLS with exactly this pair."""
    servers = config.get("apps", {}).get("http", {}).get("servers", {})
    if not servers:
        raise Refused("Caddy's config has no server")
    for server in servers.values():
        server["tls_connection_policies"] = [{"certificate_selection": {"any_tag": [TAG]}}]
        # One mapped TCP port: no HTTP/3 listener on UDP.
        server["protocols"] = ["h1", "h2"]
        server["automatic_https"] = {"disable": True}
    config.setdefault("apps", {})["tls"] = {
        "certificates": {"load_pem": [{"certificate": cert, "key": key, "tags": [TAG]}]},
    }
    return config


def main(env=os.environ, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr):
    try:
        if not any((env.get(name) or "").strip() for name in (CERT_ENV, KEY_ENV)):
            raise Refused(f"{CERT_ENV} and {KEY_ENV} are set but empty")
        cert, key = _pem(CERT_ENV, env), _pem(KEY_ENV, env)
        check_pair(cert, key)
        try:
            config = json.load(stdin)
        except ValueError:
            raise Refused("couldn't read Caddy's adapted config") from None
        json.dump(tls_config(config, cert, key), stdout)
    except Refused as e:
        print(f"couchy-cloud: {e}; not starting", file=stderr)
        return 64
    return 0


if __name__ == "__main__":
    sys.exit(main())
