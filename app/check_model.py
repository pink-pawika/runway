"""Confirm the model wrapper can make one short call.

Prints the provider, model, key location, and token counts. Never prints the key.
The request goes through app.llm.call_llm, which is the only model caller.
"""

from __future__ import annotations

import asyncio

from app.llm import PING_SCHEMA, PING_USER_TEXT, call_llm
from app.model import resolve_model, scrub


def main() -> int:
    choice = resolve_model()
    print(f"Provider: {choice.provider}")
    print(f"Model: {choice.model}")
    print(f"Key source: {choice.key_source}")
    try:
        result = asyncio.run(
            call_llm(
                "ping",
                "Set ok to true.",
                user_text=PING_USER_TEXT,
                json_schema=PING_SCHEMA,
            )
        )
    except Exception as exc:
        print(scrub(f"{type(exc).__name__}: {exc}"))
        return 1
    usage = result["usage"]
    print(f"Reply: {result['data']}")
    print(
        "Tokens: "
        f"input={usage['input_tokens']} "
        f"cached_input={usage['cached_tokens']} "
        f"output={usage['output_tokens']}"
    )
    print(f"Cost USD: {usage['cost_usd']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
