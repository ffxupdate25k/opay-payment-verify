import os
import time
import uuid
import json
import difflib
import requests

from flask import Flask, request, jsonify


# ============================================================
# VERCEL APP
# ============================================================

app = Flask(__name__)


# ============================================================
# SECRETS
# ============================================================

PHONE_SECRET = os.environ.get("9c054cd236e17a8a366748987c3096d93b5a6f3021474d615b843cee0e3ea046", "").strip()
BOT_SECRET = os.environ.get("2af361f3ec15bbe278d175de7e68b57f35755f6923197d0168ce498a82656f96", "").strip()


# ============================================================
# SETTINGS
# ============================================================

NAME_MATCH_THRESHOLD = 0.72
REQUEST_EXPIRY_SECONDS = 60 * 30

PENDING_INDEX_KEY = "pending_index"


# ============================================================
# KV / UPSTASH
# ============================================================

KV_URL = (
    os.environ.get("KV_REST_API_URL")
    or os.environ.get("UPSTASH_REDIS_REST_URL")
)

KV_TOKEN = (
    os.environ.get("KV_REST_API_TOKEN")
    or os.environ.get("UPSTASH_REDIS_REST_TOKEN")
)

USE_LOCAL_FALLBACK = not (
    KV_URL and KV_TOKEN
)

_local_store = {}
_local_sets = {}


# ============================================================
# KV HELPERS
# ============================================================

def kv_headers():
    return {
        "Authorization": "Bearer " + KV_TOKEN
    }


def kv_get(key):

    if USE_LOCAL_FALLBACK:
        return _local_store.get(key)

    try:

        r = requests.get(
            KV_URL + "/get/" + key,
            headers=kv_headers(),
            timeout=10
        )

        result = r.json().get("result")

        if result is None:
            return None

        if isinstance(result, str):

            try:
                return json.loads(result)
            except:
                return result

        return result

    except Exception:
        return None


def kv_set(key, value):

    if USE_LOCAL_FALLBACK:

        _local_store[key] = value

        return True

    try:

        requests.post(
            KV_URL + "/set/" + key,
            headers=kv_headers(),
            data=json.dumps(value),
            timeout=10
        )

        return True

    except Exception:
        return False


def kv_sadd(key, value):

    if USE_LOCAL_FALLBACK:

        if key not in _local_sets:
            _local_sets[key] = set()

        _local_sets[key].add(str(value))

        return True

    try:

        requests.post(
            KV_URL + "/sadd/" + key + "/" + str(value),
            headers=kv_headers(),
            timeout=10
        )

        return True

    except Exception:
        return False


def kv_srem(key, value):

    if USE_LOCAL_FALLBACK:

        if key in _local_sets:
            _local_sets[key].discard(
                str(value)
            )

        return True

    try:

        requests.post(
            KV_URL + "/srem/" + key + "/" + str(value),
            headers=kv_headers(),
            timeout=10
        )

        return True

    except Exception:
        return False


def kv_smembers(key):

    if USE_LOCAL_FALLBACK:

        return list(
            _local_sets.get(
                key,
                set()
            )
        )

    try:

        r = requests.get(
            KV_URL + "/smembers/" + key,
            headers=kv_headers(),
            timeout=10
        )

        result = r.json().get(
            "result",
            []
        )

        return result or []

    except Exception:

        return []


# ============================================================
# GENERAL HELPERS
# ============================================================

def now():
    return time.time()


def normalize_name(name):

    return " ".join(
        str(name or "")
        .strip()
        .lower()
        .split()
    )


def names_match(first, second):

    first = normalize_name(first)
    second = normalize_name(second)

    if not first or not second:
        return False

    if first == second:
        return True

    ratio = difflib.SequenceMatcher(
        None,
        first,
        second
    ).ratio()

    return ratio >= NAME_MATCH_THRESHOLD


def amounts_match(expected, received):

    try:

        return abs(
            float(expected) -
            float(received)
        ) < 0.01

    except:

        return False


def payment_key(request_id):

    return "payment:" + str(
        request_id
    )


def load_payment(request_id):

    return kv_get(
        payment_key(request_id)
    )


def save_payment(request_id, data):

    return kv_set(
        payment_key(request_id),
        data
    )


def authorized(expected_secret):

    supplied = request.headers.get(
        "Authorization",
        ""
    ).strip()

    return (
        supplied
        and
        expected_secret
        and
        supplied == expected_secret
    )


# ============================================================
# EXPIRY
# ============================================================

