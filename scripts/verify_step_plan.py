#!/usr/bin/env python3
"""Verify Step Plan access for Kilo Code (and any OpenAI-compatible client).

Checks, in order:
  1. chat completion against the Step Plan endpoint (default model step-5-preview)
  2. optionally, the StepSearch MCP endpoint's tools/list

Uses only the standard library so it runs in any Python 3 environment.

Usage:
    export STEP_API_KEY="sk-..."
    python3 scripts/verify_step_plan.py                 # chat check, step-5-preview
    python3 scripts/verify_step_plan.py --model step-3.7-flash
    python3 scripts/verify_step_plan.py --max-tokens 8192
    python3 scripts/verify_step_plan.py --mcp            # also probe StepSearch MCP
    python3 scripts/verify_step_plan.py --verbose        # print full error bodies

Env:
    STEP_API_KEY   required. StepFun API key (no "Bearer " prefix).
    STEP_BASE_URL  optional. Defaults to https://api.stepfun.com/step_plan/v1
    STEP_MCP_URL   optional. Defaults to the StepSearch MCP endpoint.

Exit codes: 0 = all requested checks passed, 1 = a check failed, 2 = bad usage/env.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_BASE_URL = "https://api.stepfun.com/step_plan/v1"
DEFAULT_MODEL = "step-5-preview"
DEFAULT_MCP_URL = "https://api.stepfun.com/step_plan/v1/mcp/web_search/mcp"
# Reasoning models (e.g. step-5-preview) spend the output budget on the chain before
# any body text; 16 tokens came back with finish_reason=length and empty content.
DEFAULT_MAX_TOKENS = 2048
TIMEOUT = 60


def _post(url: str, payload: dict, key: str, timeout: int = TIMEOUT) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send_notification(url: str, key: str, timeout: int = TIMEOUT) -> int:
    """POST the MCP 'initialized' notification; 202 with an empty body is the good case."""
    payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _explain(status: int, body: str) -> str:
    if status in (401, 403):
        return (
            "auth rejected: key is missing/invalid, or this key lacks plan/permission "
            "for the model. Check the key in Kilo Code Providers (no 'Bearer ' prefix) "
            "and that the Step Plan subscription is active."
        )
    if status == 404:
        return (
            "endpoint not found: the Base URL should be "
            f"{DEFAULT_BASE_URL} (no trailing path) for the built-in or custom provider."
        )
    if status == 429:
        return "rate limited: back off and retry, or check your plan quota."
    return body[:400] if body else "no response body"


def check_chat(base_url: str, key: str, model: str, prompt: str, max_tokens: int, verbose: bool) -> bool:
    print(f"[1/1] chat completion  base={base_url}  model={model}  max_tokens={max_tokens}")
    try:
        data = _post(
            base_url.rstrip("/") + "/chat/completions",
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0,
            },
            key,
        )
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"  FAIL  HTTP {exc.code}: {_explain(exc.code, body)}")
        if verbose:
            print("  --- body ---")
            print(body[:2000])
        return False
    except urllib.error.URLError as exc:
        print(f"  FAIL  cannot reach {base_url}: {exc.reason}")
        return False

    choices = data.get("choices") or []
    text = ""
    message = {}
    if choices:
        message = choices[0].get("message") or {}
        text = (message.get("content") or "").strip()
    finish = (choices[0].get("finish_reason") if choices else None) or "?"
    usage = data.get("usage") or {}

    if not text:
        # Reasoning models can burn the whole budget on the chain before any body
        # text, which surfaces as an empty content plus finish_reason=length.
        reasoning = message.get("reasoning") or message.get("reasoning_content")
        hint = (
            f"the reasoning chain consumed the budget (finish_reason={finish}, "
            f"usage={usage}). Re-run with a larger --max-tokens, or pick a "
            "non-reasoning model such as step-3.7-flash."
            if reasoning else
            "no content returned at all; re-run with --verbose to inspect the response."
        )
        print(f"  FAIL  empty reply: {hint}")
        if verbose:
            print(json.dumps(data, ensure_ascii=False)[:2000])
        return False

    print(f"  OK    reply={text!r}  finish_reason={finish}  usage={usage}")
    return True


def check_mcp(mcp_url: str, key: str, verbose: bool) -> bool:
    print(f"[mcp] handshake + tools/list  url={mcp_url}")
    try:
        # A real client handshakes first; a bare tools/list can be rejected.
        init = _post(mcp_url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26",
                       "capabilities": {},
                       "clientInfo": {"name": "verify_step_plan", "version": "1.0"}},
        }, key)
        _send_notification(mcp_url, key)
        data = _post(mcp_url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, key)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"  FAIL  HTTP {exc.code}: {_explain(exc.code, body)}")
        if verbose:
            print(body[:2000])
        return False
    except urllib.error.URLError as exc:
        print(f"  FAIL  cannot reach MCP endpoint: {exc.reason}")
        return False

    if "error" in init:
        print(f"  FAIL  initialize rejected: {json.dumps(init['error'], ensure_ascii=False)[:300]}")
        if verbose:
            print(json.dumps(init, ensure_ascii=False)[:2000])
        return False

    tools = [t.get("name") for t in (data.get("result") or {}).get("tools") or []]
    if not tools:
        print(f"  FAIL  no tools returned: {json.dumps(data, ensure_ascii=False)[:400]}")
        return False
    print(f"  OK    initialize={init.get('result', {}).get('protocolVersion', '?')}  "
          f"tools={', '.join(str(t) for t in tools)}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("STEP_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"default: {DEFAULT_MODEL}")
    parser.add_argument("--prompt", default="请只回复 OK。", help='default: "请只回复 OK。"')
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help=f"default: {DEFAULT_MAX_TOKENS} (reasoning models need budget "
                             "for the reasoning chain before any body text)")
    parser.add_argument("--mcp", action="store_true", help="also probe the StepSearch MCP tools/list")
    parser.add_argument("--mcp-url", default=os.environ.get("STEP_MCP_URL", DEFAULT_MCP_URL),
                        help=f"default: {DEFAULT_MCP_URL}")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    key = os.environ.get("STEP_API_KEY", "").strip()
    if not key:
        print("error: STEP_API_KEY is not set. Export it, then retry:\n"
              '  export STEP_API_KEY="sk-..."', file=sys.stderr)
        return 2

    ok = check_chat(args.base_url, key, args.model, args.prompt, args.max_tokens, args.verbose)
    if args.mcp:
        ok = check_mcp(args.mcp_url, key, args.verbose) and ok

    print("\nresult: " + ("all checks passed" if ok else "a check failed"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
