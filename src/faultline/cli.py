"""Entrypoints. One image, four processes.

faultline api        -- webhook, reads, approvals, SSE
faultline worker     -- claims jobs, runs the investigation graph
faultline gateway    -- the credential-isolated tool boundary
faultline demo       -- the whole path in one process, no infrastructure
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from typing import Any

from faultline.config import get_settings
from faultline.logging import configure_logging, get_logger

log = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="faultline")
    sub = parser.add_subparsers(dest="command", required=True)

    api = sub.add_parser("api", help="run the API service")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8080)
    api.add_argument("--reload", action="store_true")

    gateway = sub.add_parser("gateway", help="run the tool gateway")
    gateway.add_argument("--host", default="0.0.0.0")
    gateway.add_argument("--port", type=int, default=8081)

    worker = sub.add_parser("worker", help="run an investigation worker")
    worker.add_argument("--name", default="worker-1")

    demo = sub.add_parser("demo", help="run one investigation end to end, in process")
    demo.add_argument("--approve", action="store_true", help="approve the proposed action")
    demo.add_argument("--json", action="store_true", help="emit the RCA report as JSON")

    ev = sub.add_parser("eval", help="score the agent against the capsule benchmark")
    ev.add_argument(
        "--provider",
        action="append",
        default=None,
        help="repeatable: stub, anthropic, ollama (default: stub)",
    )
    ev.add_argument("--capsule", action="append", default=None, help="repeatable capsule name")
    ev.add_argument("--min-accuracy", type=float, default=None, help="fail below this (CI gate)")
    ev.add_argument("--max-wrong", type=float, default=0.0, help="fail above this (CI gate)")
    ev.add_argument("--export", type=str, default=None, help="write capsules as JSON to this dir")
    ev.add_argument(
        "--no-corpus",
        action="store_true",
        help="run without retrieval, to measure what the corpus contributes",
    )
    ev.add_argument(
        "--retrieval", action="store_true", help="score retrieval itself instead of the agent"
    )

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.environment != "local")

    match args.command:
        case "api":
            import uvicorn

            uvicorn.run(
                "faultline.api.app:app",
                host=args.host,
                port=args.port,
                reload=args.reload,
                log_config=None,
            )
        case "gateway":
            import uvicorn

            uvicorn.run(
                "faultline.gateway.app:app", host=args.host, port=args.port, log_config=None
            )
        case "worker":
            asyncio.run(_run_worker(args.name))
        case "demo":
            return asyncio.run(_run_demo(approve=args.approve, as_json=args.json))
        case "eval":
            return asyncio.run(
                _run_eval(
                    providers=args.provider or ["stub"],
                    capsule_names=args.capsule,
                    min_accuracy=args.min_accuracy,
                    max_wrong=args.max_wrong,
                    export_dir=args.export,
                    use_corpus=not args.no_corpus,
                    retrieval_only=args.retrieval,
                )
            )
    return 0


async def _run_worker(name: str) -> None:
    from faultline.api.deps import build_container
    from faultline.worker.runner import InvestigationWorker, make_registry

    settings = get_settings()
    container = build_container(settings)
    worker = InvestigationWorker(
        settings=settings,
        bus=container.bus,
        repository=container.repository,
        publisher=container.publisher,
        registry=make_registry(settings),
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        # Graceful shutdown: finish the current node, release the job, let the
        # next worker resume from the checkpoint.
        loop.add_signal_handler(sig, worker.stop)

    log.info("worker_started", name=name, backend=settings.backend)
    await worker.run_forever(consumer=name)
    log.info("worker_stopped", name=name)


async def _run_demo(approve: bool, as_json: bool) -> int:
    """The walking skeleton in one process: webhook to RCA to approval to recovery."""
    from faultline.demo import run_demo

    result: dict[str, Any] = await run_demo(approve=approve)
    if as_json:
        json.dump(result, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    return 0


async def _run_eval(
    providers: list[str],
    capsule_names: list[str] | None,
    min_accuracy: float | None,
    max_wrong: float,
    export_dir: str | None,
    use_corpus: bool = True,
    retrieval_only: bool = False,
) -> int:
    from pathlib import Path

    from faultline.eval.capsule import builtin, builtins
    from faultline.eval.runner import (
        RegressionFailure,
        RunConfig,
        assert_no_regression,
        run_suite,
    )
    from faultline.eval.scoring import render, render_detail
    from faultline.worker.runner import build_retriever_for

    settings = get_settings()

    if retrieval_only:
        from faultline.retrieval.dense import OllamaEmbedder
        from faultline.retrieval.evaluate import ablation
        from faultline.retrieval.evaluate import render as render_retrieval

        embedder = (
            OllamaEmbedder(settings.embed_model, host=settings.ollama_host)
            if settings.embed_provider == "ollama"
            else None
        )
        print(render_retrieval(await ablation(embedder)))
        return 0

    capsules = [builtin(n) for n in capsule_names] if capsule_names else builtins()

    if export_dir:
        target = Path(export_dir)
        for capsule in capsules:
            written = capsule.save(target / f"{capsule.name}.json")
            print(f"wrote {written}")
        return 0

    retriever = await build_retriever_for(settings) if use_corpus else None
    cards = await run_suite(providers, capsules=capsules, config=RunConfig(retriever=retriever))
    print(render(cards))
    print(render_detail(cards))

    if min_accuracy is None:
        return 0
    failures = []
    for card in cards:
        try:
            assert_no_regression(card, min_accuracy=min_accuracy, max_wrong=max_wrong)
        except RegressionFailure as exc:
            failures.append(str(exc))
    if failures:
        print("\nREGRESSION:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
