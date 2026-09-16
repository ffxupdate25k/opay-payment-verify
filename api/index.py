"""
PalmPay Payment Verification API
---------------------------------
A single-file Flask application designed to run on Vercel's Python runtime.

Flow:
  1. Bot calls POST /api/request-payment to register expected payment.
  2. Termux calls POST /api/notify when PalmPay notification is detected.
  3. API matches amount + sender name.
  4. Sender name supports partial matching because PalmPay may shorten names.
  5. Bot polls GET /api/verify/<request_id>.
  6. Only /api/verify confirms the payment.

Storage:
  Upstash Redis REST API
"""

import os
import json
import uuid
import hmac
import logging
import re

from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation

import requests
from flask import Flask, request, jsonify


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_SECRET = os.environ.get(
    "BOT_SECRET",
    ""
)

PHONE_SECRET = os.environ.get(
    "PHONE_SECRET",
    ""
)

KV_REST_API_URL = os.environ.get(
    "KV_REST_API_URL",
    ""
).rstrip("/")

KV_REST_API_TOKEN = os.environ.get(
    "KV_REST_API_TOKEN",
    "")


REQUEST_TTL_SECONDS = 30 * 60

PENDING_INDEX_KEY = (
    "palmpay:pending_index"
)

REQUEST_KEY_PREFIX = (
    "palmpay:request:"
)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO
)

logger = logging.getLogger(
    "palmpay-verification"
)


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------

app = Flask(__name__)


# ===========================================================================
# UPSTASH REDIS
# ===========================================================================

class KVError(Exception):
    """Raised whenever the Upstash REST API call fails."""
    pass


def _kv_headers():

    return {
        "Authorization": (
            "Bearer "
            + KV_REST_API_TOKEN
        ),
        "Content-Type": "application/json",
    }


def _ensure_kv_configured():

    if not KV_REST_API_URL:

        raise KVError(
            "KV_REST_API_URL is not configured"
        )

    if not KV_REST_API_TOKEN:

        raise KVError(
            "KV_REST_API_TOKEN is not configured"
        )


def kv_command(*parts):

    _ensure_kv_configured()

    try:

        resp = requests.post(
            KV_REST_API_URL,
            headers=_kv_headers(),
            json=list(parts),
            timeout=10,
        )

    except requests.RequestException as exc:

        raise KVError(
            f"KV request failed: {exc}"
        ) from exc


    if resp.status_code >= 400:

        raise KVError(
            f"KV error {resp.status_code}: {resp.text}"
        )


    try:

        data = resp.json()

    except Exception as exc:

        raise KVError(
            f"Invalid KV response: {exc}"
        )


    if (
        isinstance(data, dict)
        and data.get("error")
    ):

        raise KVError(
            f"KV command error: {data['error']}"
        )


    if isinstance(data, dict):

        return data.get("result")

    return data


def kv_get_json(key):

    raw = kv_command(
        "GET",
        key
    )

    if raw is None:

        return None


    try:

        return json.loads(raw)

    except (
        TypeError,
        ValueError
    ):

        return None


def kv_set_json(
    key,
    value,
    ex=None
):

    payload = json.dumps(
        value
    )


    if ex:

        kv_command(
            "SET",
            key,
            payload,
            "EX",
            ex
        )

    else:

        kv_command(
            "SET",
            key,
            payload
        )


def kv_add_pending(request_id):

    kv_command(
        "SADD",
        PENDING_INDEX_KEY,
        request_id
    )


def kv_remove_pending(request_id):

    kv_command(
        "SREM",
        PENDING_INDEX_KEY,
        request_id
    )


def kv_pending_ids():

    members = kv_command(
        "SMEMBERS",
        PENDING_INDEX_KEY
    )

    return members or []


def request_key(request_id):

    return (
        REQUEST_KEY_PREFIX
        + str(request_id)
    )


# ===========================================================================
# TIME HELPERS
# ===========================================================================

def now_utc():

    return datetime.now(
        timezone.utc
    )


def iso(dt):

    return dt.isoformat()


