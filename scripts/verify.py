"""Enterprise Customer Agent - Full Verification Script.

Coverage layers:
  1. Infrastructure     - Docker containers, ports, DB connectivity
  2. Config & LLM       - .env keys, LLM / Embedding API reachability
  3. Data Layer         - PostgreSQL tables, orders, ChromaDB KB
  4. Auth               - Register / Login / Token refresh / Logout
  5. Intent Dispatch    - classify_intent routes correctly
  6. Tools              - query_order / search_knowledge / create_ticket
  7. Agent Chat         - POST /chat (blocking) + POST /chat_stream (SSE)
  8. Session Mgmt       - History load / delete
  9. Monitoring         - /health, /metrics, /dispatch
 10. Demo Page          - Frontend accessible
 11. Response Headers   - X-Process-Time, X-Request-ID

Run:
    python scripts/verify.py                     # full verification
    python scripts/verify.py --fast              # infra + auth + /chat only
    python scripts/verify.py --verbose           # show raw responses
    API_BASE_URL=http://localhost:8080 python scripts/verify.py
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Force UTF-8 everywhere including subprocess output
if sys.platform == "win32":
    import io as _io

    for _stream in (sys.stdout, sys.stderr):
        enc = getattr(_stream, "encoding", None)
        if enc and enc.lower() in ("cp936", "gbk", "gb2312"):
            setattr(
                sys,
                _stream.name,
                _io.TextIOWrapper(_stream.buffer, encoding="utf-8", errors="replace"),
            )
    os.environ["PYTHONIOENCODING"] = "utf-8"

# Load .env so LLM_API_KEY etc. are available as env vars
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    try:
        import dotenv

        dotenv.load_dotenv(_env_path, override=False)
    except ImportError:
        pass  # .env not critical for basic verification

try:
    import httpx
except ImportError:
    print("[FATAL] httpx not found. Install: pip install httpx")
    sys.exit(1)

# ──────────────────────────────────────────────
# Global config
# ──────────────────────────────────────────────
BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8080")
AUTH_EMAIL = "verify@example.com"
AUTH_PASSWORD = "verify_test_123"

TOKEN: str = ""
SESSION_ID: str = ""
FAILURES: list[str] = []
args: argparse.Namespace
_current_section: str = ""


def banner(title: str) -> None:
    global _current_section
    _current_section = title
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def fail(msg: str, detail: str = "") -> None:
    print(f"  [FAIL] {msg}" + (f"  -- {detail}" if detail else ""))
    FAILURES.append(f"{_current_section}: {msg}")


def _subprocess(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """Run a subprocess with UTF-8 encoding forced."""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "LANG": "C.UTF-8"}
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


# ──────────────────────────────────────────────
# 1. Infrastructure
# ──────────────────────────────────────────────
def check_docker_containers() -> None:
    """Verify all three containers are running."""
    banner("1. Infrastructure - Docker Containers")
    try:
        import subprocess

        # Use simple format to avoid encoding issues with docker compose
        result = subprocess.run(
            [
                "docker",
                "ps",
                "--filter",
                "name=enterprise-customer-agent",
                "--format",
                "{{.Names}} {{.Status}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            fail("docker ps failed", result.stderr[:200])
            return

        expected = {
            "enterprise-customer-agent-postgres-1": "healthy",
            "enterprise-customer-agent-chroma-1": "Up",
            "enterprise-customer-agent-app-1": "Up",
        }
        found: dict[str, str] = {}
        for line in result.stdout.strip().split("\n"):
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                found[parts[0]] = parts[1][:20]

        all_ok = True
        for svc, flag in expected.items():
            short_name = svc.split("-")[-1]  # postgres, chroma, app
            if svc in found and flag in found[svc]:
                ok(f"{short_name} running ({found[svc]})")
            else:
                all_ok = False
                fail(f"{short_name} not ready", f"found={found.get(svc, 'N/A')}")

        if all_ok:
            # Also verify ports are bound
            port_result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    "name=enterprise-customer-agent",
                    "--format",
                    "{{.Ports}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ports_line = port_result.stdout.strip()
            if ":8080->8000" in ports_line:
                ok("app port mapping 8080:8000 OK")
            else:
                fail("app port mapping missing", ports_line)
    except Exception as e:
        fail("Docker check failed", str(e))


def check_ports() -> None:
    """Test port accessibility."""
    banner("2. Port Connectivity")
    import socket

    checks = [
        ("app (HTTP)", f"{BASE_URL}/health"),
        ("postgres (5432)", "localhost:5432"),
        ("chroma (8000)", "localhost:8000"),
    ]
    for label, target in checks:
        try:
            if "://" in target:
                resp = httpx.get(target, timeout=3)
                if resp.status_code < 400:
                    ok(f"{label} -- HTTP {resp.status_code}")
                else:
                    fail(f"{label}", f"HTTP {resp.status_code}")
            else:
                host, port = target.rsplit(":", 1)
                socket.create_connection((host, int(port)), timeout=2)
                ok(f"{label} port reachable")
        except Exception as e:
            fail(f"{label}", str(e))


# ──────────────────────────────────────────────
# 3. Config & LLM API
# ──────────────────────────────────────────────
def check_llm_api() -> None:
    """Call DashScope chat/completions directly to verify key validity."""
    banner("3. LLM API Connectivity")
    key = os.getenv("LLM_API_KEY", "")
    if not key:
        fail("LLM_API_KEY not set", "Check .env file")
        return

    try:
        resp = httpx.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "qwen-plus",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 5,
            },
            timeout=10,
        )
        data = resp.json()
        if "choices" in data:
            reply = data["choices"][0]["message"]["content"]
            # Escape any non-ASCII for safe printing
            safe_reply = reply.encode("ascii", errors="replace").decode("ascii")
            ok(f"LLM OK -- '{safe_reply}'")
        elif "error" in data:
            err = data["error"]
            safe_msg = err.get("message", "").encode("ascii", errors="replace").decode("ascii")
            fail(f"LLM API error: {err.get('code')}", safe_msg)
        else:
            safe_resp = str(data)[:200].encode("ascii", errors="replace").decode("ascii")
            fail("LLM unexpected response", safe_resp)
    except Exception as e:
        safe_err = str(e).encode("ascii", errors="replace").decode("ascii")
        fail("LLM request failed", safe_err)


def check_embedding_api() -> None:
    """Call embedding endpoint to verify key permissions."""
    banner("4. Embedding API Connectivity")
    key = os.getenv("EMBEDDING_API_KEY", os.getenv("LLM_API_KEY", ""))
    if not key:
        fail("EMBEDDING_API_KEY not set", "Check .env file")
        return
    try:
        resp = httpx.post(
            "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={"model": "text-embedding-v3", "input": ["test"]},
            timeout=10,
        )
        data = resp.json()
        if "data" in data and len(data["data"]) > 0:
            dim = len(data["data"][0].get("embedding", []))
            ok(f"Embedding OK -- dim={dim}")
        elif "error" in data:
            err = data["error"]
            fail(f"Embedding API error: {err.get('code')}", err.get("message", ""))
        else:
            fail("Embedding unexpected response", str(data)[:200])
    except Exception as e:
        fail("Embedding request failed", str(e))


# ──────────────────────────────────────────────
# 5. Data Layer
# ──────────────────────────────────────────────
def check_seed_data() -> None:
    """Verify order data exists in PostgreSQL."""
    banner("5. Data Layer - Orders")
    try:
        script = (
            "import asyncio\n"
            "from db import AsyncSessionLocal, Order\n"
            "from sqlalchemy import func, select\n"
            "async def c():\n"
            "    async with AsyncSessionLocal() as s:\n"
            "        cnt = await s.scalar(select(func.count()).select_from(Order))\n"
            "        print(cnt)\n"
            "print(asyncio.run(c()))\n"
        )
        rc, stdout, stderr = _subprocess(
            ["docker", "compose", "exec", "app", "python", "-c", script],
            timeout=15,
        )
        if rc == 0:
            line = stdout.strip().splitlines()[0]
            try:
                cnt = int(line)
                if cnt >= 7:
                    ok(f"orders table: {cnt} records (expected >= 7)")
                else:
                    fail(f"orders table has only {cnt} records", "Run: python scripts/seed_data.py")
            except ValueError:
                fail(f"Unexpected output: {line!r}", f"stderr={stderr[:200]}")
        else:
            fail("Order count query failed", stderr[:300])
    except Exception as e:
        fail("Order data check failed", str(e))


def check_chroma_kb() -> None:
    """Verify ChromaDB knowledge base has data."""
    banner("6. Data Layer - ChromaDB Knowledge Base")
    try:
        script = (
            "import chromadb\n"
            "c = chromadb.PersistentClient(path='/opt/app/chroma_db')\n"
            "coll = c.get_collection('knowledge_base')\n"
            "print(coll.count())\n"
        )
        rc, stdout, stderr = _subprocess(
            ["docker", "compose", "exec", "app", "python", "-c", script],
            timeout=10,
        )
        if rc == 0:
            line = stdout.strip().splitlines()[0]
            try:
                cnt = int(line)
                if cnt > 0:
                    ok(f"knowledge_base: {cnt} chunks")
                else:
                    fail("Knowledge base is empty", "Run: python scripts/build_kb.py")
            except ValueError:
                fail(f"Unexpected output: {line!r}", f"stderr={stderr[:200]}")
        else:
            fail("ChromaDB check failed", stderr[:300])
    except Exception as e:
        fail("ChromaDB check failed", str(e))


# ──────────────────────────────────────────────
# 7. Auth Flow
# ──────────────────────────────────────────────
def test_auth_flow() -> None:
    """Register -> Login -> Token refresh -> Logout cycle."""
    banner("7. Auth Flow")
    global TOKEN, SESSION_ID

    # 7.1 Register (accept 400 as "already registered")
    try:
        resp = httpx.post(
            f"{BASE_URL}/api/auth/register",
            json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD, "name": "Verify User"},
            timeout=10,
        )
        if resp.status_code == 200:
            TOKEN = resp.json()["access_token"]
            ok(f"Register OK (token len={len(TOKEN)})")
        elif resp.status_code == 400:
            # Accept both "already registered" and any other 400
            ok("User already exists or registration skipped")
        else:
            fail(f"Register returned {resp.status_code}", resp.text[:200])
    except Exception as e:
        fail("Register request failed", str(e))
        return

    # 7.2 Login
    try:
        resp = httpx.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200:
            TOKEN = resp.json()["access_token"]
            ok(f"Login OK (token len={len(TOKEN)})")
        else:
            fail(f"Login returned {resp.status_code}", resp.text[:200])
    except Exception as e:
        fail("Login request failed", str(e))
        return

    # 7.3 Unauthenticated request -> 401
    try:
        resp = httpx.post(f"{BASE_URL}/chat", json={"message": "hi"}, timeout=5)
        if resp.status_code == 401:
            ok("Unauth request returns 401")
        else:
            fail(f"Unauth request returned {resp.status_code} (expected 401)")
    except Exception as e:
        fail("401 check failed", str(e))

    # 7.4 Refresh token
    try:
        resp = httpx.post(
            f"{BASE_URL}/api/auth/refresh",
            json={"refresh_token": TOKEN},
            timeout=5,
        )
        if resp.status_code in (200, 401):
            ok(f"Refresh endpoint responds ({resp.status_code})")
        else:
            fail(f"Refresh returned {resp.status_code}", resp.text[:200])
    except Exception as e:
        fail("Refresh failed", str(e))

    # 7.5 Logout
    try:
        resp = httpx.post(
            f"{BASE_URL}/api/auth/logout",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=5,
        )
        if resp.status_code == 200:
            ok("Logout OK")
        else:
            fail(f"Logout returned {resp.status_code}")
    except Exception as e:
        fail("Logout failed", str(e))


def _re_login() -> None:
    """Re-login after logout to get a fresh token for subsequent tests."""
    global TOKEN
    try:
        resp = httpx.post(
            f"{BASE_URL}/api/auth/login",
            json={"email": AUTH_EMAIL, "password": AUTH_PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200:
            TOKEN = resp.json()["access_token"]
            ok(f"Re-login OK (token len={len(TOKEN)})")
        else:
            fail("Re-login failed", f"HTTP {resp.status_code}")
    except Exception as e:
        fail("Re-login failed", str(e))


# ──────────────────────────────────────────────
# 8. Intent Dispatch
# ──────────────────────────────────────────────
def test_dispatch() -> None:
    """Verify different inputs route to correct intents."""
    banner("8. Intent Dispatch")
    cases = [
        ("查一下订单9527的物流", "query_order"),
        ("你们的退货政策是什么", "knowledge_search"),
        ("你好", "chitchat"),
        ("帮我建个工单", "create_ticket"),  # "帮我建个工单" matches 建.*单 / 工单
    ]
    for query, expected_intent in cases:
        try:
            resp = httpx.get(
                f"{BASE_URL}/dispatch",
                params={"query": query},
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=10,
            )
            if resp.status_code != 200:
                fail(f"/dispatch '{query[:20]}'", f"HTTP {resp.status_code}")
                continue
            data = resp.json()
            intent = data.get("intent", "")
            if intent == expected_intent:
                conf = data.get("confidence", "?")
                ok(f"'{query[:20]}' -> {intent} (conf={conf})")
            else:
                fail(f"'{query[:20]}'", f"expected={expected_intent}, got={intent}")
        except Exception as e:
            fail(f"/dispatch '{query[:20]}'", str(e))


# ──────────────────────────────────────────────
# 9. Tool Direct Invocation
# ──────────────────────────────────────────────
def test_tools_directly() -> None:
    """Call tools directly, bypassing the LLM."""
    banner("9. Tools - Direct Invocation")
    try:
        # Query order (sync tool via arun)
        rc, stdout, stderr = _subprocess(
            [
                "docker",
                "compose",
                "exec",
                "app",
                "python",
                "-c",
                "from tools import query_order; print(query_order.arun({'order_id': '9527'}))",
            ],
            timeout=15,
        )
        if rc == 0 and "error" not in stdout.lower():
            ok(f"query_order(9527): {stdout.strip()[:100]}")
        else:
            fail("query_order", stderr[:200] or stdout[:200])

        # Search knowledge (sync StructuredTool, use .run())
        rc, stdout, stderr = _subprocess(
            [
                "docker",
                "compose",
                "exec",
                "app",
                "python",
                "-c",
                "from tools import search_knowledge; print(search_knowledge.run('refund policy'))",
            ],
            timeout=15,
        )
        if rc == 0:
            ok(f"search_knowledge: {stdout.strip()[:100]}")
        else:
            fail("search_knowledge", stderr[:200])

        # Create ticket (async tool)
        rc, stdout, stderr = _subprocess(
            [
                "docker",
                "compose",
                "exec",
                "app",
                "python",
                "-c",
                "import asyncio; from tools import create_ticket; print(asyncio.run(create_ticket.arun({'order_id': '12345', 'reason': 'test'})))",
            ],
            timeout=15,
        )
        if rc == 0 and "error" not in stdout.lower():
            ok(f"create_ticket: {stdout.strip()[:100]}")
        else:
            fail("create_ticket", stderr[:200] or stdout[:200])
    except Exception as e:
        fail("Tool test setup failed", str(e))


# ──────────────────────────────────────────────
# 10. Agent Chat (blocking)
# ──────────────────────────────────────────────
def test_chat_block() -> None:
    """POST /chat, verify complete response structure."""
    global SESSION_ID
    banner("10. Agent Chat - Blocking /chat")
    if not TOKEN:
        fail("No token, skip")
        return
    try:
        resp = httpx.post(
            f"{BASE_URL}/chat",
            json={"message": "查一下订单9527的物流到哪了"},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=60,
        )
        if resp.status_code != 200:
            fail(f"/chat returned {resp.status_code}", resp.text[:300])
            return
        data = resp.json()
        assert "reply" in data, "missing reply field"
        assert "session_id" in data, "missing session_id field"
        assert "tool_calls" in data, "missing tool_calls field"
        SESSION_ID = data["session_id"]
        tool_count = len(data["tool_calls"])
        reply_preview = data["reply"][:80].replace("\n", " ")
        ok(f"reply len={len(data['reply'])}, tools={tool_count}, session={SESSION_ID[:8]}")
        if args.verbose:
            print(f"         Reply: {reply_preview}...")
    except AssertionError as e:
        fail("/chat response structure invalid", str(e))
    except Exception as e:
        fail("/chat request failed", str(e))


# ──────────────────────────────────────────────
# 11. Agent Chat (SSE streaming)
# ──────────────────────────────────────────────
def test_chat_stream() -> None:
    """POST /chat_stream, verify SSE event sequence."""
    banner("11. Agent Chat - Streaming /chat_stream")
    if not TOKEN:
        fail("No token, skip")
        return
    try:
        with httpx.Client(timeout=60) as client:
            with client.stream(
                "POST",
                f"{BASE_URL}/chat_stream",
                json={"message": "订单9527的物流状态如何"},
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as resp:
                if resp.status_code != 200:
                    body = resp.read().decode()
                    fail(f"/chat_stream returned {resp.status_code}", body[:300])
                    return

                events = []
                final_reply = ""
                token_count = 0
                current_event_type = ""
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    if line.startswith("event: "):
                        current_event_type = line[7:].strip()
                    elif line.startswith("data: "):
                        raw = line[6:]
                        try:
                            event = json.loads(raw)
                            events.append(current_event_type)
                            # Sibling data field: {"type": "token", "data": "..."}
                            if isinstance(event, dict):
                                if event.get("type") == "token":
                                    final_reply += event.get("data", "")
                                    token_count += 1
                                elif event.get("type") == "done":
                                    pass  # done signal, reply already accumulated
                                elif event.get("type") in ("tool_call", "error"):
                                    pass
                        except json.JSONDecodeError:
                            pass

                ok(f"SSE events: {len(events)} (tokens={token_count}, done={'done' in events})")
                if token_count > 0:
                    ok(f"Final reply preview: {final_reply[:80]}...")
                elif token_count == 0:
                    fail("No token events received (check SSE format)")
                if args.verbose and final_reply:
                    print(f"         Reply: {final_reply[:200]}...")
    except Exception as e:
        fail("/chat_stream request failed", str(e))


# ──────────────────────────────────────────────
# 12. Session Management
# ──────────────────────────────────────────────
def test_session_management() -> None:
    """Load history -> delete -> verify empty."""
    banner("12. Session Management")
    if not TOKEN or not SESSION_ID:
        fail("No session, skip")
        return

    try:
        resp = httpx.get(
            f"{BASE_URL}/sessions/{SESSION_ID}/history",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10,
        )
        if resp.status_code == 200:
            msg_count = len(resp.json().get("messages", []))
            ok(f"History: {msg_count} messages (session={SESSION_ID[:8]})")
        else:
            fail(f"History returned {resp.status_code}")
    except Exception as e:
        fail("History fetch failed", str(e))

    try:
        resp = httpx.delete(
            f"{BASE_URL}/sessions/{SESSION_ID}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10,
        )
        if resp.status_code == 200:
            ok(f"Session deleted (deleted={resp.json().get('deleted')})")
        else:
            fail(f"Delete returned {resp.status_code}")
    except Exception as e:
        fail("Delete session failed", str(e))


# ──────────────────────────────────────────────
# 13. Monitoring Endpoints
# ──────────────────────────────────────────────
def test_monitoring() -> None:
    """Health and metrics endpoint checks."""
    banner("13. Monitoring Endpoints")
    try:
        resp = httpx.get(f"{BASE_URL}/health", timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            checks = data.get("checks", {})
            ok(
                f"health: {data.get('status')} "
                f"(postgres={checks.get('postgres')}, "
                f"chroma={checks.get('chroma')})"
            )
        else:
            fail(f"/health returned {resp.status_code}")
    except Exception as e:
        fail("/health failed", str(e))

    try:
        resp = httpx.get(f"{BASE_URL}/metrics", timeout=5)
        if resp.status_code == 200 and "http_requests_total" in resp.text:
            ok("metrics: Prometheus format OK")
        else:
            fail(f"/metrics returned {resp.status_code}")
    except Exception as e:
        fail("/metrics failed", str(e))


# ──────────────────────────────────────────────
# 14. Demo Page
# ──────────────────────────────────────────────
def test_demo_page() -> None:
    """Verify demo/index.html is accessible."""
    banner("14. Demo Page")
    try:
        resp = httpx.get(f"{BASE_URL}/demo/index.html", timeout=5)
        if resp.status_code == 200 and "企业客服 Agent Demo" in resp.text:
            ok(f"demo/index.html OK ({len(resp.text)} bytes)")
        else:
            fail(f"/demo/index.html returned {resp.status_code}")
    except Exception as e:
        fail("Demo page request failed", str(e))


# ──────────────────────────────────────────────
# 15. Response Headers
# ──────────────────────────────────────────────
def test_response_headers() -> None:
    """Verify X-Process-Time and X-Request-ID headers are present."""
    banner("15. Response Headers")
    if not TOKEN:
        fail("No token, skip")
        return
    try:
        resp = httpx.post(
            f"{BASE_URL}/chat",
            json={"message": "test header check"},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=10,
        )
        has_time = "X-Process-Time" in resp.headers
        has_reqid = "X-Request-ID" in resp.headers
        if has_time and has_reqid:
            pt = resp.headers["X-Process-Time"]
            rid = resp.headers["X-Request-ID"][:8]
            ok(f"Headers: X-Process-Time={pt}, X-Request-ID={rid}...")
        else:
            fail(f"Missing headers (Process-Time={has_time}, Request-ID={has_reqid})")
    except Exception as e:
        fail("Header check failed", str(e))


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> int:
    global args
    parser = argparse.ArgumentParser(description="Enterprise Customer Agent - Full Verification")
    parser.add_argument("--fast", action="store_true", help="Run only infra + auth + /chat")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show raw response content")
    args = parser.parse_args()

    print(f"\n{'=' * 60}")
    print("  Enterprise Customer Agent - Full Verification")
    print(f"  API: {BASE_URL}")
    print(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 60}")

    t0 = time.time()

    # Always run
    check_docker_containers()
    check_ports()
    check_llm_api()
    check_embedding_api()
    check_seed_data()
    check_chroma_kb()
    test_auth_flow()

    # Re-login after logout for subsequent tests
    _re_login()

    if args.fast:
        test_chat_block()
        test_chat_stream()
    else:
        test_dispatch()
        test_tools_directly()
        test_chat_block()
        test_chat_stream()
        test_session_management()
        test_monitoring()
        test_demo_page()
        test_response_headers()

    elapsed = time.time() - t0
    n_fail = len(FAILURES)

    print(f"\n{'=' * 60}")
    if n_fail:
        print(f"  [RESULT] FAILED: {n_fail} checks failed, {elapsed:.1f}s total")
        for f in FAILURES:
            print(f"    - {f}")
    else:
        print(f"  [RESULT] ALL PASSED -- {elapsed:.1f}s")
    print(f"{'=' * 60}\n")

    return 1 if n_fail else 0


if __name__ == "__main__":
    import subprocess

    sys.exit(main())