def cleanup_expired(
    request_id,
    record
):

    created_at = record.get(
        "created_at",
        now()
    )

    if (
        record.get("status")
        != "confirmed"
        and
        now() - float(created_at)
        > REQUEST_EXPIRY_SECONDS
    ):

        record["status"] = "expired"

        save_payment(
            request_id,
            record
        )

        kv_srem(
            PENDING_INDEX_KEY,
            request_id
        )

    return record


# ============================================================
# CREATE PAYMENT REQUEST
# ============================================================

@app.route(
    "/api/request-payment",
    methods=["POST"]
)
def request_payment():

    if not authorized(BOT_SECRET):

        return jsonify({
            "error": "unauthorized"
        }), 401


    data = request.get_json(
        silent=True
    ) or {}


    required = [
        "user_id",
        "amount",
        "sender_name",
        "sender_account",
        "sender_bank"
    ]


    missing = []

    for field in required:

        if not data.get(field):

            missing.append(field)


    if missing:

        return jsonify({

            "error": "missing fields",

            "fields": missing

        }), 400


    # ========================================================
    # UNIQUE REQUEST ID
    # ========================================================

    request_id = str(
        uuid.uuid4()
    )


    # ========================================================
    # PAYMENT RECORD
    # ========================================================

    record = {

        "request_id":
            request_id,

        "user_id":
            str(data["user_id"]),

        "amount":
            str(data["amount"]),

        "sender_name":
            str(
                data["sender_name"]
            ).strip(),

        "sender_account":
            str(
                data["sender_account"]
            ).strip(),

        # Bank entered by the user.
        # It is NOT taken from the Opay notification.
        "sender_bank":
            str(
                data["sender_bank"]
            ).strip(),

        "status":
            "pending",

        "created_at":
            now(),

        "notification_received":
            False,

        "notification_count":
            0,

        "matched_notification":
            None,

        "last_notification":
            None,

        "verified_at":
            None
    }


    save_payment(
        request_id,
        record
    )


    kv_sadd(
        PENDING_INDEX_KEY,
        request_id
    )


    return jsonify({

        "request_id":
            request_id,

        "status":
            "pending"
    })


# ============================================================
# RECEIVE OPAY NOTIFICATION
#
# Termux sends:
#
# {
#   "amount": "...",
#   "sender_name": "...",
#   "raw": "..."
# }
#
# NO BANK IS REQUIRED.
# ============================================================

@app.route(
    "/api/notify",
    methods=["POST"]
)
def notify():

    if not authorized(PHONE_SECRET):

        return jsonify({
            "error": "unauthorized"
        }), 401


    data = request.get_json(
        silent=True
    ) or {}


    received_amount = data.get(
        "amount"
    )

    received_sender = str(
        data.get(
            "sender_name",
            ""
        )
    ).strip()

    raw = str(
        data.get(
            "raw",
            ""
        )
    )


    if not received_amount:

        return jsonify({

            "matched":
                False,

            "verified":
                False,

            "reason":
                "no amount parsed"
        }), 400


    if not received_sender:

        return jsonify({

            "matched":
                False,

            "verified":
                False,

            "reason":
                "no sender name parsed"
        }), 400


    pending_ids = kv_smembers(
        PENDING_INDEX_KEY
    )


    matched_requests = []


    for request_id in pending_ids:

        record = load_payment(
            request_id
        )


        if not record:
            continue


        record = cleanup_expired(
            request_id,
            record
        )


        if record.get(
            "status"
        ) in [
            "expired",
            "confirmed"
        ]:

            continue


        # ====================================================
        # MATCH ONLY:
        #
        # 1. SENDER NAME
        # 2. AMOUNT
        #
        # BANK IS NOT CHECKED.
        # ====================================================

        if not names_match(
            record.get(
                "sender_name"
            ),
            received_sender
        ):

            continue


        if not amounts_match(
            record.get(
                "amount"
            ),
            received_amount
        ):

            continue


        # ====================================================
        # SAVE NOTIFICATION
        #
        # DO NOT CONFIRM PAYMENT HERE.
        # ====================================================

        record[
            "notification_received"
        ] = True


        record[
            "notification_count"
        ] = int(
            record.get(
                "notification_count",
                0
            )
        ) + 1


        record[
            "matched_notification"
        ] = raw


        record[
            "last_notification"
        ] = {

            "amount":
                str(
                    received_amount
                ),

            "sender_name":
                received_sender,

            "raw":
                raw,

            "received_at":
                now()
        }


        # Remains pending until bot verifies.
        record["status"] = "pending"


        save_payment(
            request_id,
            record
        )


        matched_requests.append(
            request_id
        )


    if matched_requests:

        return jsonify({

            "matched":
                True,

            "verified":
                False,

            "status":
                "pending",

            "request_ids":
                matched_requests,

            "message":
                "Payment notification received. Waiting for bot verification."
        })


    return jsonify({

        "matched":
            False,

        "verified":
            False,

        "status":
            "pending",

        "reason":
            "no matching pending request"
    })