def parse_iso(value):

    try:

        return datetime.fromisoformat(
            value
        )

    except (
        TypeError,
        ValueError
    ):

        return None


def is_expired(record):

    created_at = parse_iso(
        record.get(
            "created_at"
        )
    )

    if created_at is None:

        return True


    return (
        now_utc()
        - created_at
        >
        timedelta(
            seconds=REQUEST_TTL_SECONDS
        )
    )


# ===========================================================================
# ERROR RESPONSE
# ===========================================================================

def error_response(
    message,
    status=400,
    **extra
):

    body = {
        "error": message
    }

    body.update(
        extra
    )

    return jsonify(
        body
    ), status


# ===========================================================================
# AUTHENTICATION
# ===========================================================================

def require_secret(
    expected_env_value,
    env_name
):

    if not expected_env_value:

        return jsonify({
            "error": "server_misconfigured",
            "message": (
                env_name
                + " is not configured on the server"
            ),
        }), 500


    auth_header = request.headers.get(
        "Authorization",
        ""
    )

    token = ""


    # Authorization: Bearer SECRET
    if auth_header.startswith(
        "Bearer "
    ):

        token = (
            auth_header[
                len("Bearer "):
            ]
            .strip()
        )


    # Alternative header
    if not token:

        token = request.headers.get(
            "X-Auth-Secret",
            ""
        ).strip()


    if (
        not token
        or not hmac.compare_digest(
            token,
            expected_env_value
        )
    ):

        return jsonify({
            "error": "unauthorized"
        }), 401


    return None


# ===========================================================================
# NAME NORMALIZATION
# ===========================================================================

def normalize_name(name):
    """
    Normalize names before comparison.

    Examples:

        SAMUEL(PalmPay)
        ->
        SAMUEL

        Samuel Ayomiposi Oke...
        ->
        SAMUEL AYOMIPOSI OKE
    """

    if name is None:

        return ""


    name = str(
        name
    ).strip().upper()


    # -------------------------------------------------------
    # Remove PalmPay suffix
    #
    # SAMUEL(PalmPay)
    # SAMUEL (PalmPay)
    # -------------------------------------------------------

    name = re.sub(
        r"\s*\(\s*PALMPAY\s*\)",
        "",
        name,
        flags=re.IGNORECASE
    )


    # -------------------------------------------------------
    # Replace punctuation with spaces
    # -------------------------------------------------------

    name = re.sub(
        r"[^A-Z0-9]+",
        " ",
        name
    )


    # -------------------------------------------------------
    # Remove extra spaces
    # -------------------------------------------------------

    name = " ".join(
        name.split()
    )


    return name


# ===========================================================================
# PARTIAL NAME MATCHING
# ===========================================================================

def names_match(
    received_name,
    expected_name
):
    """
    Compare PalmPay notification name against
    the verified KoraPay account name.

    Examples:

    Expected:
        SAMUEL AYOMIPOSI OKEWAL...

    Received:
        SAMUEL

    Result:
        True


    Expected:
        SAMUEL AYOMIPOSI OKEWAL...

    Received:
        SAMUEL AYOMIPOSI

    Result:
        True
    """

    received = normalize_name(
        received_name
    )

    expected = normalize_name(
        expected_name
    )


    if not received:

        return False


    if not expected:

        return False


    # -------------------------------------------------------
    # Exact match
    # -------------------------------------------------------

    if received == expected:

        return True


    # -------------------------------------------------------
    # Split into words
    # -------------------------------------------------------

    received_parts = set(
        received.split()
    )

    expected_parts = set(
        expected.split()
    )


    if not received_parts:

        return False


    if not expected_parts:

        return False


    # -------------------------------------------------------
    # PARTIAL MATCH
    #
    # Example:
    #
    # received:
    # SAMUEL
    #
    # expected:
    # SAMUEL AYOMIPOSI OKEWAL...
    #
    # SAMUEL is contained in expected.
    # -------------------------------------------------------

    if received_parts.issubset(
        expected_parts
    ):

        return True


    return False


# ===========================================================================
# HEALTH ROUTES
# ===========================================================================

