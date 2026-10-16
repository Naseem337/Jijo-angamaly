"""Check that a client's LLM providers answer, using the real keys in .env.

    python -m app.providers.llm --client truechoice              # the whole chain
    python -m app.providers.llm --client truechoice --each       # every provider separately
"""

from __future__ import annotations

import argparse
import sys
import time

from app.core.client_config import load_client_config
from app.providers.llm import ChatMessage, LLMError, LLMNotConfigured, _build_one, build_llm
from app.settings import EnvSecretResolver, get_settings

PROMPT = "Reply with one short friendly sentence greeting a car buyer in Kerala."


def _ping(provider) -> bool:
    started = time.perf_counter()
    try:
        reply = provider.complete("You are a test assistant.", [ChatMessage.user(PROMPT)], max_tokens=200)
    except LLMError as exc:
        print(f"  FAILED  {exc}")
        return False
    ms = (time.perf_counter() - started) * 1000
    print(f"  OK      {reply.provider} ({reply.model}, {ms:.0f} ms, "
          f"{reply.usage.input_tokens}+{reply.usage.output_tokens} tokens): {reply.text.strip()}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.providers.llm")
    parser.add_argument("--client", required=True)
    parser.add_argument("--each", action="store_true", help="test every provider in the chain separately")
    args = parser.parse_args(argv)

    sys.stdout.reconfigure(encoding="utf-8")
    config = load_client_config(args.client, get_settings().clients_dir)
    secrets = EnvSecretResolver()
    chain = config.providers.llm_chain
    print(f"Chain for {args.client}: {' -> '.join(chain)}")

    if not args.each:
        try:
            return 0 if _ping(build_llm(config, secrets)) else 1
        except LLMNotConfigured as exc:
            print(f"  NOT CONFIGURED  {exc}")
            return 1

    ok = False
    for name in chain:
        print(f"{name}:")
        try:
            ok = _ping(_build_one(name, config.providers.llm[name], secrets)) or ok
        except LLMNotConfigured as exc:
            print(f"  SKIPPED {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
