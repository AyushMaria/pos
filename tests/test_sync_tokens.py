"""The refresh token, finally read — phase 7 debt card.

Every test here exists because of one fact: a till signed in at 8am stopped
syncing at 9am, and nothing said why. The access token lived for an hour, the
refresh token was saved at login and never read, and every 401 after that was
treated as a network blip.

Three layers, tested at three seams:

  * `TokenRefresher` on its own, with a fake exchange and the in-memory
    keychain — the four outcomes and what each one does to the keychain.
  * The pusher's reactive path — a 401 mid-drain triggers exactly one refresh,
    and the *same* batch goes again under the *new* token.
  * The engine's proactive path — a token inside its last ten minutes is
    renewed before the push, and a dead one turns into `needs_signin`.

Plus one positive control on `build_app`, because a refresher that is never
wired to the engine passes every test above and still leaves the till dead
at 9am. "A test that constructs its own input cannot tell you where
production gets one."
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from app.api.server import build_app
from app.config import Settings
from app.data.db import Database
from app.data.repositories.outbox import OutboxRepository
from app.security import keychain
from app.sync.backoff import Backoff
from app.sync.payloads import PayloadBuilder
from app.sync.puller import Puller
from app.sync.pusher import Pusher
from app.sync.tokens import REFRESH_MARGIN, SIGN_IN_NEEDED, TokenRefresher, expires_at
from tests.conftest import FAST_ARGON2, TEST_MAC_KEY, TEST_TOKEN, settle_opening
from tests.test_sync_engine import an_engine
from tests.test_sync_push import TERMINAL_ID, FakeCloud, sell

STORE, TERMINAL = "ST01", "T1"


def token_expiring_in(delta: timedelta) -> str:
    """An unsigned JWT whose only claim that matters is `exp`."""
    exp = datetime.now(timezone.utc) + delta
    return jwt.encode({"exp": int(exp.timestamp()), "sub": "x"}, "x" * 32, algorithm="HS256")


class Rejected(Exception):
    """Stands in for `InvalidCredentials`, which `app.sync` may not import."""


class Exchange:
    """A fake `SupabaseAuthClient.refresh` that remembers what it was given."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls: list[str] = []
        self.minted = 0

    async def __call__(self, refresh_token: str) -> tuple[str, str]:
        self.calls.append(refresh_token)
        if self.raises is not None:
            raise self.raises
        self.minted += 1
        return f"access-{self.minted}", f"refresh-{self.minted}"


class Holder:
    """The session store's part: one current token, replaceable."""

    def __init__(self, token: str | None) -> None:
        self.token = token
        self.replaced: list[str] = []

    def current(self) -> str | None:
        return self.token

    def replace(self, token: str) -> None:
        self.token = token
        self.replaced.append(token)


def refresher(holder: Holder, exchange: Exchange) -> TokenRefresher:
    return TokenRefresher(
        exchange=exchange,
        current_token=holder.current,
        replace_token=holder.replace,
        store_code=STORE,
        terminal_code=TERMINAL,
        is_rejection=lambda exc: isinstance(exc, Rejected),
    )


# ── TokenRefresher on its own ───────────────────────────────────────────────


def test_expires_at_reads_the_claim_without_a_signature() -> None:
    soon = token_expiring_in(timedelta(minutes=5))
    exp = expires_at(soon)
    assert exp is not None
    assert abs((exp - datetime.now(timezone.utc)) - timedelta(minutes=5)) < timedelta(seconds=5)
    assert expires_at("not-a-jwt") is None


def test_expiring_is_the_last_margin_and_nothing_before_it() -> None:
    now = datetime.now(timezone.utc)
    fresh = refresher(Holder(token_expiring_in(timedelta(minutes=50))), Exchange())
    stale = refresher(Holder(token_expiring_in(REFRESH_MARGIN - timedelta(minutes=1))), Exchange())
    gone = refresher(Holder(token_expiring_in(-timedelta(minutes=1))), Exchange())
    signed_out = refresher(Holder(None), Exchange())

    assert not fresh.expiring(now)
    assert stale.expiring(now)
    assert gone.expiring(now)
    assert not signed_out.expiring(now)


@pytest.mark.asyncio
async def test_a_refresh_replaces_both_tokens(in_memory_keychain: dict[str, str]) -> None:
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    holder, exchange = Holder("access-0"), Exchange()

    outcome = await refresher(holder, exchange).renew()

    assert outcome == "refreshed"
    assert exchange.calls == ["refresh-0"], "must exchange the token login saved"
    assert holder.replaced == ["access-1"]
    # Supabase rotates refresh tokens: the old one is spent, so keeping it
    # would make the *next* refresh fail and look like a dead session.
    assert keychain.load_refresh_token(STORE, TERMINAL) == "refresh-1"


