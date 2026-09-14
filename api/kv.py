"""
Storage layer for the payment verification API.

Uses Vercel KV (Redis, via its REST API) in production. When the
KV_REST_API_URL / KV_REST_API_TOKEN environment variables aren't set (e.g.
while testing locally before deploying), it falls back to a local in-memory
dict automatically - so you can run and test this on your own machine
without needing real Vercel KV credentials yet.

Vercel KV env vars are injected automatically once you add the KV storage
to your Vercel project and link it - you don't set them by hand.
"""

import os
import json
import requests

KV_URL = os.environ.get("KV_REST_API_URL")
KV_TOKEN = os.environ.get("KV_REST_API_TOKEN")

USE_LOCAL_FALLBACK = not (KV_URL and KV_TOKEN)

# local fallback storage (only used when Vercel KV env vars are absent)
_local_store = {}
_local_sets = {}


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
    requests.post(
        f"{KV_URL}/set/{key}",
        headers=_headers(),
        data=json.dumps(value),
        timeout=10,
    )


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
