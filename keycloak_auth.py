# keycloak_auth.py

from flask import request, jsonify
from jose import jwt
import requests
from cachetools import TTLCache
import os

# Keycloak Config
KEYCLOAK_ISSUER = os.getenv("KEYCLOAK_ISSUER") 
KEYCLOAK_JWKS_URL = f"{KEYCLOAK_ISSUER}/protocol/openid-connect/certs"
KEYCLOAK_AUDIENCE = os.getenv("KEYCLOAK_AUDIENCE")

# Cache JWKS
jwks_cache = TTLCache(maxsize=1, ttl=3600)

def get_jwks():
    """Retrieve JWKS from Keycloak (cached for performance)."""
    if "jwks" in jwks_cache:
        return jwks_cache["jwks"]
    jwks = requests.get(KEYCLOAK_JWKS_URL).json()
    jwks_cache["jwks"] = jwks
    return jwks

def get_signing_key(token):
    jwks = requests.get(KEYCLOAK_JWKS_URL).json()
    header = jwt.get_unverified_header(token)
    kid = header["kid"]

    for key in jwks["keys"]:
        if key["kid"] == kid:
            return key

    raise Exception("Public key not found in JWKS")

def verify_token(token):
    """Verify and decode JWT using Keycloak public keys."""
    try:
        key = get_signing_key(token)

        return jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=KEYCLOAK_AUDIENCE,
            issuer=KEYCLOAK_ISSUER,
        )
    except Exception as e:
        raise Exception(f"Invalid token: {e}")


def keycloak_protect(f):
    """Flask decorator to protect routes using Keycloak JWT authentication."""

    def wrapper(*args, **kwargs):
        auth_header = request.headers.get("Authorization")

        if not auth_header:
            return jsonify({"error": "Authorization header missing"}), 401

        parts = auth_header.split()

        if parts[0].lower() != "bearer" or len(parts) != 2:
            return jsonify({"error": "Invalid Authorization header"}), 401

        token = parts[1]

        try:
            user = verify_token(token)
            request.user = user  # attach decoded JWT payload
        except Exception as e:
            return jsonify({"error": str(e)}), 401

        return f(*args, **kwargs)

    wrapper.__name__ = f.__name__
    return wrapper

def check_role(user, meeting_uuid, role):
    role_string = f"z-{meeting_uuid}-{role}"
    return role_string in user["realm_access"]["roles"]