@app.route(
    "/",
    methods=["GET"]
)
@app.route(
    "/api",
    methods=["GET"]
)
@app.route(
    "/api/index",
    methods=["GET"]
)
def health():

    return jsonify({
        "status": "ok",
        "service": (
            "palmpay-payment-verification-api"
        ),
        "time": iso(
            now_utc()
        ),
    })


# ===========================================================================
# DEBUG SECRET
# ===========================================================================

@app.route(
    "/api/debug-secret",
    methods=["GET"]
)
def debug_secret():

    auth_err = require_secret(
        BOT_SECRET,
        "BOT_SECRET"
    )

    if auth_err:

        return auth_err


    return jsonify({

        "BOT_SECRET_set": bool(
            BOT_SECRET
        ),

        "PHONE_SECRET_set": bool(
            PHONE_SECRET
        ),

        "KV_REST_API_URL_set": bool(
            KV_REST_API_URL
        ),

        "KV_REST_API_TOKEN_set": bool(
            KV_REST_API_TOKEN
        )

    })


# ===========================================================================
# POST /api/request-payment
# ===========================================================================

@app.route(
    "/api/request-payment",
    methods=["POST"]
)
def request_payment():

    # -------------------------------------------------------
    # AUTH
    # -------------------------------------------------------

    auth_err = require_secret(
        BOT_SECRET,
        "BOT_SECRET"
    )

    if auth_err:

        return auth_err


    # -------------------------------------------------------
    # JSON
    # -------------------------------------------------------

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )


    user_id = payload.get(
        "user_id"
    )

    amount_raw = payload.get(
        "amount"
    )

    sender_name_raw = payload.get(
        "sender_name"
    )


    # -------------------------------------------------------
    # VALIDATION
    # -------------------------------------------------------

    if (
        not user_id
        or amount_raw is None
        or not sender_name_raw
    ):

        return error_response(
            "user_id, amount and sender_name are required fields",
            400
        )


    amount_norm = normalize_amount(
        amount_raw
    )

    if amount_norm is None:

        return error_response(
            "amount is not a valid number",
            400
        )


    sender_name_norm = normalize_name(
        sender_name_raw
    )

    if not sender_name_norm:

        return error_response(
            "sender_name is invalid",
            400
        )


    # -------------------------------------------------------
    # CREATE REQUEST
    # -------------------------------------------------------

    request_id = str(
        uuid.uuid4()
    )

    created_at = now_utc()


    record = {

        "request_id":
            request_id,

        "user_id":
            str(user_id),

        "amount":
            amount_norm,

        "amount_raw":
            str(amount_raw),

        "sender_name":
            sender_name_norm,

        "sender_name_raw":
            str(sender_name_raw),

        "status":
            "pending",

        "notification_received":
            False,

        "notification":
            None,

        "created_at":
            iso(created_at),

        "verified_at":
            None

    }


    # -------------------------------------------------------
    # SAVE REQUEST
    # -------------------------------------------------------

    try:

        kv_set_json(
            request_key(
                request_id
            ),
            record,
            ex=REQUEST_TTL_SECONDS
        )

        kv_add_pending(
            request_id
        )


    except KVError as exc:

        logger.exception(
            "Failed to persist payment request"
        )

        return error_response(
            "storage_error",
            502,
            details=str(exc)
        )


    return jsonify({

        "request_id":
            request_id,

        "status":
            "pending"

    }), 201


# ===========================================================================
# POST /api/notify
# ===========================================================================

