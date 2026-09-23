import hashlib
from urllib.parse import parse_qs, urlparse

import pytest

from app import oidc


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def json(self, content_type=None):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return next(self.responses)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return next(self.responses)


def test_pkce_pair_and_device_id_shape():
    verifier, challenge = oidc.new_pkce_pair()

    assert challenge == oidc._b64url(hashlib.sha256(verifier.encode()).digest())
    assert "=" not in verifier + challenge
    assert len(oidc.new_device_id(16)) == 16
    assert oidc.new_device_id(16).isalnum()


def test_build_authorize_url_contains_matrix_device_scope():
    url = oidc.build_authorize_url(
        {"authorization_endpoint": "https://id.example.test/authorize"},
        "client",
        "https://app.example.test/callback",
        "state",
        "challenge",
        "DEVICE",
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.path == "/authorize"
    assert params["state"] == ["state"]
    assert params["code_challenge_method"] == ["S256"]
    assert "urn:matrix:client:device:DEVICE" in params["scope"][0]


@pytest.mark.asyncio
async def test_discover_follows_advertised_issuer():
    session = FakeSession(
        [
            FakeResponse(200, {"org.matrix.msc2965.authentication": {"issuer": "https://id.test/"}}),
            FakeResponse(200, {"authorization_endpoint": "https://id.test/auth"}),
        ]
    )

    result = await oidc.discover(session, "https://matrix.test")

    assert result["authorization_endpoint"] == "https://id.test/auth"
    assert session.requests[1][1] == "https://id.test/.well-known/openid-configuration"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (FakeResponse(404, {}), "Could not fetch"),
        (FakeResponse(200, ValueError("bad json")), "wasn't valid JSON"),
        (FakeResponse(200, {}), "does not advertise OIDC"),
    ],
)
async def test_discover_rejects_invalid_well_known(response, message):
    with pytest.raises(oidc.OIDCError, match=message):
        await oidc.discover(FakeSession([response]), "https://matrix.test")


@pytest.mark.asyncio
async def test_register_client_requires_endpoint_and_posts_metadata():
    with pytest.raises(oidc.OIDCError, match="no registration_endpoint"):
        await oidc.register_client(FakeSession([]), {}, "redirect", "name", "uri")

    session = FakeSession([FakeResponse(201, {"client_id": "new-client"})])
    result = await oidc.register_client(
        session,
        {"registration_endpoint": "https://id.test/register"},
        "https://app.test/callback",
        "Matrix Search Hub",
        "https://app.test",
    )
    assert result == {"client_id": "new-client"}
    assert session.requests[0][2]["json"]["grant_types"] == ["authorization_code", "refresh_token"]


@pytest.mark.asyncio
async def test_token_operations_and_whoami_send_expected_credentials():
    exchange = FakeSession([FakeResponse(200, {"access_token": "a"})])
    assert await oidc.exchange_code(exchange, {"token_endpoint": "https://id/token"}, "c", "s", "r", "code", "v") == {
        "access_token": "a"
    }
    assert exchange.requests[0][2]["data"]["client_secret"] == "s"

    refresh = FakeSession([FakeResponse(200, {"access_token": "b"})])
    await oidc.refresh_token(refresh, {"token_endpoint": "https://id/token"}, "c", None, "refresh")
    assert "client_secret" not in refresh.requests[0][2]["data"]

    whoami = FakeSession([FakeResponse(200, {"user_id": "@alice:test"})])
    assert await oidc.whoami(whoami, "https://matrix.test", "token") == {"user_id": "@alice:test"}
    assert whoami.requests[0][2]["headers"] == {"Authorization": "Bearer token"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        lambda session: oidc.register_client(
            session, {"registration_endpoint": "https://id/register"}, "r", "n", "u"
        ),
        lambda session: oidc.exchange_code(session, {"token_endpoint": "https://id/token"}, "c", None, "r", "x", "v"),
        lambda session: oidc.refresh_token(session, {"token_endpoint": "https://id/token"}, "c", None, "r"),
        lambda session: oidc.whoami(session, "https://matrix.test", "token"),
    ],
)
async def test_remote_operations_raise_on_error_status(operation):
    with pytest.raises(oidc.OIDCError):
        await operation(FakeSession([FakeResponse(400, {"error": "invalid"})]))
