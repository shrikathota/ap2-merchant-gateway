#!/usr/bin/env python
"""
agent/buyer_agent.py
======================
Standalone external "AI Buyer Agent" demo, built as a LangGraph state
machine, that plays the role judges actually care about: a real agent
discovering a merchant, deciding what to buy with an LLM, signing AP2
mandates, and completing (or recovering from a failed) purchase.

Pipeline (each stage prints a "PHASE" banner for live narration):

  1. DISCOVERY        GET /.well-known/agent-commerce.json + GET /api/catalog
  2. SKU SELECTION     Gemini 2.5 Flash picks a SKU matching the natural-
                        language goal (skipped in --force-failure mode,
                        where an out-of-stock SKU is deliberately chosen
                        instead, to demonstrate the recovery path).
  3. MANDATE SIGNING   Ed25519-sign an IntentMandate (simulating the human's
                        prior budget authorization) and a CartMandate for the
                        chosen SKU, using the exact Phase 2 signing utilities
                        the merchant itself uses (app.services.mandates).
  4. TRANSACT          POST /api/transact.
       - APPROVED  -> PHASE 5: confirm payment, done.
       - FAILED    -> PHASE 5: recovery — pick the top-ranked alternative
                       the merchant offered, sign a new CartMandate for it,
                       and retry once automatically.
       - DENIED    -> hard failure, no recovery is possible; abort.

Usage
-----
    python agent/buyer_agent.py --goal "running shoes, size 9, under 3000"
    python agent/buyer_agent.py --force-failure
    python agent/buyer_agent.py --goal "..." --base-url http://localhost:8000

Requires GEMINI_API_KEY (or GOOGLE_API_KEY) in the environment for the SKU
selection step, unless --force-failure is used with no prior LLM call needed
for the *first* attempt (the automatic retry after recovery still needs no
LLM call either, since it picks from the merchant's own ranked alternatives)
— i.e. --force-failure alone can run with no API key at all.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Make the merchant's own app/ package importable regardless of cwd, so we
# can reuse the *exact* Phase 2 signing primitives instead of reimplementing
# canonical JSON + Ed25519 signing here.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env")

from app.services.mandates import sign_mandate  # noqa: E402

from langgraph.graph import END, StateGraph  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Console narration helpers
# ---------------------------------------------------------------------------

def phase(label: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\nPHASE: {label}\n{bar}")


def log(msg: str) -> None:
    print(f"  · {msg}")


def ok(msg: str) -> None:
    print(f"  ✅ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠ {msg}")


def fail(msg: str) -> None:
    print(f"  ❌ {msg}")


# ---------------------------------------------------------------------------
# LLM structured output for SKU selection
# ---------------------------------------------------------------------------

class SkuChoice(BaseModel):
    sku: str = Field(..., description="The exact SKU string chosen from the provided catalog")
    category: str = Field(..., description="The exact category string of the chosen SKU, as given in the catalog")
    max_amount_paise: int = Field(
        ...,
        description=(
            "The buyer's budget ceiling in paise, parsed from the goal (e.g. "
            "'under 3000' -> 300000). If no explicit budget is stated, use "
            "1.5x the chosen SKU's price."
        ),
    )
    reasoning: str = Field(..., description="One sentence explaining why this SKU matches the goal")


def pick_sku_with_llm(goal: str, catalog: list[dict], model_name: str) -> SkuChoice:
    from langchain_google_genai import ChatGoogleGenerativeAI

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. Export it before running "
            "without --force-failure, e.g.:  export GEMINI_API_KEY=...  "
            "(get one at https://aistudio.google.com/apikey)"
        )
    os.environ.setdefault("GOOGLE_API_KEY", api_key)

    llm = ChatGoogleGenerativeAI(model=model_name, temperature=0, google_api_key=api_key)
    structured_llm = llm.with_structured_output(SkuChoice)

    catalog_json = json.dumps(catalog, indent=2)
    prompt = (
        "You are a shopping assistant for an AI buyer agent. Given the live product "
        "catalog below and the buyer's natural-language goal, pick EXACTLY ONE SKU "
        "that best satisfies the goal (respect any stated size/category/budget "
        "constraints as best you can from the fields available). You MUST choose a "
        "sku and category value that appears verbatim in the catalog.\n\n"
        f"Catalog:\n{catalog_json}\n\n"
        f"Buyer goal: {goal!r}\n"
    )
    return structured_llm.invoke(prompt)


def fallback_heuristic_pick(catalog: list[dict], goal: str) -> SkuChoice:
    """No-LLM fallback: cheapest in-stock item whose category/name loosely matches the goal."""
    goal_lower = goal.lower()
    in_stock = [p for p in catalog if p["stock_qty"] > 0]
    candidates = [
        p for p in in_stock
        if p["category"].lower() in goal_lower or any(w in p["name"].lower() for w in goal_lower.split())
    ] or in_stock
    if not candidates:
        raise RuntimeError("Catalog has no in-stock products to fall back on")
    chosen = min(candidates, key=lambda p: p["unit_price_paise"])
    return SkuChoice(
        sku=chosen["sku"],
        category=chosen["category"],
        max_amount_paise=int(chosen["unit_price_paise"] * 1.5),
        reasoning="heuristic fallback (no LLM): cheapest matching in-stock item",
    )


# ---------------------------------------------------------------------------
# Mandate construction (Phase 2 signing utilities, reused verbatim)
# ---------------------------------------------------------------------------

def _future(hours: float) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).isoformat()


def build_and_sign_intent(user_priv: Ed25519PrivateKey, *, max_amount_paise: int, categories: list[str]) -> dict:
    payload = {
        "user_id": "demo-buyer-user",
        "max_amount_paise": max_amount_paise,
        "currency": "INR",
        "allowed_categories": categories,
        "expires_at": _future(2),
        "nonce": str(uuid.uuid4()),
    }
    payload["signature"] = sign_mandate(payload, user_priv)
    return payload


def build_and_sign_cart(
    agent_priv: Ed25519PrivateKey,
    *,
    parent_intent_id: str,
    sku: str,
    unit_price_paise: int,
    category: str,
    qty: int = 1,
    agent_id: str = "buyer-agent-demo",
) -> dict:
    payload = {
        "parent_intent_id": parent_intent_id,
        "agent_id": agent_id,
        "sku_list": [
            {"sku": sku, "qty": qty, "unit_price_paise": unit_price_paise, "category": category}
        ],
        "total_amount_paise": qty * unit_price_paise,
        "expires_at": _future(1),
        "nonce": str(uuid.uuid4()),
    }
    payload["signature"] = sign_mandate(payload, agent_priv)
    return payload


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    goal: str
    force_failure: bool
    base_url: str
    gemini_model: str
    client: httpx.Client

    catalog: list[dict]
    chosen: SkuChoice

    user_priv: Ed25519PrivateKey
    user_pub_b64: str
    agent_priv: Ed25519PrivateKey
    agent_pub_b64: str

    intent_payload: dict
    intent_id: str
    cart_payload: dict

    transact_response: dict
    order_id: str
    attempt: int
    outcome: str  # "settled" | "denied" | "error"
    error: str | None


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def node_discover(state: AgentState) -> dict:
    phase("1 · DISCOVERY")
    client = state["client"]
    base_url = state["base_url"]

    manifest = client.get(f"{base_url}/.well-known/agent-commerce.json").raise_for_status().json()
    log(f"discovered merchant: {manifest['merchant_name']!r} (protocol={manifest['protocol']} v{manifest['protocol_version']})")
    log(f"endpoints: {', '.join(manifest['endpoints'].keys())}")

    catalog = client.get(f"{base_url}/api/catalog").raise_for_status().json()
    log(f"catalog: {len(catalog)} products across categories "
        f"{sorted({p['category'] for p in catalog})}")

    return {"catalog": catalog}


def node_pick_sku(state: AgentState) -> dict:
    phase("2 · SKU SELECTION")
    catalog = state["catalog"]

    if state["force_failure"]:
        oos = [p for p in catalog if p["stock_qty"] == 0]
        if not oos:
            warn("no out-of-stock SKU found in catalog for --force-failure; "
                 "falling back to normal selection")
        else:
            target = oos[0]
            log(f"--force-failure: deliberately selecting an OUT-OF-STOCK SKU to trigger recovery")
            chosen = SkuChoice(
                sku=target["sku"],
                category=target["category"],
                max_amount_paise=int(target["unit_price_paise"] * 1.5),
                reasoning="deliberately out-of-stock (--force-failure demo)",
            )
            ok(f"chosen SKU: {chosen.sku!r} (category={chosen.category}, "
               f"budget={chosen.max_amount_paise} paise) — {chosen.reasoning}")
            return {"chosen": chosen}

    goal = state["goal"]
    log(f"goal: {goal!r}")
    try:
        log(f"asking Gemini ({state['gemini_model']}) to pick a matching SKU ...")
        chosen = pick_sku_with_llm(goal, catalog, state["gemini_model"])
    except Exception as exc:
        warn(f"LLM selection failed ({exc}); using heuristic fallback")
        chosen = fallback_heuristic_pick(catalog, goal)

    valid_skus = {p["sku"] for p in catalog}
    if chosen.sku not in valid_skus:
        warn(f"LLM returned unknown sku {chosen.sku!r}; using heuristic fallback")
        chosen = fallback_heuristic_pick(catalog, goal)

    ok(f"chosen SKU: {chosen.sku!r} (category={chosen.category}, "
       f"budget={chosen.max_amount_paise} paise) — {chosen.reasoning}")
    return {"chosen": chosen}


def node_sign_and_register(state: AgentState) -> dict:
    phase("3 · MANDATE SIGNING & INTENT REGISTRATION")
    client = state["client"]
    base_url = state["base_url"]
    catalog = state["catalog"]
    chosen = state["chosen"]

    product = next(p for p in catalog if p["sku"] == chosen.sku)

    user_priv = Ed25519PrivateKey.generate()
    agent_priv = Ed25519PrivateKey.generate()
    import base64
    user_pub_b64 = base64.b64encode(user_priv.public_key().public_bytes_raw()).decode()
    agent_pub_b64 = base64.b64encode(agent_priv.public_key().public_bytes_raw()).decode()
    log("generated Ed25519 keypairs (user = prior human authorization, agent = this buyer agent)")

    max_amount = max(chosen.max_amount_paise, product["unit_price_paise"])
    intent_payload = build_and_sign_intent(
        user_priv, max_amount_paise=max_amount, categories=[chosen.category]
    )
    log(f"signed IntentMandate: max_amount_paise={max_amount}, "
        f"allowed_categories={intent_payload['allowed_categories']}, nonce={intent_payload['nonce'][:8]}…")

    resp = client.post(
        f"{base_url}/api/mandates/intent",
        json={"mandate": intent_payload, "public_key_b64": user_pub_b64},
    )
    if resp.status_code != 201:
        raise RuntimeError(f"intent registration failed: {resp.status_code} {resp.text}")
    intent_id = resp.json()["intent_id"]
    ok(f"IntentMandate registered: intent_id={intent_id}")

    cart_payload = build_and_sign_cart(
        agent_priv,
        parent_intent_id=intent_id,
        sku=chosen.sku,
        unit_price_paise=product["unit_price_paise"],
        category=chosen.category,
    )
    log(f"signed CartMandate: sku={chosen.sku}, total_amount_paise={cart_payload['total_amount_paise']}, "
        f"nonce={cart_payload['nonce'][:8]}…")

    return {
        "user_priv": user_priv,
        "user_pub_b64": user_pub_b64,
        "agent_priv": agent_priv,
        "agent_pub_b64": agent_pub_b64,
        "intent_payload": intent_payload,
        "intent_id": intent_id,
        "cart_payload": cart_payload,
    }


def _post_transact(state: AgentState, cart_payload: dict) -> dict:
    client = state["client"]
    base_url = state["base_url"]
    resp = client.post(
        f"{base_url}/api/transact",
        json={
            "cart_mandate_json": cart_payload,
            "agent_public_key_b64": state["agent_pub_b64"],
            "intent_public_key_b64": state["user_pub_b64"],
        },
    )
    resp.raise_for_status()
    return resp.json()


def node_transact(state: AgentState) -> dict:
    phase(f"4 · TRANSACT (attempt {state.get('attempt', 0) + 1})")
    result = _post_transact(state, state["cart_payload"])
    log(f"POST /api/transact -> status={result['status']} reason={result.get('reason')}")
    return {"transact_response": result, "attempt": state.get("attempt", 0) + 1}


def node_settle(state: AgentState) -> dict:
    phase("5 · SETTLEMENT")
    client = state["client"]
    base_url = state["base_url"]
    result = state["transact_response"]
    order_id = result["razorpay_order_id"]
    ok(f"order APPROVED: razorpay_order_id={order_id}")

    payment_id = f"pay_DEMO_{uuid.uuid4().hex[:12]}"
    resp = client.post(
        f"{base_url}/api/transact/{order_id}/confirm-payment",
        json={"payment_id": payment_id},
    )
    resp.raise_for_status()
    txn = resp.json()
    ok(f"payment confirmed: status={txn['status']} payment_id={payment_id}")
    log(f"view this flow live: GET {base_url}/api/audit/{state['intent_id']}")

    return {"order_id": order_id, "outcome": "settled"}


def node_recover(state: AgentState) -> dict:
    phase("5 · RECOVERY — alternative-recovery path")
    result = state["transact_response"]
    alternatives = result.get("alternatives") or []
    failed_sku = result.get("failed_sku")
    warn(f"transaction FAILED: reason={result['reason']} failed_sku={failed_sku}")

    if not alternatives:
        fail("no alternatives offered — cannot recover")
        return {"outcome": "denied", "error": "no alternatives offered"}

    log(f"merchant offered {len(alternatives)} alternative(s):")
    for alt in alternatives:
        log(f"    - {alt['sku']} · {alt['name']} · ₹{alt['price_paise']/100:.2f} "
            f"· stock={alt['stock_qty']} · {alt['similarity_reason']}")

    top = alternatives[0]
    ok(f"automatically retrying with top-ranked alternative: {top['sku']!r}")

    new_cart = build_and_sign_cart(
        state["agent_priv"],
        parent_intent_id=state["intent_id"],
        sku=top["sku"],
        unit_price_paise=top["price_paise"],
        category=state["chosen"].category,
    )
    log(f"signed new CartMandate for {top['sku']!r}: total_amount_paise={new_cart['total_amount_paise']}")

    return {"cart_payload": new_cart}


def node_denied(state: AgentState) -> dict:
    result = state["transact_response"]
    fail(f"transaction DENIED: reason={result.get('reason')} detail={result.get('reason_detail')}")
    return {"outcome": "denied", "error": result.get("reason")}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_after_transact(state: AgentState) -> str:
    status = state["transact_response"]["status"]
    if status == "APPROVED":
        return "settle"
    if status == "FAILED" and state.get("attempt", 0) <= 1:
        return "recover"
    return "denied"


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("discover", node_discover)
    graph.add_node("pick_sku", node_pick_sku)
    graph.add_node("sign_and_register", node_sign_and_register)
    graph.add_node("transact", node_transact)
    graph.add_node("settle", node_settle)
    graph.add_node("recover", node_recover)
    graph.add_node("denied", node_denied)

    graph.set_entry_point("discover")
    graph.add_edge("discover", "pick_sku")
    graph.add_edge("pick_sku", "sign_and_register")
    graph.add_edge("sign_and_register", "transact")
    graph.add_conditional_edges(
        "transact",
        route_after_transact,
        {"settle": "settle", "recover": "recover", "denied": "denied"},
    )
    graph.add_edge("recover", "transact")
    graph.add_edge("settle", END)
    graph.add_edge("denied", END)

    return graph.compile()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="External AI Buyer Agent demo (LangGraph + Gemini)")
    parser.add_argument(
        "--goal", default="running shoes, size 9, under 3000",
        help="Natural-language shopping goal",
    )
    parser.add_argument(
        "--force-failure", action="store_true",
        help="Deliberately request an out-of-stock SKU first, to demo the recovery path",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Merchant gateway base URL")
    parser.add_argument("--gemini-model", default=DEFAULT_GEMINI_MODEL, help="Gemini model name")
    args = parser.parse_args()

    print("AP2 Buyer Agent — LangGraph demo")
    print(f"target: {args.base_url}   goal: {args.goal!r}   force_failure: {args.force_failure}")

    with httpx.Client(timeout=30) as client:
        graph = build_graph()
        initial_state: AgentState = {
            "goal": args.goal,
            "force_failure": args.force_failure,
            "base_url": args.base_url,
            "gemini_model": args.gemini_model,
            "client": client,
            "attempt": 0,
        }
        try:
            final_state = graph.invoke(initial_state, config={"recursion_limit": 25})
        except Exception as exc:
            fail(f"agent run aborted: {exc}")
            return 1

    print("\n" + "=" * 78)
    if final_state.get("outcome") == "settled":
        print(f"RESULT: ✅ SETTLED — order_id={final_state['order_id']}")
        return 0
    print(f"RESULT: ❌ {final_state.get('outcome', 'unknown').upper()} — {final_state.get('error')}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
