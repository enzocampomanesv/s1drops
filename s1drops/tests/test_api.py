"""The API behind the Space4All page: Space4All sign-ins, the tool's roles, the
gate (verify email, ask for access, approval), areas and user management."""
import datetime
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from google.auth import crypt, jwt

from s1drops.api import auth, main
from s1drops.app import accounts
from s1drops.cube import registry

PROJECT = "test-project"


@pytest.fixture(scope="module")
def key():
    """A throwaway signing key and its certificate, standing in for Google's."""
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
            .serial_number(1).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(k, hashes.SHA256()))
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                          serialization.NoEncryption()).decode()
    return crypt.RSASigner.from_string(pem, key_id="kid1"), cert.public_bytes(serialization.Encoding.PEM).decode()


@pytest.fixture
def client(tmp_path, monkeypatch, key):
    monkeypatch.setenv("S1DROPS_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("S1DROPS_ADMIN_EMAILS", "boss@example.org")
    monkeypatch.setenv("S1DROPS_FIREBASE_PROJECT", PROJECT)
    monkeypatch.setenv("S1DROPS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(auth, "_google_certs", lambda: {"kid1": key[1]})
    registry.save_registry(tmp_path / "cache", {"lagos_100m": {
        "key": "lagos_100m", "name": "lagos", "path": "x", "crs": "EPSG:32631", "grid_transform": [100, 0, 0, 0, -100, 0],
        "grid_shape": [10, 10], "bbox_ll": [3.3, 6.4, 3.5, 6.6], "start": "2015-01-01", "end": "2026-11-01",
        "resolution": 100.0, "reduction": "native_median", "n_passes": 500, "created_at": "2026-11-01"}})
    return TestClient(main.app)


def token(signer, uid="u1", email="ada@example.org", verified=True, **over):
    now = int(time.time())
    claims = {"iss": f"https://securetoken.google.com/{PROJECT}", "aud": PROJECT, "sub": uid, "iat": now,
              "exp": now + 600, "email": email, "email_verified": verified, **over}
    return {"Authorization": "Bearer " + jwt.encode(signer, claims).decode()}


def test_sign_in_is_checked(client, key):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/api/me", headers=token(key[0], aud="other-project")).status_code == 401
    assert client.get("/api/me", headers=token(key[0], iss="https://securetoken.google.com/other")).status_code == 401
    assert client.get("/api/me", headers=token(key[0], exp=int(time.time()) - 3600)).status_code == 401
    me = client.get("/api/me", headers=token(key[0])).json()
    assert me == {"email": "ada@example.org", "emailVerified": True, "role": None, "roleLabel": None, "approved": False,
                  "name": "", "affiliation": "", "canReview": False, "isAdmin": False}


def test_asking_for_access_and_approval(client, key):
    ada = token(key[0])
    assert client.get("/api/areas", headers=ada).status_code == 403   # nothing without access
    unverified = token(key[0], uid="u9", email="eve@example.org", verified=False)
    assert client.post("/api/access", headers=unverified, json={"name": "Eve", "reason": "x"}).status_code == 403
    r = client.post("/api/access", headers=ada, json={"name": "Ada", "affiliation": "NGO", "reason": "Field checks"})
    assert r.json() == {"role": "pending", "roleLabel": "Waiting for approval", "approved": False}
    assert client.get("/api/me", headers=ada).json()["role"] == "pending"
    assert client.get("/api/areas", headers=ada).status_code == 403
    # a bootstrap admin asking is in at once, and approves Ada
    boss = token(key[0], uid="b1", email="boss@example.org")
    assert client.post("/api/access", headers=boss, json={"name": "Boss", "reason": "Runs it"}).json()["approved"] is True
    users = client.get("/api/users", headers=boss).json()
    ada_id = next(u["id"] for u in users if u["email"] == "ada@example.org")
    assert next(u for u in users if u["id"] == ada_id)["reason"] == "Field checks"
    assert client.get("/api/users", headers=ada).status_code == 403
    assert client.put(f"/api/users/{ada_id}/role", headers=boss, json={"role": "validator"}).status_code == 200
    me = client.get("/api/me", headers=ada).json()
    assert me["approved"] and me["roleLabel"] == "Validator" and not me["canReview"]
    areas = client.get("/api/areas", headers=ada).json()
    assert areas == [{"key": "lagos_100m", "id": "lagos_100m", "name": "Lagos", "bounds": [3.3, 6.4, 3.5, 6.6], "start": "2015-01-01",
                      "end": "2026-11-01", "resolution": 100.0, "fast": False, "passes": 500}]
    assert client.post("/api/access", headers=ada, json={"name": "Ada", "reason": "again"}).status_code == 409
    # disabled: out again
    client.put(f"/api/users/{ada_id}/role", headers=boss, json={"role": "disabled"})
    assert client.get("/api/areas", headers=ada).status_code == 403
    assert client.put(f"/api/users/{ada_id}/role", headers=boss, json={"role": "emperor"}).status_code == 400


def test_existing_accounts_carry_over_by_verified_email(client, key):
    """Someone who used the old sign-in keeps their role, but only once their
    Space4All email is verified: an unverified address can't take it over."""
    old = accounts.sign_up("ada@example.org", "Ada", "a long password")
    with accounts.connect() as con:
        con.execute("UPDATE users SET role = 'reviewer' WHERE id = ?", (old.id,))
    impostor = token(key[0], uid="other", verified=False)
    assert client.get("/api/me", headers=impostor).json()["role"] is None
    r = client.post("/api/access", headers=impostor, json={"name": "Not Ada", "reason": "x"})
    assert r.status_code == 403
    me = client.get("/api/me", headers=token(key[0], uid="real")).json()
    assert me["role"] == "reviewer" and me["canReview"] and me["name"] == "Ada"
    # linked to that Space4All account from now on, whatever the token's email says
    assert client.get("/api/me", headers=token(key[0], uid="real", email="ada@new.org", verified=False)).json()["role"] == "reviewer"
    assert client.get("/api/me", headers=token(key[0], uid="third")).json()["role"] is None  # already linked to "real"


def test_cors_only_for_space4all(client):
    ok = client.options("/api/me", headers={"Origin": "https://space4all.web.app", "Access-Control-Request-Method": "GET",
                                            "Access-Control-Request-Headers": "authorization"})
    assert ok.headers.get("access-control-allow-origin") == "https://space4all.web.app"
    bad = client.options("/api/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in bad.headers


def test_migration_adds_the_column_to_an_old_database(tmp_path, monkeypatch):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(accounts._SCHEMA)
    con.execute("INSERT INTO users (email, role, created_at) VALUES ('x@example.org', 'admin', 1)")
    con.commit()
    con.close()
    monkeypatch.setenv("S1DROPS_DB_PATH", str(path))
    assert accounts.space4all_user("uid-x", "x@example.org", True).role == "admin"
    assert accounts.space4all_user("uid-x", "", False).email == "x@example.org"