# ============================================================
# VERIFY PAYMENT
# ============================================================

@app.route(
    "/api/verify/<request_id>",
    methods=["GET"]
)
def verify(request_id):

    if not authorized(BOT_SECRET):

        return jsonify({
            "error": "unauthorized"
        }), 401


    record = load_payment(
        request_id
    )


    if not record:

        return jsonify({

            "status":
                "not_found"
        }), 404


    record = cleanup_expired(
        request_id,
        record
    )


    if record.get(
        "status"
    ) == "expired":

        return jsonify({

            "status":
                "expired",

            "verified":
                False
        })


    # ========================================================
    # ALREADY CONFIRMED
    # ========================================================

    if record.get(
        "status"
    ) == "confirmed":

        return jsonify({

            "status":
                "confirmed",

            "verified":
                True,

            "detail":
                record.get(
                    "matched_notification"
                ),

            "sender_name":
                record.get(
                    "sender_name"
                ),

            "sender_bank":
                record.get(
                    "sender_bank"
                ),

            "amount":
                record.get(
                    "amount"
                )
        })


    # ========================================================
    # WAITING FOR NOTIFICATION
    # ========================================================

    if not record.get(
        "notification_received"
    ):

        return jsonify({

            "status":
                "pending",

            "verified":
                False,

            "sender_name":
                record.get(
                    "sender_name"
                ),

            "sender_bank":
                record.get(
                    "sender_bank"
                ),

            "amount":
                record.get(
                    "amount"
                )
        })


    notification = (
        record.get(
            "last_notification"
        )
        or {}
    )


    notification_amount = (
        notification.get(
            "amount"
        )
    )


    notification_sender = (
        notification.get(
            "sender_name"
        )
    )


    # ========================================================
    # CHECK AMOUNT AGAIN
    # ========================================================

    if not amounts_match(
        record.get(
            "amount"
        ),
        notification_amount
    ):

        return jsonify({

            "status":
                "pending",

            "verified":
                False,

            "reason":
                "payment amount does not match"
        })


    # ========================================================
    # CHECK SENDER NAME AGAIN
    # ========================================================

    if not names_match(
        record.get(
            "sender_name"
        ),
        notification_sender
    ):

        return jsonify({

            "status":
                "pending",

            "verified":
                False,

            "reason":
                "sender name does not match"
        })


    # ========================================================
    # CONFIRM PAYMENT
    # ========================================================

    record["status"] = "confirmed"

    record["verified_at"] = now()


    save_payment(
        request_id,
        record
    )


    # Remove from active pending requests
    kv_srem(
        PENDING_INDEX_KEY,
        request_id
    )


    return jsonify({

        "status":
            "confirmed",

        "verified":
            True,

        "detail":
            record.get(
                "matched_notification"
            ),

        "sender_name":
            record.get(
                "sender_name"
            ),

        "sender_bank":
            record.get(
                "sender_bank"
            ),

        "amount":
            record.get(
                "amount"
            )
    })


# ============================================================
# HEALTH CHECK
# ============================================================

@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({

        "ok":
            True,

        "service":
            "Opay Payment Verification API",

        "status":
            "running"
    })


@app.route(
    "/api",
    methods=["GET"]
)
def api_home():

    return jsonify({

        "ok":
            True,

        "service":
            "Opay Payment Verification API",

        "status":
            "running"
    })


@app.route(
    "/api/index",
    methods=["GET"]
)
def api_index():

    return jsonify({

        "ok":
            True,

        "service":
            "Opay Payment Verification API",

        "status":
            "running"
    })


# ============================================================
# DEBUG
# ============================================================

@app.route(
    "/api/debug-secret",
    methods=["GET"]
)
def debug_secret():

    return jsonify({

        "bot_secret_configured":
            bool(BOT_SECRET),

        "phone_secret_configured":
            bool(PHONE_SECRET),

        "bot_secret_length":
            len(BOT_SECRET),

        "phone_secret_length":
            len(PHONE_SECRET)
    })