@pytest.mark.asyncio
async def test_no_stored_refresh_token_is_dead(in_memory_keychain: dict[str, str]) -> None:
    holder, exchange = Holder("access-0"), Exchange()

    assert await refresher(holder, exchange).renew() == "dead"
    assert exchange.calls == []
    assert holder.replaced == []


@pytest.mark.asyncio
async def test_a_rejected_refresh_token_is_dead(in_memory_keychain: dict[str, str]) -> None:
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    holder, exchange = Holder("access-0"), Exchange(raises=Rejected())

    assert await refresher(holder, exchange).renew() == "dead"
    assert holder.replaced == []


@pytest.mark.asyncio
async def test_a_network_failure_is_unreachable_not_dead(
    in_memory_keychain: dict[str, str],
) -> None:
    """The difference the card is about: the cable being out must not read
    as the session being over."""
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    holder, exchange = Holder("access-0"), Exchange(raises=httpx.ConnectError("down"))

    assert await refresher(holder, exchange).renew() == "unreachable"
    assert holder.replaced == []
    assert keychain.load_refresh_token(STORE, TERMINAL) == "refresh-0"


@pytest.mark.asyncio
async def test_signed_out_is_nothing_to_do(in_memory_keychain: dict[str, str]) -> None:
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    exchange = Exchange()

    assert await refresher(Holder(None), exchange).renew() == "nothing"
    assert exchange.calls == []


@pytest.mark.asyncio
async def test_ensure_fresh_leaves_a_young_token_alone(
    in_memory_keychain: dict[str, str],
) -> None:
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    exchange = Exchange()
    young = refresher(Holder(token_expiring_in(timedelta(minutes=50))), exchange)
    old = refresher(Holder(token_expiring_in(timedelta(minutes=3))), exchange)
    now = datetime.now(timezone.utc)

    assert await young.ensure_fresh(now) == "nothing"
    assert exchange.calls == []
    assert await old.ensure_fresh(now) == "refreshed"
    assert exchange.calls == ["refresh-0"]


# ── The pusher's reactive path ──────────────────────────────────────────────


class BearerAware(FakeCloud):
    """A FakeCloud that also remembers which token each call carried."""

    def __init__(self) -> None:
        super().__init__()
        self.bearers: list[str] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.bearers.append(request.headers.get("Authorization", ""))
        return await super().handle(request)


def pusher_for(db: Database, outbox: OutboxRepository, cloud: FakeCloud, holder: Holder) -> Pusher:
    return Pusher(
        outbox,
        PayloadBuilder(db, terminal_id=TERMINAL_ID),
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=holder.current,
        backoff=Backoff(first_seconds=0.0, cap_seconds=0.0),
        client=cloud.client,
    )


@pytest.fixture
def till(till: TestClient, db: Database) -> TestClient:
    """See `settle_opening`: the queue starts empty of the fixture shift."""
    settle_opening(db)
    return till


@pytest.fixture
def outbox(db: Database) -> OutboxRepository:
    return OutboxRepository(db)


@pytest.fixture
def cloud() -> FakeCloud:
    return FakeCloud()


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_resends_the_same_batch_under_the_new_token(
    till: Any, db: Database, outbox: OutboxRepository, in_memory_keychain: dict[str, str]
) -> None:
    sell(till)
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    cloud = BearerAware()
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(401, json={"message": "JWT expired"})
    holder, exchange = Holder("access-0"), Exchange()
    pusher = pusher_for(db, outbox, cloud, holder)
    pusher.renew = refresher(holder, exchange).renew

    result = await pusher.drain()

    assert not result.stopped_early
    assert result.pushed == 1 and result.quarantined == 0
    assert outbox.backlog() == 0
    assert exchange.calls == ["refresh-0"]
    # The retry must carry the token the refresh minted, not the one that was
    # just refused — a pusher that re-reads a stale provider would loop.
    assert cloud.bearers == ["Bearer access-0", "Bearer access-1"]


