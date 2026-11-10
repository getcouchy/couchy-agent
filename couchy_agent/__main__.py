"""python -m couchy_agent pair [--models write] | keys | config [--share name=path ...]

Run it from the pack's folder (custom_nodes/couchy-agent), as the account
ComfyUI runs as. It finds ComfyUI's user folder two levels up, as ComfyUI's
default; pass --user-directory when ComfyUI was started with one."""

import argparse
import os
import sys

from .config import Config, parse_share
from .store import CODE_TTL, MODELS_GRANTS, Store, format_code


def _default_user_dir():
    pack = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(os.path.dirname(os.path.dirname(pack)), "user")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m couchy_agent")
    p.add_argument("--user-directory", default=None, help="ComfyUI's user folder (default: ComfyUI/user)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pair = sub.add_parser("pair", help="print a fresh pairing code, replacing any waiting one")
    pair.add_argument("--models", choices=MODELS_GRANTS, default="read",
                      help="the models grant the paired key gets (default read; write lets it pull and delete models)")
    sub.add_parser("keys", help="list namespaces and key ids (never the keys)")
    cfg = sub.add_parser("config", help="show the agent's config, or set its model shares")
    cfg.add_argument("--share", action="append", default=None, metavar="NAME=PATH",
                     help="a model root mounted on this host, in ComfyUI's layout (repeat for more); "
                          "replaces the shares set before")
    cfg.add_argument("--clear-shares", action="store_true", help="remove every share")
    args = p.parse_args(argv)

    user_dir = args.user_directory or _default_user_dir()
    if not os.path.isdir(user_dir):
        print(f"no ComfyUI user folder at {user_dir}; pass --user-directory", file=sys.stderr)
        return 2
    os.umask(0o077)
    store = Store(user_dir)

    if args.cmd == "pair":
        code = store.mint_code(models=args.models)
        print(format_code(code))
        print(f"Valid for {CODE_TTL // 3600} hours, once, with models: {args.models}. "
              "Enter it in Couchy: Settings > Servers > Storage > Pair.", file=sys.stderr)
        return 0

    if args.cmd == "config":
        config = Config(store)
        if args.clear_shares or args.share:
            shares = {}
            for text in args.share or []:
                try:
                    name, path = parse_share(text)
                except ValueError as e:
                    print(e, file=sys.stderr)
                    return 2
                if name in shares:
                    print(f"share {name!r} is given twice", file=sys.stderr)
                    return 2
                if not os.path.isdir(path):
                    print(f"warning: {path} isn't a folder now; the agent lists it once it is", file=sys.stderr)
                shares[name] = path
            config.set_shares(shares)
            print("Saved. The running agent picks it up on its next scan.", file=sys.stderr)
        shares = config.shares()
        if not shares:
            print("shares: none")
        for name, path in sorted(shares.items()):
            print(f"share  {name}={path}")
        return 0

    if args.cmd == "keys":
        names = store.namespaces()
        if not names:
            print("not paired")
            return 0
        keys = store.list_keys()
        for ns, meta in sorted(names.items()):
            print(f"{ns}  {meta.get('label', '')}")
            for k in keys:
                if k["namespace"] == ns:
                    print(f"  {k['id']}  {k['mode']}  models:{k['models']}  {k['label']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