@app.route(
    "/api/notify",
    methods=["POST"]
)
def notify():

    # -------------------------------------------------------
    # AUTH
    # -------------------------------------------------------

    auth_err = require_secret(
        PHONE_SECRET,
        "PHONE_SECRET"
    )

    if auth_err:

        return auth_err


    # -------------------------------------------------------
    # JSON
    # -------------------------------------------------------

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )


    amount_raw = payload.get(
        "amount"
    )

    sender_name_raw = payload.get(
        "sender_name"
    )

    raw_text = payload.get(
        "raw",
        ""
    )


    # -------------------------------------------------------
    # VALIDATION
    # -------------------------------------------------------

    if (
        amount_raw is None
        or not sender_name_raw
    ):

        return error_response(
            "amount and sender_name are required fields",
            400
        )


    amount_norm = normalize_amount(
        amount_raw
    )

    sender_name_norm = normalize_name(
        sender_name_raw
    )


    if (
        amount_norm is None
        or not sender_name_norm
    ):

        return error_response(
            "amount or sender_name is invalid",
            400
        )


    # -------------------------------------------------------
    # GET PENDING REQUESTS
    # -------------------------------------------------------

    try:

        pending_ids = kv_pending_ids()


    except KVError as exc:

        logger.exception(
            "Failed to read pending index"
        )

        return error_response(
            "storage_error",
            502,
            details=str(exc)
        )


    matched = False

    received_at = iso(
        now_utc()
    )


    # -------------------------------------------------------
    # CHECK EACH REQUEST
    # -------------------------------------------------------

    for request_id in pending_ids:

        try:

            record = kv_get_json(
                request_key(
                    request_id
                )
            )


        except KVError:

            continue


        # ---------------------------------------------------
        # REQUEST NO LONGER EXISTS
        # ---------------------------------------------------

        if record is None:

            try:

                kv_remove_pending(
                    request_id
                )

            except KVError:

                pass

            continue


        # ---------------------------------------------------
        # ONLY PENDING REQUESTS
        # ---------------------------------------------------

        if record.get(
            "status"
        ) != "pending":

            continue


        # ---------------------------------------------------
        # CHECK EXPIRATION
        # ---------------------------------------------------

        if is_expired(
            record
        ):

            record[
                "status"
            ] = "expired"


            try:

                kv_set_json(
                    request_key(
                        request_id
                    ),
                    record,
                    ex=60
                )

                kv_remove_pending(
                    request_id
                )


            except KVError:

                pass


            continue


        # ---------------------------------------------------
        # AMOUNT MATCH
        # ---------------------------------------------------

        amount_matches = (
            record.get(
                "amount"
            )
            ==
            amount_norm
        )


        # ---------------------------------------------------
        # PARTIAL NAME MATCH
        # ---------------------------------------------------

        name_matches = names_match(

            sender_name_norm,

            record.get(
                "sender_name",
                ""
            )

        )


        # ---------------------------------------------------
        # FINAL MATCH
        # ---------------------------------------------------

        if (
            amount_matches
            and name_matches
        ):

            # -----------------------------------------------
            # SAVE NOTIFICATION
            # -----------------------------------------------

            record[
                "notification_received"
            ] = True


            record[
                "notification"
            ] = {

                "amount":
                    amount_norm,

                "amount_raw":
                    str(amount_raw),

                "sender_name":
                    sender_name_norm,

                "sender_name_raw":
                    str(sender_name_raw),

                "raw":
                    raw_text,

                "received_at":
                    received_at

            }


            # -----------------------------------------------
            # DO NOT CONFIRM HERE
            #
            # /api/verify/<request_id>
            # confirms the payment.
            # -----------------------------------------------

            record[
                "status"
            ] = "pending"


            try:

                kv_set_json(
                    request_key(
                        request_id
                    ),
                    record,
                    ex=REQUEST_TTL_SECONDS
                )

                matched = True


            except KVError:

                logger.exception(
                    "Failed to save notification"
                )

                continue


    # -------------------------------------------------------
    # RESPONSE
    # -------------------------------------------------------

    return jsonify({

        "matched":
            matched,

        "verified":
            False

    })


# ===========================================================================
# GET /api/verify/<request_id>
# ===========================================================================