@pytest.mark.asyncio
async def test_a_dead_refresh_token_stops_the_drain_and_says_sign_in(
    till: Any, db: Database, outbox: OutboxRepository, in_memory_keychain: dict[str, str]
) -> None:
    """Nothing is refused, nothing is quarantined, nothing is retried in a
    loop. The queue waits for a person, and the message says so."""
    sell(till)
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    cloud = BearerAware()
    cloud.fail_times = 5
    cloud.fail_with = httpx.Response(401, json={"message": "JWT expired"})
    holder, exchange = Holder("access-0"), Exchange(raises=Rejected())
    pusher = pusher_for(db, outbox, cloud, holder)
    pusher.renew = refresher(holder, exchange).renew

    result = await pusher.drain()

    assert result.stopped_early
    assert result.error == SIGN_IN_NEEDED
    assert result.quarantined == 0
    assert outbox.backlog() == 1
    assert outbox.failures() == []
    assert cloud.calls == 1, "a credential that cannot be renewed is not retried"


@pytest.mark.asyncio
async def test_only_one_refresh_per_drain(
    till: Any, db: Database, outbox: OutboxRepository, in_memory_keychain: dict[str, str]
) -> None:
    """A second 401 after a successful refresh is something else — an RLS
    denial, most likely — and gets the ordinary transient treatment rather
    than another refresh. Otherwise a genuine denial would spin the refresh
    endpoint every ninety seconds."""
    sell(till)
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    cloud = BearerAware()
    cloud.fail_times = 2
    cloud.fail_with = httpx.Response(401, json={"message": "permission denied"})
    holder, exchange = Holder("access-0"), Exchange()
    pusher = pusher_for(db, outbox, cloud, holder)
    pusher.renew = refresher(holder, exchange).renew

    result = await pusher.drain()

    assert result.stopped_early
    assert result.error != SIGN_IN_NEEDED
    assert exchange.calls == ["refresh-0"]
    assert cloud.calls == 2
    assert outbox.backlog() == 1


@pytest.mark.asyncio
async def test_without_a_refresher_a_401_is_still_transient(
    till: Any, db: Database, outbox: OutboxRepository
) -> None:
    """The pre-existing behaviour, kept: a pusher with nothing to refresh
    with does what it always did."""
    sell(till)
    cloud = BearerAware()
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(401, json={"message": "JWT expired"})
    pusher = pusher_for(db, outbox, cloud, Holder("access-0"))
    assert pusher.renew is None

    result = await pusher.drain()

    assert result.stopped_early
    assert outbox.backlog() == 1


# ── The engine's proactive path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_token_in_its_last_minutes_is_renewed_before_the_push(
    till: Any, db: Database, outbox: OutboxRepository, in_memory_keychain: dict[str, str]
) -> None:
    sell(till)
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    cloud = BearerAware()
    holder = Holder(token_expiring_in(timedelta(minutes=3)))
    exchange = Exchange()
    engine = an_engine(db, outbox, cloud)
    engine.pusher.token_provider = holder.current
    engine.tokens = refresher(holder, exchange)

    result = await engine.cycle()

    assert result.pushed == 1
    assert exchange.calls == ["refresh-0"]
    assert cloud.bearers == ["Bearer access-1"], "the push never used the dying token"
    assert engine.snapshot().needs_signin is False


@pytest.mark.asyncio
async def test_a_young_token_is_left_alone(
    till: Any, db: Database, outbox: OutboxRepository, in_memory_keychain: dict[str, str]
) -> None:
    sell(till)
    keychain.save_refresh_token(STORE, TERMINAL, "refresh-0")
    cloud = BearerAware()
    young = token_expiring_in(timedelta(minutes=50))
    holder, exchange = Holder(young), Exchange()
    engine = an_engine(db, outbox, cloud)
    engine.pusher.token_provider = holder.current
    engine.tokens = refresher(holder, exchange)

    await engine.cycle()

    assert exchange.calls == []
    assert cloud.bearers == [f"Bearer {young}"]


@pytest.mark.asyncio
async def test_a_dead_session_is_reported_and_cleared_by_the_next_good_push(
    till: Any, db: Database, outbox: OutboxRepository, in_memory_keychain: dict[str, str]
) -> None:
    sell(till)
    cloud = BearerAware()
    cloud.fail_times = 1
    cloud.fail_with = httpx.Response(401, json={"message": "JWT expired"})
    holder = Holder(token_expiring_in(timedelta(minutes=3)))
    engine = an_engine(db, outbox, cloud)
    engine.pusher.token_provider = holder.current
    # No refresh token in the keychain at all: proactive path says dead.
    engine.tokens = refresher(holder, Exchange())
    engine.pusher.renew = engine.tokens.renew

    await engine.cycle()
    status = engine.snapshot()
    assert status.needs_signin is True
    assert status.last_error == SIGN_IN_NEEDED
    assert outbox.backlog() == 1

    # Somebody signs in: the store hands the engine a live token and the
    # next push succeeds, so the badge stops asking.
    holder.replace(token_expiring_in(timedelta(minutes=55)))
    await engine.cycle()
    assert engine.snapshot().needs_signin is False
    assert outbox.backlog() == 0


