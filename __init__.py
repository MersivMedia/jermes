"""Hermes plugin entry point for Jermes.

When installed with ``hermes plugins install MersivMedia/jermes`` this repository
root is imported as the plugin package, and Hermes calls ``register(ctx)``.
All logic lives in the inner ``jermes`` package so it is importable and
testable without Hermes.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("jermes")

_HARNESS = None


def register(ctx) -> None:
    global _HARNESS
    from .jermes.cli import register_cli
    from .jermes.harness import Harness

    _HARNESS = Harness()
    h = _HARNESS
    ctx.register_hook("pre_llm_call", h.on_pre_llm_call)
    ctx.register_hook("pre_tool_call", h.on_pre_tool_call)
    ctx.register_hook("transform_tool_result", h.on_transform_tool_result)
    try:
        ctx.register_middleware("llm_request", h.llm_request_middleware)
    except Exception:  # older Hermes without middleware: routing just stays off
        logger.debug("jermes: llm_request middleware unavailable", exc_info=True)

    # Context engine (context trimming). Registering only makes it selectable:
    # Hermes uses it when config.yaml has `context: {engine: jermes}`.
    try:
        from .jermes.context_engine import build_engine, set_shared_engine

        set_shared_engine(h.engine)
        engine = build_engine(_hermes_config())
        if engine is not None and hasattr(ctx, "register_context_engine"):
            ctx.register_context_engine(engine)
    except Exception:
        logger.debug("jermes: context engine unavailable", exc_info=True)
    try:
        ctx.register_cli_command(
            "jermes", "Jev decision layer: status, stats, recent decisions, live check",
            register_cli.setup, register_cli.handle,
        )
    except Exception:
        logger.debug("jermes: CLI registration unavailable", exc_info=True)


def _hermes_config() -> dict:
    try:
        from hermes_cli.config import load_config  # type: ignore

        return load_config() or {}
    except Exception:
        return {}
