"""
Payment verification API - Vercel deployment version (single file).

Combined into one file because Vercel's Python builder only packages the
exact file listed in vercel.json - it does not automatically bundle sibling
files like a separate kv.py, which caused a ModuleNotFoundError.

Three jobs:
1. POST /api/request-payment  -> bot creates a pending payment request
2. POST /api/notify           -> phone (Termux) reports a parsed notification
3. GET  /api/verify/<req_id>  -> bot checks status when user taps "Verify Payment"

Storage is Vercel KV (Redis) via its REST API. Falls back to an in-memory
dict automatically if KV env vars aren't set (useful for local testing only -
in-memory storage will NOT persist between requests once deployed, since
serverless functions don't keep memory between invocations).
"""

import os
import time
import uuid
import json
import difflib
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- config ------------------------------------------------------------
PHONE_SECRET = os.environ.get("PHONE_SECRET", "").strip()
BOT_SECRET = os.environ.get("BOT_SECRET", "").strip()
NAME_MATCH_THRESHOLD = 0.72
REQUEST_EXPIRY_SECONDS = 60 * 30

PENDING_INDEX_KEY = "pending_index"

KV_URL = os.environ.get("KV_REST_API_URL") or os.environ.get("UPSTASH_REDIS_REST_URL")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN") or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
USE_LOCAL_FALLBACK = not (KV_URL and KV_TOKEN)

_local_store = {}
_local_sets = {}


# --- storage helpers (KV or local fallback) ------------------------------
def _headers():
    return {"Authorization": f"Bearer {KV_TOKEN}"}


def kv_get(key):
    if USE_LOCAL_FALLBACK:
        return _local_store.get(key)
    r = requests.get(f"{KV_URL}/get/{key}", headers=_headers(), timeout=10)
    val = r.json().get("result")
    return json.loads(val) if val else None


def kv_set(key, value):
    if USE_LOCAL_FALLBACK:
        _local_store[key] = value
        return
    requests.post(f"{KV_URL}/set/{key}", headers=_headers(), data=json.dumps(value), timeout=10)


def kv_sadd(set_key, member):
    if USE_LOCAL_FALLBACK:
        _local_sets.setdefault(set_key, set()).add(member)
        return
    requests.post(f"{KV_URL}/sadd/{set_key}/{member}", headers=_headers(), timeout=10)


def kv_srem(set_key, member):
    if USE_LOCAL_FALLBACK:
        _local_sets.get(set_key, set()).discard(member)
        return
    requests.post(f"{KV_URL}/srem/{set_key}/{member}", headers=_headers(), timeout=10)


def kv_smembers(set_key):
    if USE_LOCAL_FALLBACK:
        return list(_local_sets.get(set_key, set()))
    r = requests.get(f"{KV_URL}/smembers/{set_key}", headers=_headers(), timeout=10)
    return r.json().get("result", [])


# --- business logic helpers ------------------------------------------------
def now():
    return time.time()


def names_match(typed_name: str, notification_name: str) -> bool:
    a = typed_name.strip().lower()
    b = notification_name.strip().lower()
    return difflib.SequenceMatcher(None, a, b).ratio() >= NAME_MATCH_THRESHOLD


def amounts_match(expected: str, received: str) -> bool:
    try:
        return abs(float(expected) - float(received)) < 0.01
    except (TypeError, ValueError):
        return False


def payment_key(request_id):
    return f"payment:{request_id}"


def load_payment(request_id):
    return kv_get(payment_key(request_id))


def save_payment(request_id, data):
    kv_set(payment_key(request_id), data)


def cleanup_expired(request_id, r):
    if r["status"] == "pending" and (now() - r["created_at"]) > REQUEST_EXPIRY_SECONDS:
        r["status"] = "expired"
        save_payment(request_id, r)
        kv_srem(PENDING_INDEX_KEY, request_id)
    return r


def check_auth(expected_secret):
    provided = request.headers.get("Authorization", "").strip()
    return provided == expected_secret


