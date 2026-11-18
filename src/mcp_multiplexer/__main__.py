from __future__ import annotations

import argparse
import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Strict MCP multiplexer")
    parser.add_argument(
        "--config",
        default=os.getenv("MCP_CONFIG", "config/config.json"),
        help="path to multiplexer JSON config",
    )
    parser.add_argument("--host", default=os.getenv("MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MCP_PORT", "8010")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = create_app(args.config)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
