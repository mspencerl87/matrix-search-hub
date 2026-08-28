import base64
import hashlib
import secrets
import string
from urllib.parse import urlencode

import aiohttp


class OIDCError(Exception):
    pass


async def discover(session: aiohttp.ClientSession, server_name: str) -> dict:
    # .well-known/matrix/client is hosted on the server_name (the domain in
    # user IDs, e.g. example.com for @you:example.com) - NOT on the resolved
    # client-server API base URL (e.g. matrix.example.com), which is often a
    # different host entirely. Fetching it from the API host is a common
    # mistake since that host may still return 200 with an empty/unrelated
    # body for the path instead of a clean 404.
    async with session.get(f"{server_name}/.well-known/matrix/client") as resp:
        if resp.status != 200:
            raise OIDCError(f"Could not fetch {server_name}/.well-known/matrix/client ({resp.status})")
        try:
            wellknown = await resp.json(content_type=None)
        except Exception as e:
            raise OIDCError(
                f"{server_name}/.well-known/matrix/client returned a 200 but the body "
                f"wasn't valid JSON. Double-check MATRIX_SERVER_NAME is your account's "
                f"server name (the domain after ':' in your Matrix ID), not the "
                f"client-server API base URL."
            ) from e

    auth_meta = wellknown.get("org.matrix.msc2965.authentication")
    if not auth_meta:
        raise OIDCError(
            "This homeserver does not advertise OIDC-native auth "
            "(org.matrix.msc2965.authentication in .well-known/matrix/client). "
            "matrix-search-hub only supports MSC2965/Matrix-Authentication-Service "
            "style homeservers - for password/legacy-SSO homeservers, use the "
            "single-user matrix-search project instead."
        )
    issuer = auth_meta["issuer"].rstrip("/")

    async with session.get(f"{issuer}/.well-known/openid-configuration") as resp:
        if resp.status != 200:
            raise OIDCError(f"Could not fetch OIDC discovery document from {issuer} ({resp.status})")
        return await resp.json(content_type=None)


async def register_client(session: aiohttp.ClientSession, discovery: dict, redirect_uri: str, client_name: str, client_uri: str) -> dict:
    registration_endpoint = discovery.get("registration_endpoint")
    if not registration_endpoint:
        raise OIDCError(
            "Identity provider has no registration_endpoint - dynamic client "
            "registration isn't supported here. Set OAUTH_CLIENT_ID/"
            "OAUTH_CLIENT_SECRET from a client an admin registers manually instead."
        )
    payload = {
        "client_name": client_name,
        "client_uri": client_uri,
        "redirect_uris": [redirect_uri],
        "response_types": ["code"],
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "client_secret_post",
        "application_type": "web",
    }
    async with session.post(registration_endpoint, json=payload) as resp:
        body = await resp.json(content_type=None)
        if resp.status not in (200, 201):
            raise OIDCError(f"Client registration failed ({resp.status}): {body}")
        return body


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def new_pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def new_device_id(length: int = 10) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def build_authorize_url(discovery, client_id, redirect_uri, state, code_challenge, device_id) -> str:
    # urn:matrix:client:device:<id> ties the granted OAuth session to a
    # specific Matrix device, per MSC2967 - required for a client we intend
    # to keep using long-term rather than a one-off token grant.
    scope = f"openid urn:matrix:client:api:* urn:matrix:client:device:{device_id}"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{discovery['authorization_endpoint']}?{urlencode(params)}"


async def exchange_code(session, discovery, client_id, client_secret, redirect_uri, code, code_verifier) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with session.post(discovery["token_endpoint"], data=data) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise OIDCError(f"Token exchange failed ({resp.status}): {body}")
        return body


async def refresh_token(session, discovery, client_id, client_secret, refresh_token_value) -> dict:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_value,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    async with session.post(discovery["token_endpoint"], data=data) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise OIDCError(f"Token refresh failed ({resp.status}): {body}")
        return body


async def whoami(session, homeserver, access_token) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with session.get(f"{homeserver}/_matrix/client/v3/account/whoami", headers=headers) as resp:
        body = await resp.json(content_type=None)
        if resp.status != 200:
            raise OIDCError(f"whoami failed ({resp.status}): {body}")
        return body
