"""
PalmPay Payment Verification API
---------------------------------
A single-file Flask application designed to run on Vercel's Python runtime.

Flow:
  1. A bot calls POST /api/request-payment to register an expected payment.
     A pending record is created in Upstash Redis (Vercel KV) with a 30
     minute TTL and added to a "pending index" set.
  2. A Termux phone client calls POST /api/notify whenever a PalmPay
     notification is seen. This matches the notification against pending
     requests using sender_name + amount only, and stores the raw
     notification on the matching record(s) WITHOUT confirming payment.
     Notifications may be sent repeatedly and will simply overwrite the
     stored notification.
  3. The bot polls GET /api/verify/<request_id>. Only this endpoint ever
     promotes a request from "pending" to "confirmed". Once confirmed, the
     record is removed from the pending index and further calls return the
     same confirmed result idempotently.

Storage is done directly against the Upstash REST API using `requests`,
so no Redis client library is required.
"""

import os
import json
import uuid
import hmac
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify

# ---------------------------------------------------------------------------
# Configuration (env vars only - never hardcode secrets)
# ---------------------------------------------------------------------------

BOT_SECRET = os.environ.get("BOT_SECRET", "")
PHONE_SECRET = os.environ.get("PHONE_SECRET", "")
KV_REST_API_URL = os.environ.get("KV_REST_API_URL", "").rstrip("/")
KV_REST_API_TOKEN = os.environ.get("KV_REST_API_TOKEN", "")

REQUEST_TTL_SECONDS = 30 * 60  # requests expire after 30 minutes
PENDING_INDEX_KEY = "palmpay:pending_index"
REQUEST_KEY_PREFIX = "palmpay:request:"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("palmpay-verification")

# Vercel's Python runtime looks for a top-level WSGI-compatible `app`.
app = Flask(__name__)


# ---------------------------------------------------------------------------
# Upstash Redis (Vercel KV) REST client
# ---------------------------------------------------------------------------

class KVError(Exception):
    """Raised whenever the Upstash REST API call fails or is misconfigured."""


