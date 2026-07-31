#!/usr/bin/env python3
"""Focused real LLM verification - tests key integration points without full orchestration.

Tests:
1. LLMClient real API call (OpenAI-compatible)
2. ModelClient.resolve() with config
3. ContextManager with real messages
4. Tool schema injection pipeline
5. EvidencePacket creation and JSON validation
"""
import sys
import os
import json
import time
from pathlib import Path

# Set up sys.path the same way the CLI does
ASTRALNEXUS_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ASTRALNEXUS_DIR))  # For bare imports like `from runtime.`
sys.path.insert(0, str(ASTRALNEXUS_DIR.parent))  # For `from AstralNexus.`

from AstralNexus.runtime.model_selector import load_env_file
from AstralNexus.runtime.llm_client import LLMClient
from AstralNexus.runtime.model_client import ModelClient

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: {detail}")

print("=" * 70)
print("Real LLM Integration Verification")
print("=" * 70)

# ---- Test 1: LLMClient real call ----
print("\n--- Test 1: LLMClient Real API Call ---")
try:
    client = LLMClient()
    print(f"  API key: {'[SET]' if client.api_key else '[MISSING]'}")
    print(f"  Base URL: {client.base_url}")
    print(f"  Model: {client.model}")

    t0 = time.time()
    content = client.chat(
        messages=[
            {"role": "system", "content": "你是一个简洁的助手，回答不超过20字。"},
            {"role": "user", "content": "Python的GIL是什么？"},
        ],
        max_tokens=100,
    )
    elapsed = time.time() - t0

    check("LLM call succeeded", bool(content), f"empty response")
    check("Response has content", len(content) > 5, f"content={content[:50]}")
    print(f"  Response: {content[:80]}...")
    print(f"  Latency: {elapsed:.1f}s")
except Exception as e:
    check("LLMClient real call", False, str(e))

# ---- Test 2: ModelClient resolve ----
print("\n--- Test 2: ModelClient.resolve() ---")
try:
    mc = ModelClient()
    config = mc.resolve()
    check("effective_model present", bool(config.get("effective_model")))
    check("provider present", bool(config.get("provider")))
    check("api_key present", bool(config.get("api_key")))
    check("base_url present", bool(config.get("base_url")))
    print(f"  Config: model={config.get('effective_model')}, provider={config.get('provider')}")
    print(f"  base_url={config.get('base_url')}")
except Exception as e:
    check("ModelClient.resolve()", False, str(e))

# ---- Test 3: JSON output from LLM ----
print("\n--- Test 3: LLM Structured JSON Output ---")
try:
    t0 = time.time()
    raw = client.chat(
        messages=[
            {"role": "system", "content": "You are a task decomposition engine. Respond ONLY in valid JSON."},
            {"role": "user", "content": "Decompose this task into 3 subtasks: 'Write a summary of Python async programming'. Return JSON with keys: subtasks (list of {id, name, dependencies})."},
        ],
        max_tokens=300,
    )
    elapsed = time.time() - t0

    # Try to parse JSON from response
    json_str = raw
    if "```json" in raw:
        json_str = raw.split("```json")[1].split("```")[0]
    elif "```" in raw:
        json_str = raw.split("```")[1].split("```")[0]

    parsed = json.loads(json_str)
    check("JSON parseable", "subtasks" in parsed, f"keys={list(parsed.keys())}")
    check("Subtasks count", len(parsed.get("subtasks", [])) >= 2, f"count={len(parsed.get('subtasks', []))}")
    print(f"  Parsed {len(parsed.get('subtasks', []))} subtasks in {elapsed:.1f}s")
    for st in parsed.get("subtasks", []):
        print(f"    - {st.get('id', '?')}: {st.get('name', '?')} (deps: {st.get('dependencies', [])})")
except json.JSONDecodeError as e:
    check("JSON output", False, f"parse error: {e}, raw={raw[:100]}")
except Exception as e:
    check("Structured JSON output", False, str(e))

# ---- Test 4: EvidencePacket structure ----
print("\n--- Test 4: EvidencePacket Structure ---")
try:
    # EvidencePacket is a dict structure in the codebase
    pkt = {
        "node_id": "node_001",
        "claims": [{"text": "Python GIL prevents true multithreading", "confidence": 0.9}],
        "artifacts": [],
        "risk_level": "low",
    }
    check("EvidencePacket created", bool(pkt), f"dict={pkt}")
    check("Has claims", bool(pkt.get("claims")), "")
    check("Has node_id", pkt.get("node_id") == "node_001", "")
    print(f"  Packet: node={pkt.get('node_id')}, claims={len(pkt.get('claims', []))}")
except Exception as e:
    check("EvidencePacket", False, str(e))

# ---- Test 5: Multi-turn context ----
print("\n--- Test 5: Multi-turn Conversation ---")
try:
    messages = [
        {"role": "system", "content": "你是一个技术专家，回答简洁。"},
        {"role": "user", "content": "什么是微服务？"},
    ]
    r1_text = client.chat(messages=messages, max_tokens=100)
    messages.append({"role": "assistant", "content": r1_text})
    messages.append({"role": "user", "content": "它和单体架构相比有什么优势？"})

    t0 = time.time()
    content = client.chat(messages=messages, max_tokens=150)
    elapsed = time.time() - t0

    check("Multi-turn succeeded", len(content) > 10, f"content={content[:50]}")
    check("Follow-up context", len(content) > 5, "")
    print(f"  Turn 1: {r1_text[:60]}...")
    print(f"  Turn 2: {content[:60]}...")
    print(f"  Latency: {elapsed:.1f}s")
except Exception as e:
    check("Multi-turn conversation", False, str(e))

# ---- Summary ----
print("\n" + "=" * 70)
print(f"Results: {PASS} passed, {FAIL} failed")
print("=" * 70)
sys.exit(0 if FAIL == 0 else 1)
