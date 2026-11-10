"""python -m couchy_agent pair [--models write] | keys | config [...] | store --root <dir>

Run it from the pack's folder (custom_nodes/couchy-agent), as the account
ComfyUI runs as. It finds ComfyUI's user folder two levels up, as ComfyUI's
default; pass --user-directory when ComfyUI was started with one. On a
store-mode host, with no ComfyUI, the state lives in
~/.local/state/couchy-agent instead."""

import argparse
import os
import sys

from .config import Config, parse_host, parse_share
from .store import CODE_TTL, MODELS_GRANTS, Store, format_code


STORE_STATE = os.path.join("~", ".local", "state", "couchy-agent")


def _comfy_user_dir():
    pack = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(os.path.dirname(os.path.dirname(pack)), "user")


def _default_user_dir(cmd):
    """ComfyUI's user folder, or store mode's state folder when this isn't
    inside a ComfyUI (always for store itself)."""
    comfy = _comfy_user_dir()
    if cmd != "store" and os.path.isdir(comfy):
        return comfy
    state = os.path.expanduser(STORE_STATE)
    return state if cmd == "store" or os.path.isdir(state) else comfy


def _index_dir(path):
    """An absolute folder for the hash index, made 0700 when missing."""
    if not os.path.isabs(path):
        raise ValueError(f"--index-dir must be absolute: {path!r}")
    path = os.path.normpath(path)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as e:
        raise ValueError(f"can't make {path}: {e.strerror}")
    return path


def _shares(texts):
    """{name: path} from --share flags, or an error message."""
    shares = {}
    for text in texts or []:
        name, path = parse_share(text)
        if name in shares:
            raise ValueError(f"share {name!r} is given twice")
        if not os.path.isdir(path):
            print(f"warning: {path} isn't a folder now; the agent lists it once it is", file=sys.stderr)
        shares[name] = path
    return shares


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
    cfg.add_argument("--allowed-host", action="append", default=None, metavar="HOST",
                     help="a host pulls may fetch from beyond Civitai and Hugging Face, subdomains included "
                          "(repeat for more); replaces the ones set before")
    cfg.add_argument("--clear-allowed-hosts", action="store_true", help="remove every allowed host")
    pickle = cfg.add_mutually_exclusive_group()
    pickle.add_argument("--allow-pickle", action="store_true", default=None,
                        help="let pulls fetch .ckpt, .pt, .pth and .bin, which run code when ComfyUI loads them")
    pickle.add_argument("--no-allow-pickle", dest="allow_pickle", action="store_false")
    cfg.add_argument("--reserve-gb", type=float, default=None,
                     help="free space a pull must leave on its volume, in GB (default 20)")
    cfg.add_argument("--index-dir", default=None, metavar="PATH",
                     help="keep the models hash index in this folder, such as one beside a models folder on a "
                          "network volume (read when ComfyUI starts)")
    cfg.add_argument("--clear-index-dir", action="store_true", help="keep the index in user/couchy-agent again")
    st = sub.add_parser("store", help="serve the models routes and pairing without ComfyUI (store mode)")
    st.add_argument("--root", required=True, help="the models root, in ComfyUI's layout (checkpoints/, loras/, ...)")
    st.add_argument("--listen", default=None, help="host:port (default 127.0.0.1:8190)")
    st.add_argument("--share", action="append", default=None, metavar="NAME=PATH",
                    help="another model root on this host, for this run (default: the config's shares)")
    st.add_argument("--index-dir", default=None, metavar="PATH",
                    help="keep the models hash index in this folder (default: the config's, else the state folder)")
    args = p.parse_args(argv)

    user_dir = args.user_directory or _default_user_dir(args.cmd)
    os.umask(0o077)
    if args.cmd == "store":
        os.makedirs(user_dir, mode=0o700, exist_ok=True)
    if not os.path.isdir(user_dir):
        print(f"no ComfyUI user folder at {user_dir}; pass --user-directory", file=sys.stderr)
        return 2
    store = Store(user_dir)

    if args.cmd == "store":
        from . import standalone
        if not os.path.isdir(args.root):
            print(f"no folder at {args.root}", file=sys.stderr)
            return 2
        try:
            shares = _shares(args.share) if args.share else None
            standalone.parse_listen(args.listen or standalone.DEFAULT_LISTEN)
            index_dir = _index_dir(args.index_dir) if args.index_dir else None
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        standalone.run(user_dir, args.root, args.listen or standalone.DEFAULT_LISTEN, shares, index_dir)
        return 0

    if args.cmd == "pair":
        code = store.mint_code(models=args.models)
        print(format_code(code))
        print(f"Valid for {CODE_TTL // 3600} hours, once, with models: {args.models}. "
              "Enter it in Couchy: Settings > Servers > Storage > Pair.", file=sys.stderr)
        return 0

    if args.cmd == "config":
        config = Config(store)
        changes = {}
        try:
            if args.clear_shares or args.share:
                changes["shares"] = _shares(args.share)
            if args.clear_allowed_hosts or args.allowed_host:
                changes["allowedHosts"] = [parse_host(h) for h in args.allowed_host or []]
            if args.clear_index_dir:
                changes["indexDir"] = None
            elif args.index_dir:
                changes["indexDir"] = _index_dir(args.index_dir)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 2
        if args.allow_pickle is not None:
            changes["allowPickle"] = args.allow_pickle
        if args.reserve_gb is not None:
            if args.reserve_gb < 0:
                print("--reserve-gb is 0 or more", file=sys.stderr)
                return 2
            changes["reserveBytes"] = int(args.reserve_gb * 10**9)
        if changes:
            config.set(**changes)
            print("Saved. The running agent picks it up on its next scan or pull"
                  + ("; restart ComfyUI for the index dir." if "indexDir" in changes else "."), file=sys.stderr)
        now = config.load()
        if not now["shares"]:
            print("shares: none")
        for name, path in sorted(now["shares"].items()):
            print(f"share  {name}={path}")
        print("allowed-hosts: " + (", ".join(now["allowedHosts"]) or "none (Civitai and Hugging Face only)"))
        print(f"allow-pickle: {'yes' if now['allowPickle'] else 'no'}")
        print(f"reserve: {now['reserveBytes'] / 10**9:g} GB")
        print(f"index-dir: {now['indexDir'] or 'none (user/couchy-agent)'}")
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
