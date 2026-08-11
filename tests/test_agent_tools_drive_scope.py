"""
Tests for Drive search scope disclosure in the chat tool surface.

Regression context: same bug class as tests/test_agent_tools_scope_widening.py.
search_drive asked Drive for only the five most recently modified matches per
account, said nothing about the cut or the ordering, and swallowed a failing
account into the same bare "No drive files found." An old document was therefore
systematically unreachable whenever a handful of newer files also matched, and
an expired token was indistinguishable from an empty Drive — so chat denied the
existence of a document that was present and indexed.

These tests pin the fix: a cap that matches the service default and discloses
itself when it binds, an explicit modification-time ordering (Drive has no
relevance sort — see _DRIVE_ORDERINGS) so old files stay reachable, per-account
failures named even when other accounts return files, and an empty result that
names the scope it searched instead of implying a fault.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import api.services.agent_tools as at
from api.services.agent_tools import (
    _DRIVE_DEFAULT_RESULTS,
    _DRIVE_MAX_RESULTS,
    _DRIVE_ORDERINGS,
    _positive_int,
    _tool_search_drive,
    TOOL_DEFINITIONS,
    _TOOL_HANDLERS,
)

pytestmark = pytest.mark.unit

# Phrases that would tell the orchestrator the backend broke. An empty result is
# a fact about the data, so none of these may appear in one.
FAULT_WORDS = ("sync issue", "permission", "failed", "error", "unavailable")


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


def _account(value: str = "personal"):
    """Stand-in for a GoogleAccount enum member; only `.value` is read."""
    return SimpleNamespace(value=value)


@pytest.fixture
def accounts(monkeypatch):
    """One configured Google account. Tests may append more in place."""
    configured = [_account("personal")]
    monkeypatch.setattr(at, "get_configured_accounts", lambda: list(configured))
    return configured


def _file(days_ago: int, name: str, account: str = "personal", content: str = ""):
    """A DriveFile stand-in; the tool reads only these attributes."""
    return SimpleNamespace(
        name=name,
        source_account=account,
        mime_type="application/vnd.google-apps.document",
        modified_time=_days_ago(days_ago),
        content=content,
    )


@pytest.fixture
def fake_drive(monkeypatch, accounts):
    """Stub DriveService, recording the kwargs of every search call.

    Applies order_by and max_results the way Drive would (server-side sort, then
    a single page) so both the ordering and the truncation signal are real rather
    than assumed.
    """
    state = SimpleNamespace(files=[], per_account={}, broken=set(), calls=[])

    class FakeDriveService:
        def __init__(self, account):
            self.account = account

        def search(
            self,
            name=None,
            full_text=None,
            mime_type=None,
            folder_id=None,
            max_results=20,
            order_by="modifiedTime desc",
        ):
            state.calls.append({
                "account": self.account.value, "name": name,
                "full_text": full_text, "max_results": max_results,
                "order_by": order_by,
            })
            if self.account.value in state.broken:
                raise RuntimeError("synthetic credentials expired")
            files = list(state.per_account.get(self.account.value, state.files))
            files.sort(
                key=lambda f: f.modified_time,
                reverse=(order_by == "modifiedTime desc"),
            )
            return files[:max_results]

    monkeypatch.setattr("api.services.drive.DriveService", FakeDriveService)
    return state


# ---------------------------------------------------------------------------
# Cap and truncation disclosure
# ---------------------------------------------------------------------------

class TestDriveCapAndTruncation:
    async def test_default_cap_matches_the_service_default(self, fake_drive):
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "synthetic"})
        assert fake_drive.calls[0]["max_results"] == _DRIVE_DEFAULT_RESULTS == 20

    async def test_explicit_max_results_is_passed_through(self, fake_drive):
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "synthetic", "max_results": 7})
        assert fake_drive.calls[0]["max_results"] == 7

    async def test_truncation_is_disclosed_when_the_cap_is_hit(self, fake_drive):
        fake_drive.files = [_file(i + 1, f"Synthetic doc {i}") for i in range(20)]
        out = await _tool_search_drive({"query": "synthetic"})
        assert "Capped at 20 files per account" in out
        assert "more may exist" in out

    async def test_no_truncation_note_below_the_cap(self, fake_drive):
        fake_drive.files = [_file(1, "Only doc")]
        out = await _tool_search_drive({"query": "only"})
        assert "Capped at" not in out

    async def test_truncation_on_one_of_several_accounts_is_disclosed(
        self, fake_drive, accounts
    ):
        accounts.append(_account("work"))
        fake_drive.per_account = {
            "personal": [_file(1, "Only personal doc")],
            "work": [
                _file(i + 1, f"Work doc {i}", account="work") for i in range(4)
            ],
        }
        out = await _tool_search_drive({"query": "doc", "max_results": 4})
        assert "Capped at 4 files per account" in out

    async def test_a_broken_account_does_not_fake_truncation(
        self, fake_drive, accounts
    ):
        """A raising account returns nothing, not a full page."""
        accounts.append(_account("work"))
        fake_drive.broken = {"work"}
        fake_drive.per_account = {"personal": [_file(1, "Only personal doc")]}
        out = await _tool_search_drive({"query": "doc"})
        assert "Only personal doc" in out
        assert "Capped at" not in out

    async def test_capped_result_says_the_cut_is_by_time_not_relevance(
        self, fake_drive
    ):
        """The cut must not read as "these were the best matches".

        Drive ranks nothing; the dropped files may match better than the kept
        ones, and the note is the only place the model can learn that.
        """
        fake_drive.files = [_file(i + 1, f"Synthetic doc {i}") for i in range(5)]
        out = await _tool_search_drive({"query": "synthetic", "max_results": 5})
        assert "no relevance ranking" in out
        assert "order_by='oldest'" in out

    async def test_capped_result_suggests_the_ordering_not_already_used(
        self, fake_drive
    ):
        """Advising order_by='oldest' to a caller already on 'oldest' is noise."""
        fake_drive.files = [_file(i + 1, f"Synthetic doc {i}") for i in range(5)]
        out = await _tool_search_drive(
            {"query": "synthetic", "max_results": 5, "order_by": "oldest"}
        )
        assert "order_by='recent'" in out
        assert "order_by='oldest'" not in out


# ---------------------------------------------------------------------------
# Ordering — Drive has no relevance sort, so ordering is explicit
# ---------------------------------------------------------------------------

class TestDriveOrdering:
    async def test_default_ordering_is_newest_modified_first(self, fake_drive):
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "synthetic"})
        assert fake_drive.calls[0]["order_by"] == "modifiedTime desc"

    async def test_old_file_is_reachable_behind_newer_matches(self, fake_drive):
        """The reported failure: a comp doc from last year, six newer matches.

        At the old cap of 5 the older file was pushed off the page entirely and
        nothing in the output hinted at it, so chat reported the document absent.
        """
        fake_drive.files = [
            _file(i + 1, f"Synthetic planning note {i}") for i in range(6)
        ] + [_file(420, "Synthetic comp planning 2025")]
        out = await _tool_search_drive({"query": "comp planning"})
        assert "Synthetic comp planning 2025" in out
        assert "Capped at" not in out

    async def test_oldest_ordering_reaches_an_old_file_the_cap_would_hide(
        self, fake_drive
    ):
        """With the cap binding, ordering is the only way to reach the old file."""
        fake_drive.files = [
            _file(i + 1, f"Synthetic planning note {i}") for i in range(6)
        ] + [_file(420, "Synthetic comp planning 2025")]
        recent = await _tool_search_drive({"query": "comp", "max_results": 3})
        assert "Synthetic comp planning 2025" not in recent

        oldest = await _tool_search_drive(
            {"query": "comp", "max_results": 3, "order_by": "oldest"}
        )
        assert "Synthetic comp planning 2025" in oldest
        assert fake_drive.calls[-1]["order_by"] == "modifiedTime"

    async def test_ordering_note_names_the_direction_actually_used(self, fake_drive):
        fake_drive.files = [_file(i + 1, f"Synthetic doc {i}") for i in range(3)]
        out = await _tool_search_drive(
            {"query": "synthetic", "max_results": 3, "order_by": "oldest"}
        )
        assert "oldest-modified first" in out
        assert "newest-modified first" not in out

    async def test_unknown_ordering_falls_back_and_is_disclosed(self, fake_drive):
        """A made-up sort key must not be sent to Drive, nor silently ignored."""
        fake_drive.files = [_file(1, "Synthetic doc")]
        out = await _tool_search_drive(
            {"query": "synthetic", "order_by": "relevance"}
        )
        assert fake_drive.calls[0]["order_by"] == "modifiedTime desc"
        assert "Ignored order_by='relevance'" in out
        assert "newest-modified first" in out

    async def test_unknown_ordering_is_disclosed_on_an_empty_result_too(
        self, fake_drive
    ):
        out = await _tool_search_drive({"query": "nothing", "order_by": "relevance"})
        assert "Ignored order_by='relevance'" in out

    async def test_a_valid_ordering_is_not_reported_as_ignored(self, fake_drive):
        fake_drive.files = [_file(1, "Synthetic doc")]
        out = await _tool_search_drive({"query": "synthetic", "order_by": "recent"})
        assert "Ignored order_by" not in out

    @pytest.mark.parametrize("raw", [[], ["recent"], {}, {"order": "recent"}, 3, 1.5, True])
    async def test_wrong_typed_ordering_is_disclosed_not_raised(self, fake_drive, raw):
        """The model writes this argument, so a list or dict arrives.

        `raw_order in _DRIVE_ORDERINGS` raised TypeError: unhashable type on those
        — a malformed argument taken down as a tool crash instead of being
        reported. It is now handled exactly like an unknown sort key.
        """
        fake_drive.files = [_file(1, "Synthetic doc")]
        out = await _tool_search_drive({"query": "synthetic", "order_by": raw})
        assert f"Ignored order_by={raw!r}" in out
        assert fake_drive.calls[0]["order_by"] == "modifiedTime desc"
        assert "Synthetic doc" in out

    async def test_wrong_typed_ordering_is_disclosed_on_an_empty_result_too(
        self, fake_drive
    ):
        out = await _tool_search_drive({"query": "nothing", "order_by": []})
        assert "Ignored order_by=[]" in out
        assert not any(word in out.lower() for word in FAULT_WORDS)

    async def test_merged_accounts_are_sorted_not_concatenated(
        self, fake_drive, accounts
    ):
        """Two pages joined end to end would contradict the ordering claimed."""
        accounts.append(_account("work"))
        fake_drive.per_account = {
            "personal": [_file(30, "Synthetic older personal doc")],
            "work": [_file(1, "Synthetic newer work doc", account="work")],
        }
        out = await _tool_search_drive({"query": "synthetic"})
        assert out.index("Synthetic newer work doc") < out.index(
            "Synthetic older personal doc"
        )

    async def test_oldest_ordering_reverses_the_merged_order(
        self, fake_drive, accounts
    ):
        accounts.append(_account("work"))
        fake_drive.per_account = {
            "personal": [_file(30, "Synthetic older personal doc")],
            "work": [_file(1, "Synthetic newer work doc", account="work")],
        }
        out = await _tool_search_drive({"query": "synthetic", "order_by": "oldest"})
        assert out.index("Synthetic older personal doc") < out.index(
            "Synthetic newer work doc"
        )

    async def test_mixed_timestamp_shapes_do_not_break_the_merge(self, fake_drive):
        """The merge sort runs outside the per-account handler.

        A naive or missing modified_time from one account would take the whole
        search down with a TypeError rather than costing one file.
        """
        undated = _file(5, "Synthetic undated doc")
        undated.modified_time = None
        naive = _file(2, "Synthetic naive doc")
        naive.modified_time = datetime.now() - timedelta(days=2)
        fake_drive.per_account = {"personal": [naive, undated, _file(1, "Aware doc")]}
        # The fake's own sort needs comparable keys; bypass it with a page big
        # enough that ordering there is irrelevant to what the tool receives.
        fake_drive.per_account["personal"].sort(key=lambda f: f.name)

        class NoSortDrive:
            def __init__(self, account):
                self.account = account

            def search(self, **kw):
                return list(fake_drive.per_account["personal"])

        with patch("api.services.drive.DriveService", NoSortDrive):
            out = await _tool_search_drive({"query": "synthetic"})
        assert "Synthetic undated doc" in out
        assert "Aware doc" in out

    async def test_modified_date_is_shown_so_the_ordering_is_checkable(
        self, fake_drive
    ):
        stamp = _days_ago(3)
        fake_drive.files = [_file(3, "Synthetic doc")]
        out = await _tool_search_drive({"query": "synthetic"})
        assert f"modified {stamp.strftime('%Y-%m-%d')}" in out


# ---------------------------------------------------------------------------
# Per-account failures — a fault is not an absence
# ---------------------------------------------------------------------------

class TestDriveAccountFailureDisclosure:
    """A failing account must not be rendered as an empty Drive.

    An expired token was logged and the tool still said "No drive files found."
    — a genuine backend fault dressed as absence, which is the misdiagnosis this
    change exists to prevent.
    """

    async def test_total_failure_is_disclosed_not_reported_as_empty(
        self, fake_drive, accounts
    ):
        fake_drive.broken = {"personal"}
        out = await _tool_search_drive({"query": "comp planning"})
        assert "personal" in out
        assert "NOT an empty Drive" in out

    async def test_total_failure_leads_with_the_fault(self, fake_drive, accounts):
        """A denial with a footnote still reads as an answer."""
        fake_drive.broken = {"personal"}
        out = await _tool_search_drive({"query": "comp planning"})
        assert out.startswith("Could not search Drive")

    async def test_total_failure_does_not_claim_an_absence(self, fake_drive, accounts):
        """Nothing responded, so "the accounts that responded" was none of them."""
        fake_drive.broken = {"personal"}
        out = await _tool_search_drive({"query": "comp planning"})
        assert "No Drive files" not in out
        assert "accounts that responded" not in out

    async def test_every_account_failing_is_a_total_failure(
        self, fake_drive, accounts
    ):
        """Two accounts, both down — still nothing searched."""
        accounts.append(_account("work"))
        fake_drive.broken = {"personal", "work"}
        out = await _tool_search_drive({"query": "comp planning"})
        assert out.startswith("Could not search Drive")
        assert "personal" in out and "work" in out

    async def test_total_failure_still_discloses_a_bad_ordering(
        self, fake_drive, accounts
    ):
        """A bad argument does not stop being wrong because an account broke."""
        fake_drive.broken = {"personal"}
        out = await _tool_search_drive(
            {"query": "comp planning", "order_by": "relevance"}
        )
        assert "Ignored order_by='relevance'" in out

    async def test_partial_failure_still_discloses_alongside_results(
        self, fake_drive, accounts
    ):
        """The dangerous case: real files returned, one account silently missing."""
        accounts.append(_account("work"))
        fake_drive.broken = {"work"}
        fake_drive.per_account = {"personal": [_file(1, "Synthetic personal doc")]}
        out = await _tool_search_drive({"query": "synthetic"})
        assert "Synthetic personal doc" in out
        assert "Could not reach work" in out

    async def test_healthy_accounts_produce_no_failure_note(
        self, fake_drive, accounts
    ):
        accounts.append(_account("work"))
        fake_drive.per_account = {
            "personal": [_file(1, "Synthetic personal doc")],
            "work": [_file(2, "Synthetic work doc", account="work")],
        }
        out = await _tool_search_drive({"query": "synthetic"})
        assert "Could not reach" not in out

    async def test_healthy_accounts_produce_no_failure_note_when_empty(
        self, fake_drive, accounts
    ):
        accounts.append(_account("work"))
        out = await _tool_search_drive({"query": "nothing matches this"})
        assert "Could not reach" not in out

    async def test_only_the_failing_account_is_named(self, fake_drive, accounts):
        accounts.append(_account("work"))
        fake_drive.broken = {"work"}
        out = await _tool_search_drive({"query": "synthetic"})
        reach = out.split("Could not reach", 1)[1]
        assert "work" in reach
        assert "personal" not in reach

    async def test_a_real_fault_is_named_rather_than_hidden(
        self, fake_drive, accounts
    ):
        """The complement of the no-fault-words rule.

        An honest empty must never imply a fault — but a genuine failure must
        not be scrubbed into one either, or we are back to reporting absence for
        a Drive we could not read.
        """
        fake_drive.broken = {"personal"}
        out = await _tool_search_drive({"query": "synthetic"})
        assert "errored" in out
        assert any(word in out.lower() for word in FAULT_WORDS)

    async def test_partial_failure_still_calls_the_answer_incomplete(
        self, fake_drive, accounts
    ):
        """The partial branch keeps its own wording: results, minus one account."""
        accounts.append(_account("work"))
        fake_drive.broken = {"work"}
        fake_drive.per_account = {"personal": [_file(1, "Synthetic personal doc")]}
        out = await _tool_search_drive({"query": "synthetic"})
        assert "incomplete" in out
        assert "not necessarily an empty Drive" in out

    async def test_empty_after_a_failure_is_a_distinct_branch(
        self, fake_drive, accounts
    ):
        """"Nothing found" and "nothing found in what answered" differ."""
        accounts.append(_account("work"))
        fake_drive.broken = {"work"}
        partial = await _tool_search_drive({"query": "synthetic"})
        fake_drive.broken = set()
        healthy = await _tool_search_drive({"query": "synthetic"})
        assert partial != healthy
        assert "accounts that responded" in partial
        assert "accounts that responded" not in healthy


# ---------------------------------------------------------------------------
# Honest empty results
# ---------------------------------------------------------------------------

class TestDriveHonestEmpty:
    async def test_empty_result_names_the_query(self, fake_drive):
        out = await _tool_search_drive({"query": "comp planning"})
        assert "'comp planning'" in out

    async def test_empty_result_names_the_cap_and_the_ordering(self, fake_drive):
        out = await _tool_search_drive({"query": "comp planning"})
        assert "up to 20 files per account" in out
        assert "newest-modified first" in out

    async def test_empty_result_names_the_explicit_cap_actually_used(
        self, fake_drive
    ):
        out = await _tool_search_drive({"query": "comp planning", "max_results": 3})
        assert "up to 3 files per account" in out

    @pytest.mark.parametrize(
        "inp",
        [
            {"query": "comp planning"},
            {"query": "comp planning", "max_results": 0},
            {"query": "comp planning", "order_by": "relevance"},
            {"query": "comp planning", "order_by": []},
            {},
            {"query": "   "},
        ],
    )
    async def test_no_empty_result_suggests_a_backend_fault(self, fake_drive, inp):
        """Blanket ban, no exceptions on the paths where nothing actually broke.

        The one branch deliberately excluded is the raising-account branch, which
        must name the fault — see test_a_real_fault_is_named_rather_than_hidden.
        """
        out = await _tool_search_drive(inp)
        assert not any(word in out.lower() for word in FAULT_WORDS)

    async def test_missing_query_is_not_blamed_on_the_accounts(self, fake_drive):
        """query was read inside the per-account try.

        A missing query raised KeyError once per account and came back as
        "Could not reach personal, work" — a malformed argument reported as
        expired credentials.
        """
        out = await _tool_search_drive({})
        assert "Could not reach" not in out
        assert "query" in out
        assert fake_drive.calls == []

    @pytest.mark.parametrize("raw", [None, "", "   ", 0])
    async def test_blank_query_never_reaches_drive(self, fake_drive, raw):
        await _tool_search_drive({"query": raw})
        assert fake_drive.calls == []

    async def test_query_is_trimmed_before_the_service_call(self, fake_drive):
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "  comp planning  "})
        assert fake_drive.calls[0]["full_text"] == "comp planning"


# ---------------------------------------------------------------------------
# Argument hardening — the model fills these in, so garbled values arrive
# ---------------------------------------------------------------------------

class TestDriveCapNormalization:
    """max_results doubles as the truncation yardstick.

    A None or 0 from the model would both confuse Drive's pageSize and silently
    disable the disclosure that the page was cut short.
    """

    @pytest.mark.parametrize("raw", [None, "not a number"])
    async def test_uninterpretable_cap_falls_back_to_the_default(
        self, fake_drive, raw
    ):
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "synthetic", "max_results": raw})
        assert fake_drive.calls[0]["max_results"] == _DRIVE_DEFAULT_RESULTS

    @pytest.mark.parametrize("raw", [0, -5])
    async def test_non_positive_cap_is_raised_to_one(self, fake_drive, raw):
        """A cap is clamped, not dropped — unlike a scope.

        A cap of 1 still returns data and the truncation check still works, which
        is the same treatment search_email and search_slack give theirs.
        """
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "synthetic", "max_results": raw})
        assert fake_drive.calls[0]["max_results"] == 1

    async def test_numeric_string_is_coerced(self, fake_drive):
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "synthetic", "max_results": "6"})
        assert fake_drive.calls[0]["max_results"] == 6

    async def test_absurd_cap_is_clamped(self, fake_drive):
        fake_drive.files = [_file(1, "Synthetic doc")]
        await _tool_search_drive({"query": "synthetic", "max_results": 10_000})
        assert fake_drive.calls[0]["max_results"] == _DRIVE_MAX_RESULTS

    async def test_every_cap_the_model_can_send_is_a_positive_int(self, fake_drive):
        for raw in (None, 0, -1, "0", 3.9, 10_000, "nonsense"):
            fake_drive.calls.clear()
            fake_drive.files = [_file(1, "Synthetic doc")]
            await _tool_search_drive({"query": "synthetic", "max_results": raw})
            used = fake_drive.calls[0]["max_results"]
            assert isinstance(used, int) and used > 0

    async def test_normalised_cap_is_the_yardstick_the_note_reports(
        self, fake_drive
    ):
        """The cap sent and the cap compared against must be the same number.

        If the clamped value reached Drive but the raw one drove the check, a cut
        page would come back looking complete.
        """
        fake_drive.files = [_file(i + 1, f"Synthetic doc {i}") for i in range(25)]
        out = await _tool_search_drive({"query": "synthetic", "max_results": 10_000})
        sent = fake_drive.calls[0]["max_results"]
        assert sent == _DRIVE_MAX_RESULTS
        # 25 files, page of 100: the cap did not bind, so nothing to disclose.
        assert "Capped at" not in out

        fake_drive.calls.clear()
        out = await _tool_search_drive({"query": "synthetic", "max_results": 0})
        assert fake_drive.calls[0]["max_results"] == 1
        assert "Capped at 1 files per account" in out

    async def test_zero_cap_still_searches_rather_than_faking_an_empty(
        self, fake_drive
    ):
        fake_drive.files = [_file(1, "Synthetic doc")]
        out = await _tool_search_drive({"query": "synthetic", "max_results": 0})
        assert "Synthetic doc" in out

    def test_cap_bounds_are_sane(self):
        assert _positive_int(None, _DRIVE_DEFAULT_RESULTS, _DRIVE_MAX_RESULTS) == 20
        assert _DRIVE_DEFAULT_RESULTS <= _DRIVE_MAX_RESULTS


# ---------------------------------------------------------------------------
# Tool schema — the model can only use parameters that are advertised
# ---------------------------------------------------------------------------

class TestDriveToolDefinition:
    @staticmethod
    def _schema() -> dict:
        return next(
            t for t in TOOL_DEFINITIONS if t["name"] == "search_drive"
        )["input_schema"]

    def test_tool_is_registered(self):
        assert "search_drive" in _TOOL_HANDLERS
        assert any(t["name"] == "search_drive" for t in TOOL_DEFINITIONS)

    def test_documents_the_new_default_cap(self):
        desc = self._schema()["properties"]["max_results"]["description"]
        assert "20" in desc

    def test_ordering_option_is_advertised(self):
        prop = self._schema()["properties"]["order_by"]
        assert set(prop["enum"]) == set(_DRIVE_ORDERINGS)

    def test_ordering_description_explains_when_to_use_oldest(self):
        desc = self._schema()["properties"]["order_by"]["description"].lower()
        assert "oldest" in desc
        assert "cap" in desc

    def test_description_warns_there_is_no_relevance_ranking(self):
        desc = next(
            t for t in TOOL_DEFINITIONS if t["name"] == "search_drive"
        )["description"].lower()
        assert "no relevance ranking" in desc


# ---------------------------------------------------------------------------
# Service boundary — the disclosure above depends on faults reaching the tool
# ---------------------------------------------------------------------------

class TestDriveServiceContract:
    """DriveService.search() swallows API errors into an empty list.

    That is fine for a bad query, but the credential fetch used to sit inside the
    same handler, so an expired token returned [] and the tool could not tell an
    unreachable account from one with no matching files. These pin the boundary
    the per-account disclosure relies on.
    """

    def test_auth_failure_propagates_instead_of_returning_empty(self):
        from api.services.drive import DriveService, GoogleAccount

        with patch(
            "api.services.drive.get_google_auth",
            side_effect=RuntimeError("synthetic token expired"),
        ):
            service = DriveService(account_type=GoogleAccount.PERSONAL)
            with pytest.raises(RuntimeError, match="synthetic token expired"):
                service.search(full_text="comp planning")

    def test_order_by_reaches_the_drive_api(self):
        from api.services.drive import DriveService, GoogleAccount

        service = DriveService.__new__(DriveService)
        service.account_type = GoogleAccount.PERSONAL
        service._service = MagicMock()
        service._service.files().list().execute.return_value = {"files": []}

        service.search(full_text="comp planning", order_by="modifiedTime")

        kwargs = service._service.files().list.call_args.kwargs
        assert kwargs["orderBy"] == "modifiedTime"

    def test_default_order_by_is_unchanged_for_existing_callers(self):
        from api.services.drive import DriveService, GoogleAccount

        service = DriveService.__new__(DriveService)
        service.account_type = GoogleAccount.PERSONAL
        service._service = MagicMock()
        service._service.files().list().execute.return_value = {"files": []}

        service.search(name="Synthetic doc")

        kwargs = service._service.files().list.call_args.kwargs
        assert kwargs["orderBy"] == "modifiedTime desc"
