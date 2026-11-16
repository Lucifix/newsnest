"""python -m demo build OUT [--base /newsnest/demo/]   |   python -m demo serve [--base ...] [--port 8000]"""

import argparse
import sys

from .build import DEFAULT_BASE, BuildError, build, serve


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m demo", description="The read-only NewsNest demo.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="write the demo into a folder")
    b.add_argument("out")
    b.add_argument("--base", default=DEFAULT_BASE, help="path the demo is served under (default %(default)s)")
    s = sub.add_parser("serve", help="build into a temporary folder and serve it")
    s.add_argument("--base", default=DEFAULT_BASE)
    s.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    try:
        if args.command == "build":
            print(f"Built {build(args.out, args.base)} pages into {args.out}")
        else:
            serve(args.base, args.port)
    except BuildError as e:
        print(f"Demo build failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
