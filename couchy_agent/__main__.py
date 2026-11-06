"""python -m couchy_agent pair | keys

Run it from the pack's folder (custom_nodes/couchy-agent), as the account
ComfyUI runs as. It finds ComfyUI's user folder two levels up, as ComfyUI's
default; pass --user-directory when ComfyUI was started with one."""

import argparse
import os
import sys

from .store import CODE_TTL, Store, format_code


def _default_user_dir():
    pack = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(os.path.dirname(os.path.dirname(pack)), "user")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m couchy_agent")
    p.add_argument("--user-directory", default=None, help="ComfyUI's user folder (default: ComfyUI/user)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pair", help="print a fresh pairing code, replacing any waiting one")
    sub.add_parser("keys", help="list namespaces and key ids (never the keys)")
    args = p.parse_args(argv)

    user_dir = args.user_directory or _default_user_dir()
    if not os.path.isdir(user_dir):
        print(f"no ComfyUI user folder at {user_dir}; pass --user-directory", file=sys.stderr)
        return 2
    os.umask(0o077)
    store = Store(user_dir)

    if args.cmd == "pair":
        code = store.mint_code()
        print(format_code(code))
        print(f"Valid for {CODE_TTL // 3600} hours, once. Enter it in Couchy: Settings > Servers > Storage > Pair.",
              file=sys.stderr)
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
                    print(f"  {k['id']}  {k['mode']}  {k['label']}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
