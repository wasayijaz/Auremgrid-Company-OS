"""Secure provider integration primitives.

The module deliberately stores references, digests, and encrypted blobs only.  Raw
OAuth credentials, webhook secrets, and authorization headers never enter an
operational row or an outbox payload.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from auremgrid.domain.errors import AuthorizationError, NotFoundError, ValidationError
from auremgrid.domain.security import AuthenticatedIdentity
from auremgrid.services.secrets import ENV_REFERENCE, EnvironmentSecretStore, redact

PROVIDERS = frozenset({"google", "slack", "figma", "github"})


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _scope(identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, capability: str) -> None:
    if identity.organization_id != organization_id:
        raise AuthorizationError("identity scope mismatch")
    if workspace_id is not None and identity.workspace_id not in {None, workspace_id}:
        raise AuthorizationError("identity workspace mismatch")
    identity.require(capability)


class EncryptedSecretVault:
    """Small authenticated local vault using a supplied deployment key.

    The deployment key is never persisted.  Ciphertext is an HMAC-authenticated
    stream encryption envelope; this keeps the base installation dependency-free
    while failing closed when no deployment key is supplied.
    """

    def __init__(self, conn: Any, new_id: Callable[[str], str], deployment_key: str | bytes | None = None) -> None:
        key = deployment_key if deployment_key is not None else os.environ.get("AUREMGRID_DEPLOYMENT_KEY")
        if isinstance(key, str):
            key = key.encode()
        if not key or len(key) < 16:
            raise ValidationError("a deployment key of at least 16 bytes is required")
        self.conn, self.new_id, self.key = conn, new_id, bytes(key)

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        chunks, counter = [], 0
        while sum(map(len, chunks)) < length:
            chunks.append(hmac.new(self.key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
            counter += 1
        return b"".join(chunks)[:length]

    def _seal(self, plaintext: str) -> str:
        nonce = secrets.token_bytes(16)
        raw = plaintext.encode()
        ciphertext = bytes(a ^ b for a, b in zip(raw, self._keystream(nonce, len(raw))))
        tag = hmac.new(self.key, nonce + ciphertext, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + tag + ciphertext).decode()

    def _open(self, envelope: str) -> str:
        try:
            raw = base64.urlsafe_b64decode(envelope.encode())
            nonce, tag, ciphertext = raw[:16], raw[16:48], raw[48:]
            if not hmac.compare_digest(tag, hmac.new(self.key, nonce + ciphertext, hashlib.sha256).digest()):
                raise ValidationError("vault ciphertext authentication failed")
            return bytes(a ^ b for a, b in zip(ciphertext, self._keystream(nonce, len(ciphertext)))).decode()
        except (ValueError, UnicodeDecodeError, base64.binascii.Error) as exc:
            raise ValidationError("vault ciphertext is invalid") from exc

    def put(self, organization_id: str, workspace_id: str | None, name: str, value: str, reference: str | None = None) -> dict[str, Any]:
        if not name.strip() or not value:
            raise ValidationError("vault name and value are required")
        if reference is not None and ENV_REFERENCE.fullmatch(reference) is None:
            raise ValidationError("secret reference must use env:UPPER_CASE_NAME")
        now = _iso(_now())
        item = {"id": self.new_id("vault"), "organization_id": organization_id, "workspace_id": workspace_id,
                "name": name.strip(), "reference": reference, "ciphertext": self._seal(value),
                "key_version": 1, "created_at": now, "updated_at": now}
        self.conn.execute("INSERT INTO local_secret_vault VALUES (?,?,?,?,?,?,?,?,?)", tuple(item.values()))
        self.conn.commit()
        return {k: v for k, v in item.items() if k != "ciphertext"}

    def upsert(self, organization_id: str, workspace_id: str | None, name: str, value: str) -> dict[str, Any]:
        """Store a rotated credential without exposing plaintext or creating duplicates."""
        if not name.strip() or not value:
            raise ValidationError("vault name and value are required")
        now = _iso(_now())
        row = self.conn.execute(
            "SELECT id FROM local_secret_vault WHERE organization_id=? AND workspace_id IS ? AND name=?",
            (organization_id, workspace_id, name.strip()),
        ).fetchone()
        if row is None:
            return self.put(organization_id, workspace_id, name, value)
        self.conn.execute(
            "UPDATE local_secret_vault SET ciphertext=?,reference=NULL,key_version=key_version+1,updated_at=? WHERE id=?",
            (self._seal(value), now, row["id"]),
        )
        self.conn.commit()
        return {"id": row["id"], "organization_id": organization_id, "workspace_id": workspace_id,
                "name": name.strip(), "key_version": int(self.conn.execute(
                    "SELECT key_version FROM local_secret_vault WHERE id=?", (row["id"],)
                ).fetchone()[0]), "updated_at": now}

    def revoke(self, organization_id: str, workspace_id: str | None, name: str) -> None:
        """Invalidate a stored credential while retaining an auditable vault row."""
        row = self.conn.execute(
            "SELECT id FROM local_secret_vault WHERE organization_id=? AND workspace_id IS ? AND name=?",
            (organization_id, workspace_id, name),
        ).fetchone()
        if row is None:
            return
        self.conn.execute(
            "UPDATE local_secret_vault SET ciphertext=?,reference=NULL,updated_at=? WHERE id=?",
            (self._seal(json.dumps({"revoked_at": _iso(_now())}, separators=(",", ":"))), _iso(_now()), row["id"]),
        )
        self.conn.commit()

    def resolve(self, organization_id: str, workspace_id: str | None, name: str) -> str:
        row = self.conn.execute("SELECT * FROM local_secret_vault WHERE organization_id=? AND workspace_id IS ? AND name=?", (organization_id, workspace_id, name)).fetchone()
        if row is None:
            raise NotFoundError("vault secret not found")
        return self._open(row["ciphertext"])


class OAuthPKCEService:
    def __init__(self, conn: Any, new_id: Callable[[str], str], redirect_allowlist: dict[str, set[str]], ttl_seconds: int = 600) -> None:
        self.conn, self.new_id, self.allowlist, self.ttl = conn, new_id, redirect_allowlist, ttl_seconds
        self._verifiers: dict[str, str] = {}

    def begin(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, provider: str, client_id: str, redirect_uri: str, scope: str, installation_id: str | None = None) -> dict[str, str]:
        _scope(identity, organization_id, workspace_id, "integration_configure")
        if provider not in PROVIDERS or redirect_uri not in self.allowlist.get(provider, set()):
            raise ValidationError("redirect URI is not allowlisted")
        if installation_id is not None:
            installation = self.conn.execute(
                "SELECT organization_id,workspace_id,provider,status FROM provider_installations WHERE id=?",
                (installation_id,),
            ).fetchone()
            if installation is None or installation["organization_id"] != organization_id or installation["provider"] != provider:
                raise ValidationError("OAuth installation is outside the requested scope")
            if workspace_id is not None and installation["workspace_id"] not in {None, workspace_id}:
                raise ValidationError("OAuth installation workspace mismatch")
            if installation["status"] == "revoked":
                raise ValidationError("OAuth installation is revoked")
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        now, expires = _now(), _now() + timedelta(seconds=self.ttl)
        self.conn.execute("INSERT INTO oauth_states VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (self.new_id("oauth"), _digest(state), organization_id, workspace_id, installation_id, provider, client_id, redirect_uri, challenge, scope, _iso(expires), None, _iso(now)))
        self.conn.commit()
        self._verifiers[_digest(state)] = verifier
        return {"state": state, "code_verifier": verifier, "code_challenge": challenge, "expires_at": _iso(expires)}

    def _consume_with_verifier(
        self, state: str, code_verifier: str | None, redirect_uri: str, provider: str
    ) -> tuple[dict[str, Any], str]:
        row = self.conn.execute("SELECT * FROM oauth_states WHERE state_digest=? AND provider=?", (_digest(state), provider)).fetchone()
        if row is None or row["used_at"] is not None:
            raise ValidationError("OAuth state is invalid or already used")
        if datetime.fromisoformat(row["expires_at"]) <= _now() or row["redirect_uri"] != redirect_uri:
            raise ValidationError("OAuth state expired or redirect mismatch")
        state_digest = _digest(state)
        verifier = code_verifier or self._verifiers.pop(state_digest, None)
        if not verifier:
            raise ValidationError("OAuth verifier handoff is unavailable")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if not hmac.compare_digest(challenge, row["code_challenge"]):
            raise ValidationError("PKCE verification failed")
        updated = self.conn.execute("UPDATE oauth_states SET used_at=? WHERE id=? AND used_at IS NULL", (_iso(_now()), row["id"]))
        if updated.rowcount != 1:
            raise ValidationError("OAuth state is already used")
        self._verifiers.pop(state_digest, None)
        self.conn.commit()
        return {
            "installation_id": row["installation_id"],
            "organization_id": row["organization_id"],
            "workspace_id": row["workspace_id"],
            "client_id": row["client_id"],
            "scope": row["scope"],
        }, verifier

    def consume(self, state: str, code_verifier: str | None, redirect_uri: str, provider: str) -> dict[str, Any]:
        context, _ = self._consume_with_verifier(state, code_verifier, redirect_uri, provider)
        return context


class OAuthConnectorService:
    """Provider-neutral OAuth install lifecycle.

    Token exchange and revocation are injected transports.  The default transport
    deliberately fails closed, so an installation can never pretend to be live
    without a configured provider adapter and real credentials.
    """

    def __init__(self, conn: Any, new_id: Callable[[str], str], vault: EncryptedSecretVault | None = None,
                 redirect_allowlist: dict[str, set[str]] | None = None,
                 exchange_transport: Callable[..., dict[str, Any]] | None = None,
                 revoke_transport: Callable[..., Any] | None = None) -> None:
        self.conn, self.new_id, self.vault = conn, new_id, vault
        self.pkce = OAuthPKCEService(conn, new_id, redirect_allowlist or {})
        self.exchange_transport, self.revoke_transport = exchange_transport, revoke_transport

    @staticmethod
    def _exchange_missing(*_: Any, **__: Any) -> dict[str, Any]:
        raise ValidationError("OAuth provider transport is not configured")

    def _vault(self) -> EncryptedSecretVault:
        if self.vault is None:
            self.vault = EncryptedSecretVault(self.conn, self.new_id)
        return self.vault

    def begin(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None,
              provider: str, client_id: str, redirect_uri: str, scope: str,
              installation_id: str | None = None) -> dict[str, str]:
        return self.pkce.begin(identity, organization_id, workspace_id, provider, client_id,
                               redirect_uri, scope, installation_id)

    def complete(self, state: str, code: str, code_verifier: str | None, redirect_uri: str, provider: str,
                 exchange_transport: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
        if not code or not isinstance(code, str):
            raise ValidationError("OAuth authorization code is required")
        context, verifier = self.pkce._consume_with_verifier(state, code_verifier, redirect_uri, provider)
        transport = exchange_transport or self.exchange_transport or self._exchange_missing
        try:
            try:
                result = transport(provider=provider, code=code, code_verifier=verifier,
                                   client_id=context["client_id"], redirect_uri=redirect_uri,
                                   scope=context["scope"])
            except TypeError:
                # Small injected test transports commonly use positional arguments.
                result = transport(provider, code, verifier, context["client_id"], redirect_uri)
        except ValidationError:
            raise
        except Exception as exc:
            raise ValidationError("OAuth provider transport failed") from exc
        if not isinstance(result, dict):
            raise ValidationError("OAuth provider transport returned an invalid response")
        access_token = result.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ValidationError("OAuth provider response did not include access_token")
        account_id = str(result.get("account_id") or result.get("sub") or result.get("email") or "").strip()
        if not account_id:
            raise ValidationError("OAuth provider response did not include a stable account_id")
        scopes = result.get("scope") or result.get("scopes") or context["scope"]
        if isinstance(scopes, str):
            scopes = tuple(item for item in scopes.split() if item)
        else:
            scopes = tuple(str(item) for item in (scopes or ()))
        expires_at = result.get("expires_at")
        if not expires_at:
            expires_in = int(result.get("expires_in") or 3600)
            expires_at = _iso(_now() + timedelta(seconds=expires_in))
        token_payload = {"access_token": access_token, "refresh_token": result.get("refresh_token"),
                        "token_type": result.get("token_type", "Bearer"), "expires_at": expires_at,
                        "scopes": list(scopes), "account_id": account_id}
        vault = self._vault()
        installation_id = context.get("installation_id") or self.new_id("install")
        if context.get("installation_id"):
            existing = self.conn.execute("SELECT * FROM provider_installations WHERE id=?", (installation_id,)).fetchone()
            if existing is None:
                raise NotFoundError("provider installation not found")
            self.conn.execute("UPDATE provider_installations SET account_id=?,account_label=?,client_id=?,redirect_uri=?,status='active',updated_at=? WHERE id=?",
                              (account_id, result.get("account_label") or result.get("email"), context["client_id"], redirect_uri, _iso(_now()), installation_id))
        else:
            now = _iso(_now())
            self.conn.execute("INSERT INTO provider_installations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                              (installation_id, context["organization_id"], context["workspace_id"], provider,
                               account_id, result.get("account_label") or result.get("email"), context["client_id"],
                               redirect_uri, None, "active", now, now))
        self.conn.commit()
        vault_name = f"oauth:{installation_id}"
        vault.upsert(context["organization_id"], context["workspace_id"], vault_name,
                     json.dumps(token_payload, separators=(",", ":"), sort_keys=True))
        return {"installation_id": installation_id, "provider": provider, "account_id": account_id,
                "account_label": result.get("account_label") or result.get("email"), "status": "active",
                "scopes": list(scopes), "expires_at": expires_at}

    def health(self, identity: AuthenticatedIdentity, installation_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM provider_installations WHERE id=?", (installation_id,)).fetchone()
        if row is None:
            raise NotFoundError("provider installation not found")
        _scope(identity, row["organization_id"], row["workspace_id"], "integration_sync")
        if row["status"] != "active":
            return {"installation_id": installation_id, "status": row["status"], "healthy": False}
        try:
            token = json.loads(self._vault().resolve(row["organization_id"], row["workspace_id"], f"oauth:{installation_id}"))
            expires_at = datetime.fromisoformat(str(token["expires_at"]))
            healthy = expires_at > _now()
            return {"installation_id": installation_id, "status": "active" if healthy else "expired",
                    "healthy": healthy, "expires_at": token.get("expires_at"), "scopes": token.get("scopes", [])}
        except (NotFoundError, ValidationError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return {"installation_id": installation_id, "status": "missing_token", "healthy": False}

    def revoke(self, identity: AuthenticatedIdentity, installation_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM provider_installations WHERE id=?", (installation_id,)).fetchone()
        if row is None:
            raise NotFoundError("provider installation not found")
        _scope(identity, row["organization_id"], row["workspace_id"], "integration_configure")
        if self.revoke_transport is not None:
            try:
                token = self._vault().resolve(row["organization_id"], row["workspace_id"], f"oauth:{installation_id}")
                self.revoke_transport(provider=row["provider"], token=json.loads(token).get("access_token"))
            except (NotFoundError, ValidationError, json.JSONDecodeError):
                pass
        try:
            self._vault().revoke(row["organization_id"], row["workspace_id"], f"oauth:{installation_id}")
        except ValidationError:
            pass
        now = _iso(_now())
        self.conn.execute("UPDATE provider_installations SET status='revoked',updated_at=? WHERE id=?", (now, installation_id))
        self.conn.commit()
        return {"installation_id": installation_id, "status": "revoked", "revoked_at": now}


class ProviderInstallationService:
    def __init__(self, conn: Any, new_id: Callable[[str], str]) -> None:
        self.conn, self.new_id = conn, new_id

    def create(self, identity: AuthenticatedIdentity, organization_id: str, workspace_id: str | None, provider: str, account_id: str, redirect_uri: str, client_id: str | None = None, account_label: str | None = None, webhook_secret_reference: str | None = None) -> dict[str, Any]:
        _scope(identity, organization_id, workspace_id, "integration_configure")
        if provider not in PROVIDERS or not account_id.strip() or not redirect_uri.strip():
            raise ValidationError("provider, account, and redirect URI are required")
        if webhook_secret_reference and ENV_REFERENCE.fullmatch(webhook_secret_reference) is None:
            raise ValidationError("webhook secret must be an env reference")
        now = _iso(_now()); item = {"id": self.new_id("install"), "organization_id": organization_id, "workspace_id": workspace_id, "provider": provider, "account_id": account_id.strip(), "account_label": account_label, "client_id": client_id, "redirect_uri": redirect_uri, "webhook_secret_reference": webhook_secret_reference, "status": "active", "created_at": now, "updated_at": now}
        self.conn.execute("INSERT INTO provider_installations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", tuple(item.values())); self.conn.commit(); return item


class WebhookIntakeService:
    def __init__(self, conn: Any, new_id: Callable[[str], str], secret_store: Any | None = None) -> None:
        self.conn, self.new_id, self.secrets = conn, new_id, secret_store or EnvironmentSecretStore()

    def receive(self, identity: AuthenticatedIdentity, installation_id: str, body: bytes, signature: str, provider_event_id: str | None = None, enqueue: Callable[[dict[str, Any]], Any] | None = None, timestamp: int | str | None = None, max_skew_seconds: int = 300) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM provider_installations WHERE id=?", (installation_id,)).fetchone()
        if row is None or row["status"] != "active": raise NotFoundError("provider installation not found")
        _scope(identity, row["organization_id"], row["workspace_id"], "integration_sync")
        if not row["webhook_secret_reference"]: raise ValidationError("webhook secret is not configured")
        secret = self.secrets.resolve(row["webhook_secret_reference"])
        if timestamp is not None:
            try:
                if abs(int(_now().timestamp()) - int(timestamp)) > max_skew_seconds:
                    raise AuthorizationError("webhook replay window exceeded")
            except (TypeError, ValueError) as exc:
                raise AuthorizationError("webhook timestamp is invalid") from exc
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        event_digest, signature_digest = _digest(body), _digest(signature)
        if not hmac.compare_digest(expected, signature):
            self.conn.execute("INSERT INTO webhook_events VALUES (?,?,?,?,?,?,?,?)", (self.new_id("webhook"), installation_id, row["organization_id"], event_digest, signature_digest, provider_event_id, _iso(_now()), "rejected")); self.conn.commit(); raise AuthorizationError("webhook signature rejected")
        try:
            self.conn.execute("INSERT INTO webhook_events VALUES (?,?,?,?,?,?,?,?)", (self.new_id("webhook"), installation_id, row["organization_id"], event_digest, signature_digest, provider_event_id, _iso(_now()), "accepted"))
        except sqlite3.IntegrityError:
            self.conn.rollback(); return {"duplicate": True, "event_digest": event_digest}
        self.conn.commit()
        if enqueue is not None: enqueue({"installation_id": installation_id, "event_digest": event_digest, "provider_event_id": provider_event_id})
        return {"duplicate": False, "event_digest": event_digest}


class OutboundSendService:
    def __init__(self, conn: Any, new_id: Callable[[str], str], jobs: Any | None = None) -> None:
        self.conn, self.new_id, self.jobs = conn, new_id, jobs

    def create_intent(self, identity: AuthenticatedIdentity, installation_id: str, approval_request_id: str, idempotency_key: str, payload: dict[str, Any], transport: Callable[[dict[str, Any]], Any] | None = None) -> dict[str, Any]:
        identity.require("external_send")
        install = self.conn.execute("SELECT * FROM provider_installations WHERE id=?", (installation_id,)).fetchone()
        if install is None or install["status"] != "active": raise NotFoundError("provider installation not found")
        _scope(identity, install["organization_id"], install["workspace_id"], "external_send")
        approval = self.conn.execute("SELECT status,organization_id FROM approval_requests WHERE id=?", (approval_request_id,)).fetchone()
        if approval is None or approval["organization_id"] != install["organization_id"] or approval["status"] != "approved": raise AuthorizationError("explicit human approval is required")
        controls = {r["key"]: r["value"] for r in self.conn.execute("SELECT key,value FROM system_state WHERE key IN ('recovery_mode','outbound_dispatch')")}
        if controls.get("recovery_mode") == "1" or controls.get("outbound_dispatch") == "disabled": raise ValidationError("outbound dispatch is blocked in recovery mode")
        safe = redact(payload)
        if safe != payload: raise ValidationError("outbound payload contains credential material")
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")); payload_hash = _digest(raw); now = _iso(_now())
        with self.conn:
            existing = self.conn.execute("SELECT * FROM outbound_send_intents WHERE organization_id=? AND idempotency_key=?", (install["organization_id"], idempotency_key)).fetchone()
            if existing is not None: return dict(existing)
            intent_id = self.new_id("send")
            self.conn.execute("INSERT INTO outbound_send_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (intent_id, install["organization_id"], install["workspace_id"], installation_id, approval_request_id, idempotency_key, raw, payload_hash, "pending", 0, None, now, now))
            outbox_id = self.new_id("outbox")
            out_payload = {"intent_id": intent_id, "installation_id": installation_id, "payload": payload}
            self.conn.execute("INSERT INTO outbox_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (outbox_id, install["organization_id"], install["workspace_id"], "outbound_send", intent_id, "provider.send", json.dumps(out_payload, sort_keys=True, separators=(",", ":")), _digest(json.dumps(out_payload, sort_keys=True, separators=(",", ":"))), idempotency_key, "pending", 0, 3, now, None, None, None, None, None, now, now, 1))
            return {"id": intent_id, "outbox_id": outbox_id, "status": "pending", "idempotency_key": idempotency_key}

    def dispatch(self, intent_id: str, transport: Callable[[dict[str, Any]], Any]) -> dict[str, Any]:
        """Deliver one intent through an injected transport with retry metadata."""
        row = self.conn.execute("SELECT * FROM outbound_send_intents WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            raise NotFoundError("outbound intent not found")
        if row["status"] == "sent":
            return dict(row)
        controls = {r["key"]: r["value"] for r in self.conn.execute("SELECT key,value FROM system_state WHERE key IN ('recovery_mode','outbound_dispatch')")}
        if controls.get("recovery_mode") == "1" or controls.get("outbound_dispatch") == "disabled":
            raise ValidationError("outbound dispatch is blocked in recovery mode")
        payload = json.loads(row["payload"])
        try:
            transport(payload)
        except Exception as exc:
            attempts = int(row["attempts"]) + 1
            status = "failed" if attempts >= 3 else "pending"
            now = _iso(_now())
            with self.conn:
                self.conn.execute("UPDATE outbound_send_intents SET status=?,attempts=?,last_error=?,updated_at=? WHERE id=?", (status, attempts, exc.__class__.__name__, now, intent_id))
                self.conn.execute("UPDATE outbox_events SET status=?,attempts=?,last_error=?,updated_at=?,version=version+1 WHERE aggregate_id=? AND aggregate_type='outbound_send'", ("failed" if status == "failed" else "pending", attempts, exc.__class__.__name__, now, intent_id))
            return dict(self.conn.execute("SELECT * FROM outbound_send_intents WHERE id=?", (intent_id,)).fetchone())
        now = _iso(_now())
        with self.conn:
            self.conn.execute("UPDATE outbound_send_intents SET status='sent',attempts=attempts+1,last_error=NULL,updated_at=? WHERE id=?", (now, intent_id))
            self.conn.execute("UPDATE outbox_events SET status='published',attempts=attempts+1,published_at=?,updated_at=?,version=version+1 WHERE aggregate_id=? AND aggregate_type='outbound_send'", (now, now, intent_id))
        return dict(self.conn.execute("SELECT * FROM outbound_send_intents WHERE id=?", (intent_id,)).fetchone())