@app.route(
    "/api/verify/<request_id>",
    methods=["GET"]
)
def verify(request_id):

    # -------------------------------------------------------
    # AUTH
    # -------------------------------------------------------

    auth_err = require_secret(
        BOT_SECRET,
        "BOT_SECRET"
    )

    if auth_err:

        return auth_err


    # -------------------------------------------------------
    # GET REQUEST
    # -------------------------------------------------------

    try:

        record = kv_get_json(
            request_key(
                request_id
            )
        )


    except KVError as exc:

        logger.exception(
            "Failed to read request"
        )

        return error_response(
            "storage_error",
            502,
            details=str(exc)
        )


    # -------------------------------------------------------
    # NOT FOUND
    # -------------------------------------------------------

    if record is None:

        return error_response(
            "not_found",
            404,
            request_id=request_id
        )


    # -------------------------------------------------------
    # ALREADY CONFIRMED
    # -------------------------------------------------------

    if record.get(
        "status"
    ) == "confirmed":

        return jsonify({

            "request_id":
                request_id,

            "status":
                "confirmed",

            "verified_at":
                record.get(
                    "verified_at"
                ),

            "notification":
                record.get(
                    "notification"
                )

        })


    # -------------------------------------------------------
    # EXPIRED
    # -------------------------------------------------------

    if (
        record.get(
            "status"
        ) == "expired"
        or
        is_expired(
            record
        )
    ):

        record[
            "status"
        ] = "expired"


        try:

            kv_set_json(
                request_key(
                    request_id
                ),
                record,
                ex=60
            )

            kv_remove_pending(
                request_id
            )


        except KVError:

            pass


        return jsonify({

            "request_id":
                request_id,

            "status":
                "expired"

        }), 410


    # -------------------------------------------------------
    # GET NOTIFICATION
    # -------------------------------------------------------

    notification = record.get(
        "notification"
    )


    if (
        not record.get(
            "notification_received"
        )
        or not notification
    ):

        return jsonify({

            "request_id":
                request_id,

            "status":
                "pending"

        })


    # -------------------------------------------------------
    # FINAL AMOUNT CHECK
    # -------------------------------------------------------

    notification_amount = normalize_amount(

        notification.get(
            "amount"
        )

    )


    expected_amount = normalize_amount(

        record.get(
            "amount"
        )

    )


    amount_matches = (
        notification_amount
        ==
        expected_amount
    )


    # -------------------------------------------------------
    # FINAL PARTIAL NAME CHECK
    # -------------------------------------------------------

    name_matches = names_match(

        notification.get(
            "sender_name",
            ""
        ),

        record.get(
            "sender_name",
            ""
        )

    )


    # -------------------------------------------------------
    # FINAL MATCH
    # -------------------------------------------------------

    matches = (
        amount_matches
        and
        name_matches
    )


    if not matches:

        return jsonify({

            "request_id":
                request_id,

            "status":
                "pending"

        })


    # -------------------------------------------------------
    # CONFIRM
    # -------------------------------------------------------

    verified_at = iso(
        now_utc()
    )


    record[
        "status"
    ] = "confirmed"


    record[
        "verified_at"
    ] = verified_at


    # -------------------------------------------------------
    # SAVE CONFIRMATION
    # -------------------------------------------------------

    try:

        kv_set_json(
            request_key(
                request_id
            ),
            record,
            ex=REQUEST_TTL_SECONDS
        )

        kv_remove_pending(
            request_id
        )


    except KVError as exc:

        logger.exception(
            "Failed to persist confirmation"
        )

        return error_response(
            "storage_error",
            502,
            details=str(exc)
        )


    # -------------------------------------------------------
    # RETURN CONFIRMED
    # -------------------------------------------------------

    return jsonify({

        "request_id":
            request_id,

        "status":
            "confirmed",

        "verified_at":
            verified_at,

        "notification":
            notification

    })


# ===========================================================================
# ERROR HANDLERS
# ===========================================================================

@app.errorhandler(404)
def handle_404(_err):

    return jsonify({
        "error": "not_found"
    }), 404


@app.errorhandler(405)
def handle_405(_err):

    return jsonify({
        "error": "method_not_allowed"
    }), 405


@app.errorhandler(500)
def handle_500(_err):

    logger.exception(
        "Unhandled server error"
    )

    return jsonify({
        "error": "internal_server_error"
    }), 500


# ===========================================================================
# LOCAL RUN
# ===========================================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        )
)
