#!/usr/bin/env python3
"""
Check the LLM tier against a running llama-server.

Verifies, in order:
  1. the server answers on llm.local.base_url
  2. the configured model name is accepted
  3. grammar-constrained decoding produces a parseable intent
  4. a representative set of utterances maps to the expected intents

Usage:
    python scripts/check_llm.py
    python scripts/check_llm.py --url http://localhost:8080/v1 --model qwen2.5-3b-instruct

Start the server first, e.g.:
    tools\\llama-b7376-bin-win-cpu-x64\\llama-server.exe ^
        -m models\\llm\\qwen2.5-3b-instruct-q4_k_m.gguf --port 8080 -c 4096
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# (utterance, any acceptable intent). Rule-tier utterances are included to show
# where the local model takes over.
CASES = [
    ("打开记事本", {"open_app"}),
    ("把音量调大 20", {"set_volume"}),
    ("播放音乐", {"media_control"}),
    ("下一首", {"media_control"}),
    ("搜索 Python 教程", {"search_web"}),
    ("读取文件 C:/notes.txt", {"read_file"}),
    ("运行脚本 test.py", {"run_script"}),
    ("今天天气怎么样", {"get_weather"}),
    ("现在几点了", {"get_time"}),
]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Check the local LLM tier")
    parser.add_argument("--url", default=None, help="override llm.local.base_url")
    parser.add_argument("--model", default=None, help="override llm.local.model")
    parser.add_argument("--config", default="config/config.yaml")
    args = parser.parse_args()

    from winvoice.config import get_config, reset_config
    from winvoice.llm.grammar import INTENT_GRAMMAR
    from winvoice.llm.local import LocalLlmBackend

    reset_config()
    get_config(args.config)

    backend = LocalLlmBackend(base_url=args.url, model=args.model)

    print("=" * 70)
    print("LLM tier check (llama-server)")
    print("=" * 70)
    print(f"  base_url : {backend.base_url}")
    print(f"  model    : {backend.model}")
    print(f"  grammar  : {len(INTENT_GRAMMAR.splitlines())} rules")
    print()

    if not await backend.health_check():
        print("[X] llama-server is not reachable.")
        print("    Start it with, e.g.:")
        print(r"      tools\llama-b7376-bin-win-cpu-x64\llama-server.exe \ "
              "\n"
              r"          -m models\llm\qwen2.5-3b-instruct-q4_k_m.gguf --port 8080 -c 4096")
        return 2

    print("[OK] server reachable")

    passed = 0
    for text, expected in CASES:
        start = time.perf_counter()
        try:
            resp = await backend.complete(text)
        except Exception as e:
            print(f"  [X] {text:22} -> {type(e).__name__}: {e}")
            continue
        ms = (time.perf_counter() - start) * 1000
        ok = resp.intent.value in expected
        passed += int(ok)
        mark = "OK" if ok else "XX"
        print(f"  [{mark}] {text:22} -> {resp.intent.value:14} args={resp.args}  ({ms:.0f} ms)")

    print()
    print(f"  {passed}/{len(CASES)} intents matched")

    if backend._grammar_supported is True:
        print("  grammar-constrained decoding: ACTIVE (GBNF accepted)")
    elif backend._grammar_supported is False:
        print("  grammar-constrained decoding: FELL BACK to json_object")
        print("      -> the server rejected the GBNF; JSON mode still returns JSON")
    else:
        print("  grammar-constrained decoding: not exercised")

    await backend.close()

    if passed == len(CASES):
        print("\n[OK] LLM tier check PASSED")
        return 0
    print("\n[X] LLM tier check FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
