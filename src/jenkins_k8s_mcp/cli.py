from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import Settings
from .core import Engine
from .errors import ToolError
from .store import OperationStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Jenkins and Kubernetes management MCP")
    parser.add_argument("--config", help="YAML configuration; defaults to JENKINS_K8S_MCP_CONFIG")
    subcommands = parser.add_subparsers(dest="command")
    serve = subcommands.add_parser("serve", help="Start the configured MCP server")
    serve.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    subcommands.add_parser("check-config", help="Validate configuration without connecting to platforms")
    subcommands.add_parser("tools", help="Print actual registered tools without connecting to platforms")
    approve = subcommands.add_parser("approve", help="Trusted OS-admin approval of one pending frozen plan")
    approve.add_argument("operation_id")
    approve.add_argument(
        "--yes", action="store_true", help="Explicitly approve the displayed metadata and plan hash"
    )
    args = parser.parse_args()
    engine = None
    try:
        settings = Settings.load(args.config)
        if args.command == "check-config":
            print(
                json.dumps(
                    {"valid": True, "connections": [c.public() for c in settings.connections if c.enabled]},
                    ensure_ascii=False,
                )
            )
            return
        if args.command == "approve":
            store = OperationStore(settings.state_dir)
            row = store.get(args.operation_id)
            print(json.dumps(Engine.public(row), ensure_ascii=False, indent=2))
            if not args.yes:
                parser.error(
                    "approval requires --yes after reviewing the original MCP preview and this plan hash"
                )
            store.approve(args.operation_id)
            print(json.dumps({"approved": args.operation_id}))
            return
        from .server import build_server, create_app

        engine = Engine(settings)
        if args.command == "tools":
            server = build_server(engine)

            async def list_tools():
                tools = await server.list_tools()
                print(
                    json.dumps([tool.model_dump(mode="json") for tool in tools], ensure_ascii=False, indent=2)
                )
                await engine.close()

            asyncio.run(list_tools())
            return
        transport = getattr(args, "transport", "stdio")
        if transport == "streamable-http":
            import uvicorn

            uvicorn.run(
                create_app(engine),
                host=settings.http.host,
                port=settings.http.port,
                log_level="warning",
                access_log=False,
            )
        else:
            server = build_server(engine)

            async def run_stdio():
                try:
                    await server.run_stdio_async()
                finally:
                    await engine.close()

            asyncio.run(run_stdio())
    except ToolError as exc:
        if engine:
            engine.store.close()
        print(json.dumps({"error": exc.as_dict()}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
