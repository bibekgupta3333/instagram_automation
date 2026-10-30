from __future__ import annotations

import argparse

from .server import serve


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m studio.dashboard", description=__doc__)
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", default="127.0.0.1", help="loopback only; anything else is refused")
    ap.add_argument("--db", default=None, help="pipeline.sqlite (default: STUDIO_PIPELINE_DB or output/pipeline.sqlite)")
    a = ap.parse_args()
    try:
        serve(a.host, a.port, a.db)
    except ValueError as e:
        raise SystemExit(f"refused: {e}") from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
