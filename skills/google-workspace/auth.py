"""Google Service Account Authentication & Token Management via pure REST and RS256 JWT."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse
import httpx

# Standard Google OAuth 2.0 Token Endpoint
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Allowed Google OAuth token hosts for security validation
ALLOWED_TOKEN_HOSTS = {
    "oauth2.googleapis.com",
    "accounts.google.com",
    "www.googleapis.com",
}

# OAuth Scopes for Google Workspace APIs
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

# In-memory token cache keyed by credential identity fingerprint + subject
# Shape: {cache_key: (access_token, expiration_timestamp)}
_TOKEN_CACHE: Dict[str, tuple[str, float]] = {}


def _base64url_encode(data: bytes) -> str:
    """Encode bytes to a URL-safe Base64 string without padding."""
    import base64
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _compute_credential_fingerprint(sa_info: Dict[str, Any], subject: Optional[str] = None) -> str:
    """Compute a non-secret deterministic fingerprint for credential identity including key hash and optional subject."""
    client_email = sa_info.get("client_email", "")
    private_key_id = sa_info.get("private_key_id", "")
    project_id = sa_info.get("project_id", "")
    private_key_raw = sa_info.get("private_key", "")
    key_sha = hashlib.sha256(private_key_raw.encode("utf-8")).hexdigest()[:16]
    sub_val = (subject or "").strip()
    key_material = f"{project_id}:{client_email}:{private_key_id}:{key_sha}:{sub_val}".encode("utf-8")
    return hashlib.sha256(key_material).hexdigest()


def parse_service_account_info(raw_info: Optional[str]) -> Dict[str, Any]:
    """Parse and validate Google Service Account JSON credentials with token_uri validation."""
    if not raw_info or not str(raw_info).strip():
        raise ValueError(
            "Missing Service Account configuration. Please configure 'GOOGLE_SERVICE_ACCOUNT_JSON' in Settings -> Secrets."
        )

    try:
        data = json.loads(str(raw_info).strip())
    except Exception as exc:
        raise ValueError(f"GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: {exc}") from None

    if not isinstance(data, dict):
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object.")

    if data.get("type") != "service_account":
        raise ValueError(f"Invalid credential type '{data.get('type')}'. Expected 'service_account'.")

    required_keys = ["client_email", "private_key", "token_uri"]
    missing = [k for k in required_keys if not data.get(k)]
    if missing:
        raise ValueError(f"GOOGLE_SERVICE_ACCOUNT_JSON missing required keys: {missing}")

    # Validate token_uri belongs to Google OAuth endpoint
    token_uri = str(data.get("token_uri", "")).strip()
    parsed_url = urlparse(token_uri)
    if parsed_url.scheme != "https" or parsed_url.netloc not in ALLOWED_TOKEN_HOSTS:
        raise ValueError(
            f"Invalid token_uri '{token_uri}'. Must be a secure Google OAuth2 endpoint (e.g. '{GOOGLE_TOKEN_URI}')."
        )

    return data


def create_signed_jwt(sa_info: Dict[str, Any], subject: Optional[str] = None) -> str:
    """Create a signed RS256 JWT assertion for Google OAuth2 token exchange."""
    # Registration and execution have separate isolated-dependency scopes.
    # Load native crypto only in the tool child that performs the signing;
    # retaining its classes across registration cleanup breaks Rust type checks.
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    client_email = sa_info["client_email"]
    private_key_pem = sa_info["private_key"]
    token_uri = sa_info.get("token_uri", GOOGLE_TOKEN_URI)

    now = int(time.time())
    expiry = now + 3600  # 1 hour lifetime

    # Standard Service Account assertion: 'iss' is client_email, 'aud' is token_uri
    # 'sub' is intentionally omitted by default for standard service account access.
    # It is included only if explicit domain-wide delegation subject is supplied.
    payload: Dict[str, Any] = {
        "iss": client_email,
        "scope": " ".join(GOOGLE_SCOPES),
        "aud": token_uri,
        "exp": expiry,
        "iat": now,
    }
    if subject and subject.strip():
        payload["sub"] = subject.strip()

    header = {
        "alg": "RS256",
        "typ": "JWT",
    }
    if "private_key_id" in sa_info:
        header["kid"] = sa_info["private_key_id"]

    header_b64 = _base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")

    # Load private RSA key
    try:
        private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"),
            password=None,
        )
    except Exception as exc:
        raise ValueError(f"Failed to load Service Account private_key: {exc}") from None

    if not isinstance(private_key, RSAPrivateKey):
        raise ValueError("Service Account private key must be an RSA private key.")

    signature = private_key.sign(
        signing_input,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    sig_b64 = _base64url_encode(signature)

    return f"{header_b64}.{payload_b64}.{sig_b64}"


def get_access_token(
    raw_info: Optional[str],
    http_client: Optional[httpx.Client] = None,
    force_refresh: bool = False,
    subject: Optional[str] = None,
) -> str:
    """Obtain a valid OAuth 2.0 Bearer access token using credential- and subject-keyed caching."""
    sa_info = parse_service_account_info(raw_info)
    fingerprint = _compute_credential_fingerprint(sa_info, subject=subject)
    now = time.time()

    if not force_refresh and fingerprint in _TOKEN_CACHE:
        cached_token, expiry = _TOKEN_CACHE[fingerprint]
        # Use cached token if valid for at least 60 more seconds
        if now < expiry - 60:
            return cached_token

    # Build signed JWT and exchange for Bearer token
    jwt_assertion = create_signed_jwt(sa_info, subject=subject)
    token_uri = sa_info.get("token_uri", GOOGLE_TOKEN_URI)

    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": jwt_assertion,
    }

    client = http_client or httpx.Client(timeout=30.0)
    should_close_client = http_client is None

    try:
        response = client.post(token_uri, data=data)
        if response.status_code != 200:
            err_msg = response.text[:500]
            raise RuntimeError(
                f"Google OAuth2 token exchange failed ({response.status_code} {response.reason_phrase}): {err_msg}"
            )

        resp_json = response.json()
        access_token = resp_json.get("access_token")
        expires_in = resp_json.get("expires_in", 3600)

        if not access_token:
            raise RuntimeError("OAuth2 token response did not contain 'access_token'.")

        _TOKEN_CACHE[fingerprint] = (access_token, now + expires_in)
        return access_token
    finally:
        if should_close_client:
            client.close()