# --- 1. bot creates a pending payment request ---------------------------
@app.route("/api/request-payment", methods=["POST"])
def request_payment():
    if not check_auth(BOT_SECRET):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    required = ["user_id", "amount", "sender_name", "sender_account", "sender_bank"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"missing fields: {missing}"}), 400

    request_id = str(uuid.uuid4())
    record = {
        "user_id": data["user_id"],
        "amount": str(data["amount"]),
        "sender_name": data["sender_name"],
        "sender_account": data["sender_account"],
        "sender_bank": data["sender_bank"],
        "status": "pending",
        "created_at": now(),
        "matched_notification": None,
        "fail_reason": None,
    }
    save_payment(request_id, record)
    kv_sadd(PENDING_INDEX_KEY, request_id)

    return jsonify({"request_id": request_id, "status": "pending"})


# --- 2. phone sends parsed notification data -----------------------------
@app.route("/api/notify", methods=["POST"])
def notify():
    if not check_auth(PHONE_SECRET):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(force=True)
    received_amount = data.get("amount")
    received_sender = data.get("sender_name") or ""
    raw = data.get("raw", "")

    if not received_amount:
        return jsonify({"error": "no amount parsed"}), 400

    pending_ids = kv_smembers(PENDING_INDEX_KEY)
    candidates = []
    for rid in pending_ids:
        r = load_payment(rid)
        if not r:
            continue
        r = cleanup_expired(rid, r)
        if r["status"] == "pending" and names_match(r["sender_name"], received_sender):
            candidates.append((rid, r))

    if not candidates:
        return jsonify({"matched": False, "reason": "no pending request for this sender"})

    match_found = None
    failed_ids = []
    for request_id, r in candidates:
        if amounts_match(r["amount"], received_amount):
            r["status"] = "matched"
            r["matched_notification"] = raw
            save_payment(request_id, r)
            kv_srem(PENDING_INDEX_KEY, request_id)
            match_found = request_id
        else:
            r["status"] = "failed"
            r["fail_reason"] = f"expected ₦{r['amount']} but received ₦{received_amount}"
            r["matched_notification"] = raw
            save_payment(request_id, r)
            kv_srem(PENDING_INDEX_KEY, request_id)
            failed_ids.append(request_id)

    if match_found:
        return jsonify({"matched": True, "request_id": match_found})

    return jsonify({"matched": False, "failed_request_ids": failed_ids, "reason": "amount mismatch"})


# --- 3. bot checks status when user taps "Verify Payment" -----------------
@app.route("/api/verify/<request_id>", methods=["GET"])
def verify(request_id):
    if not check_auth(BOT_SECRET):
        return jsonify({"error": "unauthorized"}), 401

    r = load_payment(request_id)
    if not r:
        return jsonify({"error": "not found"}), 404

    r = cleanup_expired(request_id, r)

    if r["status"] == "matched":
        r["status"] = "confirmed"
        save_payment(request_id, r)
        return jsonify({"status": "confirmed", "detail": r["matched_notification"]})

    if r["status"] == "failed":
        return jsonify({
            "status": "failed",
            "reason": r.get("fail_reason"),
            "detail": r.get("matched_notification"),
        })

    return jsonify({"status": r["status"]})


# --- TEMPORARY debug endpoint - remove once secrets are confirmed working --
@app.route("/api/debug-secret", methods=["GET"])
def debug_secret():
    return jsonify({
        "bot_secret_length": len(BOT_SECRET),
        "bot_secret_first4": BOT_SECRET[:4],
        "bot_secret_last4": BOT_SECRET[-4:],
        "phone_secret_length": len(PHONE_SECRET),
        "using_default_bot_secret": BOT_SECRET == "change-me-bot-secret",
    })


# --- simple health check --------------------------------------------------
@app.route("/api/index", methods=["GET"])
@app.route("/api", methods=["GET"])
@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "opay payment verification api"})