def _kv_headers():
    return {
        "Authorization": f"Bearer {KV_REST_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _ensure_kv_configured():
    if not KV_REST_API_URL or not KV_REST_API_TOKEN:
        raise KVError("KV_REST_API_URL / KV_REST_API_TOKEN are not configured")


def kv_command(*parts):
    """Execute a single Redis command against the Upstash REST API.

    e.g. kv_command("SET", "foo", "bar", "EX", 60)
    """
    _ensure_kv_configured()

    try:
        resp = requests.post(
            KV_REST_API_URL,
            headers=_kv_headers(),
            json=list(parts),
            timeout=10,
        )
    except requests.RequestException as exc:
        raise KVError(f"KV request failed: {exc}") from exc

    if resp.status_code >= 400:
        raise KVError(f"KV error {resp.status_code}: {resp.text}")

    data = resp.json()
    if isinstance(data, dict) and data.get("error"):
        raise KVError(f"KV command error: {data['error']}")

    return data.get("result") if isinstance(data, dict) else data


def kv_get_json(key):
    raw = kv_command("GET", key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def kv_set_json(key, value, ex=None):
    payload = json.dumps(value)
    if ex:
        kv_command("SET", key, payload, "EX", ex)
    else:
        kv_command("SET", key, payload)


def kv_add_pending(request_id):
    kv_command("SADD", PENDING_INDEX_KEY, request_id)


def kv_remove_pending(request_id):
    kv_command("SREM", PENDING_INDEX_KEY, request_id)


def kv_pending_ids():
    members = kv_command("SMEMBERS", PENDING_INDEX_KEY)
    return members or []


def request_key(request_id):
    return f"{REQUEST_KEY_PREFIX}{request_id}"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.isoformat()


def parse_iso(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def normalize_name(name):
    """Uppercase + collapse whitespace so name comparisons are robust."""
    if name is None:
        return ""
    return " ".join(str(name).strip().upper().split())


def normalize_amount(amount):
    """Normalize an amount to a fixed 2-decimal string for comparison."""
    try:
        cleaned = str(amount).replace(",", "").replace("NGN", "").replace("₦", "").strip()
        value = Decimal(cleaned)
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return str(value.quantize(Decimal("0.01")))


def is_expired(record):
    created_at = parse_iso(record.get("created_at"))
    if created_at is None:
        return True
    return now_utc() - created_at > timedelta(seconds=REQUEST_TTL_SECONDS)


def error_response(message, status=400, **extra):
    body = {"error": message}
    body.update(extra)
    return jsonify(body), status


def require_secret(expected_env_value, env_name):
    """Validate a bearer token in the request against a configured secret.

    Accepts either `Authorization: Bearer <secret>` or a raw `X-Auth-Secret`
    header (useful for simple Termux HTTP clients that can't easily set
    Authorization headers). Returns a Flask response tuple on failure, or
    None if the caller is authorized.
    """
    if not expected_env_value:
        return jsonify({
            "error": "server_misconfigured",
            "message": f"{env_name} is not configured on the server",
        }), 500

    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[len("Bearer "):].strip()

    if not token:
        token = request.headers.get("X-Auth-Secret", "").strip()

    if not token or not hmac.compare_digest(token, expected_env_value):
        return jsonify({"error": "unauthorized"}), 401

    return None


# ---------------------------------------------------------------------------
# Health routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
@app.route("/api", methods=["GET"])
@app.route("/api/index", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "service": "palmpay-payment-verification-api",
        "time": iso(now_utc()),
    })


@app.route("/api/debug-secret", methods=["GET"])
def debug_secret():
    auth_err = require_secret(BOT_SECRET, "BOT_SECRET")
    if auth_err:
        return auth_err

    return jsonify({
        "BOT_SECRET_set": bool(BOT_SECRET),
        "PHONE_SECRET_set": bool(PHONE_SECRET),
        "KV_REST_API_URL_set": bool(KV_REST_API_URL),
        "KV_REST_API_TOKEN_set": bool(KV_REST_API_TOKEN),
    })


# ---------------------------------------------------------------------------
# POST /api/request-payment  (protected by BOT_SECRET)
# ---------------------------------------------------------------------------

@app.route("/api/request-payment", methods=["POST"])
def request_payment():
    auth_err = require_secret(BOT_SECRET, "BOT_SECRET")
    if auth_err:
        return auth_err

    payload = request.get_json(silent=True) or {}
    user_id = payload.get("user_id")
    amount_raw = payload.get("amount")
    sender_name_raw = payload.get("sender_name")

    if not user_id or amount_raw is None or not sender_name_raw:
        return error_response(
            "user_id, amount and sender_name are required fields", 400
        )

    amount_norm = normalize_amount(amount_raw)
    if amount_norm is None:
        return error_response("amount is not a valid number", 400)

    sender_name_norm = normalize_name(sender_name_raw)
    if not sender_name_norm:
        return error_response("sender_name is invalid", 400)

    request_id = str(uuid.uuid4())
    created_at = now_utc()

    record = {
        "request_id": request_id,
        "user_id": str(user_id),
        "amount": amount_norm,
        "amount_raw": str(amount_raw),
        "sender_name": sender_name_norm,
        "sender_name_raw": str(sender_name_raw),
        "status": "pending",
        "notification_received": False,
        "notification": None,
        "created_at": iso(created_at),
        "verified_at": None,
    }

    try:
        kv_set_json(request_key(request_id), record, ex=REQUEST_TTL_SECONDS)
        kv_add_pending(request_id)
    except KVError as exc:
        logger.exception("Failed to persist payment request")
        return error_response("storage_error", 502, message=str(exc))

    return jsonify({"request_id": request_id, "status": "pending"}), 201


# ---------------------------------------------------------------------------
# POST /api/notify  (protected by PHONE_SECRET)
# ---------------------------------------------------------------------------

@app.route("/api/notify", methods=["POST"])
def notify():
    auth_err = require_secret(PHONE_SECRET, "PHONE_SECRET")
    if auth_err:
        return auth_err

    payload = request.get_json(silent=True) or {}
    amount_raw = payload.get("amount")
    sender_name_raw = payload.get("sender_name")
    raw_text = payload.get("raw", "")

    if amount_raw is None or not sender_name_raw:
        return error_response("amount and sender_name are required fields", 400)

    amount_norm = normalize_amount(amount_raw)
    sender_name_norm = normalize_name(sender_name_raw)

    if amount_norm is None or not sender_name_norm:
        return error_response("amount or sender_name is invalid", 400)

    try:
        pending_ids = kv_pending_ids()
    except KVError as exc:
        logger.exception("Failed to read pending index")
        return error_response("storage_error", 502, message=str(exc))

    matched = False
    received_at = iso(now_utc())

    for request_id in pending_ids:
        try:
            record = kv_get_json(request_key(request_id))
        except KVError:
            continue

        if record is None:
            # Evicted by the Redis TTL already - drop it from the index.
            try:
                kv_remove_pending(request_id)
            except KVError:
                pass
            continue

        if record.get("status") != "pending":
            continue

        if is_expired(record):
            record["status"] = "expired"
            try:
                kv_set_json(request_key(request_id), record, ex=60)
                kv_remove_pending(request_id)
            except KVError:
                pass
            continue

        if (
            record.get("amount") == amount_norm
            and record.get("sender_name") == sender_name_norm
        ):
            # Store the notification, but do NOT confirm payment here.
            record["notification_received"] = True
            record["notification"] = {
                "amount": amount_norm,
                "amount_raw": str(amount_raw),
                "sender_name": sender_name_norm,
                "sender_name_raw": str(sender_name_raw),
                "raw": raw_text,
                "received_at": received_at,
            }
            # status stays "pending" - repeated notifications simply
            # overwrite the stored notification above.
            try:
                kv_set_json(request_key(request_id), record, ex=REQUEST_TTL_SECONDS)
            except KVError:
                logger.exception("Failed to persist notification for %s", request_id)
                continue
            matched = True

    return jsonify({"matched": matched, "verified": False})


# ---------------------------------------------------------------------------
# GET /api/verify/<request_id>  (protected by BOT_SECRET)
# ---------------------------------------------------------------------------

@app.route("/api/verify/<request_id>", methods=["GET"])
def verify(request_id):
    auth_err = require_secret(BOT_SECRET, "BOT_SECRET")
    if auth_err:
        return auth_err

    try:
        record = kv_get_json(request_key(request_id))
    except KVError as exc:
        logger.exception("Failed to read request %s", request_id)
        return error_response("storage_error", 502, message=str(exc))

    if record is None:
        return error_response("not_found", 404, request_id=request_id)

    # Already confirmed earlier - return the stored result idempotently.
    if record.get("status") == "confirmed":
        return jsonify({
            "request_id": request_id,
            "status": "confirmed",
            "verified_at": record.get("verified_at"),
            "notification": record.get("notification"),
        })

    # Expired (30 minutes elapsed with no confirmation).
    if record.get("status") == "expired" or is_expired(record):
        if record.get("status") != "expired":
            record["status"] = "expired"
            try:
                kv_set_json(request_key(request_id), record, ex=60)
                kv_remove_pending(request_id)
            except KVError:
                pass
        return jsonify({"request_id": request_id, "status": "expired"}), 410

    notification = record.get("notification")
    if not record.get("notification_received") or not notification:
        return jsonify({"request_id": request_id, "status": "pending"})

    matches = (
        notification.get("amount") == record.get("amount")
        and notification.get("sender_name") == record.get("sender_name")
    )

    if not matches:
        # A notification exists but it doesn't line up - stay pending.
        return jsonify({"request_id": request_id, "status": "pending"})

    verified_at = iso(now_utc())
    record["status"] = "confirmed"
    record["verified_at"] = verified_at

    try:
        kv_set_json(request_key(request_id), record, ex=REQUEST_TTL_SECONDS)
        kv_remove_pending(request_id)
    except KVError as exc:
        logger.exception("Failed to persist confirmation for %s", request_id)
        return error_response("storage_error", 502, message=str(exc))

    return jsonify({
        "request_id": request_id,
        "status": "confirmed",
        "verified_at": verified_at,
        "notification": notification,
    })


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def handle_404(_err):
    return jsonify({"error": "not_found"}), 404


@app.errorhandler(405)
def handle_405(_err):
    return jsonify({"error": "method_not_allowed"}), 405


@app.errorhandler(500)
def handle_500(_err):
    logger.exception("Unhandled server error")
    return jsonify({"error": "internal_server_error"}), 500


if __name__ == "__main__":
    app.run(debug=True)
