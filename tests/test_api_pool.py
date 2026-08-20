"""The request lifecycle must return its database connection.

FastAPI only runs teardown for dependencies that *yield*. A dependency that
returns is never cleaned up, so ``return Repositories.open()`` leaks one
connection per request and the pool runs dry after pool_size + max_overflow
requests - the app works fine, then dies under ordinary use.
"""

from __future__ import annotations

import inspect

from fastapi.testclient import TestClient

from aiacct.api.main import app, get_repos
from aiacct.auth import create_access_token
from aiacct.db.models import User
from aiacct.db.session import get_engine


def test_get_repos_is_a_generator_so_fastapi_can_close_it():
    assert inspect.isgeneratorfunction(get_repos), (
        "get_repos must yield, not return - FastAPI skips teardown for "
        "dependencies that return, leaking a connection per request"
    )


def test_many_requests_do_not_exhaust_the_pool(offline):
    """More requests than the pool can hold, without a TimeoutError.

    The token is well-formed but names a user that does not exist. That is
    deliberate: it makes ``user_from_token`` run a real query - and so check
    out a real connection - before the request is rejected. A request with no
    token at all is refused before touching the database, which would leak
    nothing and prove nothing.
    """
    engine = get_engine()
    limit = engine.pool.size() + engine.pool._max_overflow

    ghost = User(name="Nobody", email="nobody@firm.example")
    ghost.id = 10_000_000
    token, _ = create_access_token(ghost)

    before = engine.pool.checkedout()

    with TestClient(app) as client:
        for _ in range(limit * 3):
            response = client.get(
                "/api/v1/clients", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401, response.text

    leaked = engine.pool.checkedout() - before
    assert leaked == 0, (
        f"{leaked} connection(s) leaked across {limit * 3} requests; "
        f"get_repos is not returning them to the pool"
    )