def test_status_response_carries_needs_signin(
    db: Database, outbox: OutboxRepository, cloud: FakeCloud
) -> None:
    engine = an_engine(db, outbox, cloud)
    engine.status.needs_signin = True
    assert engine.snapshot().as_dict()["needs_signin"] is True


# ── Positive control: production wires it ───────────────────────────────────


def test_build_app_wires_the_refresher_into_the_engine(
    db: Database, tmp_path: Any
) -> None:
    """Every test above hands the engine a refresher by hand. This one asks
    whether `build_app` does, because a refresher that exists and is never
    attached is exactly the bug this card describes, one layer up."""
    settings = Settings(
        data_dir=tmp_path,
        store_code=STORE,
        terminal_code=TERMINAL,
        terminal_id=TERMINAL_ID,
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon-key",
        **FAST_ARGON2,
    )
    app = build_app(
        token=TEST_TOKEN, settings=settings, db=db, run_migrations=False, mac_key=TEST_MAC_KEY
    )
    engine = app.state.sync
    assert engine is not None
    assert isinstance(engine.tokens, TokenRefresher)
    assert engine.pusher.renew == engine.tokens.renew
    assert engine.tokens.store_code == STORE and engine.tokens.terminal_code == TERMINAL
    # And the seam it reads through is the live session store: sign somebody
    # in and the refresher sees their token without being told.
    assert engine.tokens.current_token() is None, "signed out: nothing to refresh"
    with TestClient(app, base_url="http://127.0.0.1"):
        pass


def _expiry_aware_puller(db: Database, holder: Holder) -> Puller:
    """A puller whose cloud refuses an expired JWT, because a real one does.

    `an_engine`'s puller answers 200 to any token, which is fine for the
    tests it was written for and makes this one unrepresentable: the whole
    question is what happens when the *pull* is refused. A fake that accepts
    a dead credential cannot be used to test a dead credential — the same
    trap as the PostgREST fake that ignored `select`.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        bearer = request.headers.get("Authorization", "").removeprefix("Bearer ")
        exp = expires_at(bearer)
        if exp is None or exp <= datetime.now(timezone.utc):
            return httpx.Response(401, json={"message": "JWT expired"})
        return httpx.Response(200, json=[])

    return Puller(
        db,
        base_url="https://example.supabase.co",
        anon_key="anon",
        token_provider=holder.current,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handle)),
    )


@pytest.mark.asyncio
async def test_signing_in_again_clears_the_badge_with_nothing_to_push(
    till: Any, db: Database, outbox: OutboxRepository, in_memory_keychain: dict[str, str]
) -> None:
    """A quiet morning, which is when this actually happens.

    The test above clears the badge with a pending sale. Take the sale away
    and the only signal left was a successful push that never came: a till
    signed in again at 8am went on saying "Sign in needed" until somebody
    sold something. The screen's one instruction was the one thing that would
    not help, and the obvious reaction — sign in again — does nothing.

    A completed pull is the proof, not the clock. The puller calls
    `raise_for_status`, so a pull that returned is the cloud accepting this
    token. A token inside its hour can still be refused, which is why "not
    near expiry" is not evidence of anything.
    """
    cloud = BearerAware()
    holder = Holder(token_expiring_in(timedelta(minutes=-1)))  # already dead
    engine = an_engine(db, outbox, cloud)
    engine.pusher.token_provider = holder.current
    engine.puller = _expiry_aware_puller(db, holder)
    engine.tokens = refresher(holder, Exchange())
    engine.pusher.renew = engine.tokens.renew

    # Nothing in the outbox, and the session is past saving: no refresh token
    # in the keychain, so the proactive path says dead.
    await engine.cycle()
    assert outbox.backlog() == 0, "the premise: there is nothing to push"
    assert engine.snapshot().needs_signin is True

    # Somebody signs in. No sale is made.
    holder.replace(token_expiring_in(timedelta(minutes=55)))
    await engine.cycle()

    assert engine.snapshot().needs_signin is False
