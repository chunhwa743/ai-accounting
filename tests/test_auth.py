"""Login, tokens, and whether the audit trail names a real person.

Identity is not decoration in an accounting system: `allocation.approved_by`
records who signed off on a set of books, and preparer/reviewer separation is
ordinary practice. A shared key cannot answer "who approved this".
"""

from __future__ import annotations

import pytest

from aiacct.auth import (
    AuthError,
    authenticate,
    create_access_token,
    decode_access_token,
    hash_password,
    user_from_token,
    verify_password,
)
from aiacct.db.models import User

PASSWORD = "aiacct-demo-2026"


@pytest.fixture
def accountant(repos):
    user = repos.users.create(
        User(name="Wei Ling Tan", email="weiling@firm.example")
    )
    repos.users.set_password(user.id, hash_password(PASSWORD))
    return user


class TestPasswordHashing:
    def test_a_correct_password_verifies(self):
        assert verify_password(PASSWORD, hash_password(PASSWORD))

    def test_a_wrong_password_does_not(self):
        assert not verify_password("wrong", hash_password(PASSWORD))

    def test_the_hash_is_not_the_password(self):
        digest = hash_password(PASSWORD)
        assert PASSWORD not in digest
        assert digest.startswith("$argon2")

    def test_the_same_password_hashes_differently_each_time(self):
        # Per-hash salt: two users sharing a password must not share a hash,
        # or the database leaks which accounts are identical.
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_a_user_with_no_password_cannot_be_verified(self):
        # A seeded row can exist before a password is set. It simply cannot
        # log in, rather than blowing up.
        assert not verify_password(PASSWORD, None)

    def test_a_malformed_hash_is_rejected_not_raised(self):
        assert not verify_password(PASSWORD, "not-a-hash")


class TestTokens:
    def test_a_token_round_trips(self, accountant):
        token, expires_in = create_access_token(accountant)
        payload = decode_access_token(token)

        assert payload["sub"] == str(accountant.id)
        assert payload["email"] == accountant.email
        assert expires_in > 0

    def test_a_tampered_token_is_rejected(self, accountant):
        token, _ = create_access_token(accountant)
        with pytest.raises(AuthError):
            decode_access_token(token[:-4] + "AAAA")

    def test_an_expired_token_is_rejected(self, accountant, offline, monkeypatch):
        monkeypatch.setattr(offline, "access_token_minutes", -1)
        token, _ = create_access_token(accountant, offline)
        with pytest.raises(AuthError, match="expired"):
            decode_access_token(token, offline)

    def test_a_token_signed_with_another_secret_is_rejected(self, accountant, offline):
        token, _ = create_access_token(accountant, offline)
        other = offline.model_copy(update={"jwt_secret": "a-completely-different-secret-value-here"})
        with pytest.raises(AuthError):
            decode_access_token(token, other)


class TestAuthenticate:
    def test_correct_credentials_return_the_user(self, repos, accountant):
        assert authenticate(repos, accountant.email, PASSWORD).id == accountant.id

    def test_login_records_the_time(self, repos, accountant):
        assert accountant.last_login_at is None
        authenticate(repos, accountant.email, PASSWORD)
        assert repos.users.get(accountant.id).last_login_at is not None

    def test_email_is_matched_case_insensitively(self, repos, accountant):
        # People type their address however they like.
        assert authenticate(repos, "WeiLing@Firm.Example", PASSWORD).id == accountant.id

    def test_a_wrong_password_is_refused(self, repos, accountant):
        with pytest.raises(AuthError):
            authenticate(repos, accountant.email, "wrong")

    def test_an_unknown_email_gives_the_same_message_as_a_wrong_password(
        self, repos, accountant
    ):
        """Otherwise the endpoint tells an attacker which addresses exist."""
        with pytest.raises(AuthError) as unknown:
            authenticate(repos, "nobody@firm.example", PASSWORD)
        with pytest.raises(AuthError) as wrong:
            authenticate(repos, accountant.email, "wrong")

        assert str(unknown.value) == str(wrong.value)

    def test_a_deactivated_account_cannot_log_in(self, repos, accountant):
        accountant.is_active = False
        repos.session.commit()

        with pytest.raises(AuthError, match="no longer active"):
            authenticate(repos, accountant.email, PASSWORD)


class TestTokenToUser:
    def test_a_valid_token_resolves(self, repos, accountant):
        token, _ = create_access_token(accountant)
        assert user_from_token(repos, token).id == accountant.id

    def test_deactivating_an_account_takes_effect_before_the_token_expires(
        self, repos, accountant
    ):
        """The database is consulted, not just the token's claims.

        Otherwise revoking someone's access would wait until their token ran
        out, which for a working-day lifetime is most of a day.
        """
        token, _ = create_access_token(accountant)
        accountant.is_active = False
        repos.session.commit()

        with pytest.raises(AuthError, match="no longer active"):
            user_from_token(repos, token)

    def test_a_token_for_a_deleted_user_is_rejected(self, repos, accountant):
        token, _ = create_access_token(accountant)
        repos.session.delete(accountant)
        repos.session.commit()

        with pytest.raises(AuthError, match="no longer exists"):
            user_from_token(repos, token)
