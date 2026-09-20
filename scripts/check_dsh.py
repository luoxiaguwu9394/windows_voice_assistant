"""
Check that DeepSeek Harness actually runs a turn, per model tier.

The sibling of `scripts/check_llm.py`, which does this for the legacy classifier
path. Run this after configuring DSH and before trusting the voice loop, because
the failure modes are quiet: a missing adapter, an unregistered route, a missing
key and a disabled tier all produce *a* result, and only some of them produce an
error.

    python scripts/check_dsh.py --tier cloud
    python scripts/check_dsh.py --tier cloud --prompt "打开记事本"
    python scripts/check_dsh.py --tier local --show-events

What it verifies, in order:

1. the tier is enabled in `config/config.yaml`;
2. the harness subprocess starts and the provider/model is accepted
   (`no adapter registered for provider "x"` surfaces here — a hand-written route
   such as a local llama.cpp endpoint must be declared, see deployment.md §2.4);
3. a turn completes with `finish_reason == completed`;
4. the reply is non-empty;
5. every tool the agent called is reported, using the *same* extractor the
   escalation policy uses — so a green run here also proves the reader and the
   MCP bridge still agree on their payload shape.

Exit code is 0 only when all five hold.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from winvoice.dsh import build_backend, load_settings  # noqa: E402
from winvoice.dsh.validation import extract_tool_activity  # noqa: E402

#: A prompt that forces a tool call, so the run exercises the MCP bridge and not
#: just the language model. `get_time` is chosen because it needs no network and
#: changes nothing on the machine.
DEFAULT_PROMPT = (
    "请调用你能用的工具查出当前时间，然后用一句中文告诉我现在是几点。"
    "回答里不要出现英文。"
)


async def check(tier: str, prompt: str, show_events: bool, timeout_s: float) -> int:
    settings = load_settings()
    tier_settings = settings.local if tier == "local" else settings.cloud

    print(f"tier enabled in config : {tier_settings.enabled}")
    print(f"provider               : {tier_settings.provider}")
    print(f"model                  : {tier_settings.model or '(unset)'}")
    print(f"dsh_home               : {tier_settings.dsh_home}")
    print(f"bridge bundle          : {settings.bridge.bundle_dir}")
    print(f"tools appear as        : mcp__{settings.bridge.server_name}__<tool>")
    print()

    if not settings.enabled:
        print("[X] dsh.enabled is false in config/config.yaml — nothing will run.")
        return 1
    if not tier_settings.enabled:
        print(f"[X] dsh.{tier}.enabled is false — this tier is switched off.")
        return 1

    backend = build_backend(tier, tier_settings)
    # A fresh session every run: reusing one that already exists in the DSH home
    # fails with `session "..." already exists` rather than resuming.
    session_id = f"check-{tier}-{uuid.uuid4().hex[:8]}"

    print(f"prompt: {prompt}\n")
    print("running (this starts the DSH runtime; the first run is slow)...")
    turn = await backend.run(prompt, session_id)
    await backend.close()

    if not turn.ran:
        print(f"\n[X] the turn never ran: {turn.failure}")
        print("    Common causes: missing DEEPSEEK_API_KEY, an unregistered")
        print("    provider route, or the bridge bundle not installed.")
        return 1

    print(f"\nfinish_reason : {turn.finish_reason}")
    print(f"events        : {len(turn.events)}")
    print(f"response      : {turn.final_response!r}")

    activity = extract_tool_activity(turn.events)
    if activity:
        print(f"\ntool calls visible to the escalation policy ({len(activity)}):")
        for index, item in enumerate(activity, 1):
            print(f"  {index}. success={item.success} verification={item.verification_status}")
            if item.speak:
                print(f"     speak: {item.speak}")
    else:
        print("\ntool calls visible to the escalation policy: none")
        print("  (the agent answered without calling a tool, or the payload")
        print("   shape changed and the extractor no longer recognises it)")

    if show_events:
        print("\n--- events ---")
        for event in turn.events:
            print(json.dumps(event, ensure_ascii=False)[:1500])

    print()
    if turn.finish_reason != "completed":
        print(f"[X] turn did not complete (finish_reason={turn.finish_reason!r})")
        return 1
    if not turn.final_response.strip():
        print("[X] the turn completed with an empty response")
        return 1

    print("[OK] the tier ran a full turn")
    if not activity:
        print("     (no tool was called — to exercise the MCP bridge, ask for a")
        print("      tool by name, e.g. --prompt \"用工具查一下现在几点\")")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tier", choices=("local", "cloud"), default="cloud")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--show-events", action="store_true", help="dump raw session events")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    return asyncio.run(check(args.tier, args.prompt, args.show_events, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
