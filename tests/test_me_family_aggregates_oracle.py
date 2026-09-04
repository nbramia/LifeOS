"""
Oracle tests for #871: the Me/Family dashboard handlers were rewritten to
aggregate interactions in SQL instead of hydrating every interaction in the
window into a Python object and looping over them.

Each oracle function below is the pre-#871 algorithm, copied verbatim from
`api/routes/crm.py` (only variable sourcing was changed: stores are passed in
as parameters instead of being fetched at module scope), operating on the
SAME real store methods the new handlers still use for fetching
(`get_all_in_range`, `get_all`, `get_first_interaction_dates` are unchanged
by #871). Running both the oracle and the actual endpoint against the same
synthetic SQLite-backed stores and comparing their output is the strongest
available guarantee that the SQL rewrite preserves exact behavior.

One deliberate exception: `oracle_me_interactions`'s "Build messaging list"
block renames its per-month `by_circle` local to `by_circle_m`. The original
code reused the bare name `by_circle` there, silently shadowing the
response-level `by_circle` dict computed earlier in the same function — so
production's `/me/interactions` `by_circle` field (and the Me page's "By
Dunbar Circle" chart, which renders it) was actually showing whatever the
LAST processed messaging month's iMessage/WhatsApp-only breakdown happened to
be, not the true full-window, all-sources total the field is documented to
be. This oracle intentionally reproduces the CORRECT (fixed) behavior — see
the matching fix and comment in `get_me_interactions()` — so this test
verifies the fix rather than pinning the bug.

List-valued fields that the real code explicitly sorts by count (top_contacts,
warming, cooling) are compared as sets of dicts rather than ordered lists:
ties in count are broken by iteration order over a dict, which isn't
guaranteed identical between hydrating interactions one-by-one (old) and a
SQL GROUP BY (new) — so the synthetic dataset below gives every compared
person a distinct count, and the set comparison is a belt-and-suspenders
safety net on top of that.
"""
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import pytest

from api.services.interaction_store import Interaction, InteractionStore
from api.services.person_entity import PersonEntity, PersonEntityStore
from api.routes.crm import (
    get_me_interactions,
    get_me_stats,
    get_family_interactions,
    get_family_timeline,
    MY_PERSON_ID as REAL_MY_PERSON_ID,  # noqa: F401 (documents where the real constant lives)
)

pytestmark = pytest.mark.unit

MY_PERSON_ID = "oracle-self"


# ============================================================================
# Oracle: pre-#871 /me/interactions algorithm
# ============================================================================

def oracle_me_interactions(
    interaction_store, person_store, my_person_id, days_back=365,
    trend_period="quarter", health_period="quarter",
):
    """Verbatim copy of the pre-#871 get_me_interactions() body."""
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days_back)

    all_people = person_store.get_all()
    person_lookup = {p.id: p for p in all_people}

    hidden_person_ids = {
        p.id for p in person_store.get_all(include_hidden=True) if p.hidden
    }

    peripheral_person_ids = {
        p.id for p in all_people if p.is_peripheral_contact
    }

    all_interactions_raw = interaction_store.get_all_in_range(
        start_date=start_date,
        end_date=end_date,
        exclude_person_ids=[my_person_id] + list(hidden_person_ids) + list(peripheral_person_ids),
    )

    all_interactions = [
        i for i in all_interactions_raw
        if i.person_id in person_lookup
    ]

    circle_map = {
        p.id: (p.dunbar_circle if p.dunbar_circle is not None else 7)
        for p in all_people
    }
    circle_map[my_person_id] = -1

    daily_data = defaultdict(lambda: {"total": 0, "sources": defaultdict(int)})
    by_source = defaultdict(int)
    by_month = defaultdict(int)
    by_circle = defaultdict(int)
    person_counts_30d = defaultdict(int)
    person_counts_recent = defaultdict(int)
    person_counts_previous = defaultdict(int)

    now = datetime.now(timezone.utc)
    period_days = {"week": 7, "month": 30, "quarter": 90, "year": 365}
    trend_days = period_days.get(trend_period, 90)
    trend_recent_start = now - timedelta(days=trend_days)
    trend_previous_start = now - timedelta(days=trend_days * 2)
    thirty_days_ago = now - timedelta(days=30)

    total_count = 0

    for interaction in all_interactions:
        if interaction.timestamp:
            if hasattr(interaction.timestamp, 'strftime'):
                ts = interaction.timestamp
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                date_str = ts.strftime('%Y-%m-%d')
                month_str = ts.strftime('%Y-%m')
            else:
                date_str = str(interaction.timestamp)[:10]
                month_str = date_str[:7]
                ts = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        else:
            continue

        source = (interaction.source_type or "unknown").lower()
        person_id = interaction.person_id
        circle = circle_map.get(person_id, 7)

        if source == 'gmail':
            title = interaction.title or ""
            is_sent = title.startswith("→")
            if not is_sent:
                continue

        daily_data[date_str]["total"] += 1
        daily_data[date_str]["sources"][source] += 1

        by_source[source] += 1
        by_month[month_str] += 1
        by_circle[str(circle)] += 1

        if ts >= thirty_days_ago:
            person_counts_30d[person_id] += 1

        if ts >= trend_recent_start:
            person_counts_recent[person_id] += 1
        elif ts >= trend_previous_start:
            person_counts_previous[person_id] += 1

        total_count += 1

    daily_list = [
        {"date": date, "total": data["total"], "sources": dict(data["sources"])}
        for date, data in sorted(daily_data.items())
    ]

    top_contacts = []
    for person_id, count in sorted(person_counts_30d.items(), key=lambda x: -x[1])[:10]:
        person = person_lookup.get(person_id)
        top_contacts.append({
            "person_id": person_id,
            "person_name": person.canonical_name if person else "Unknown",
            "count": count,
        })

    warming = []
    cooling = []
    all_person_ids = set(person_counts_recent.keys()) | set(person_counts_previous.keys())
    for person_id in all_person_ids:
        recent = person_counts_recent.get(person_id, 0)
        prev = person_counts_previous.get(person_id, 0)
        if recent == prev:
            continue
        person = person_lookup.get(person_id)
        trend_person = {
            "person_id": person_id,
            "person_name": person.canonical_name if person else "Unknown",
            "recent_count": recent,
            "previous_count": prev,
        }
        if recent > prev:
            warming.append(trend_person)
        else:
            cooling.append(trend_person)

    warming.sort(key=lambda x: x["recent_count"] - x["previous_count"], reverse=True)
    cooling.sort(key=lambda x: x["previous_count"] - x["recent_count"], reverse=True)

    HEALTH_INTERACTION_TYPES = {'imessage', 'whatsapp', 'phone', 'phone_call', 'calendar', 'gmail'}

    personal_family_people = [
        p for p in person_lookup.values()
        if p.category in ('family', 'personal') and p.id != my_person_id
    ]
    personal_family_people.sort(key=lambda p: p.relationship_strength or 0, reverse=True)
    top_25_ids = {p.id for p in personal_family_people[:25]}

    personal_interactions = [
        i for i in all_interactions
        if (i.source_type or "").lower() in HEALTH_INTERACTION_TYPES
        and i.person_id in top_25_ids
    ]

    neglected = []
    person_interaction_dates = defaultdict(list)
    for interaction in all_interactions:
        if interaction.timestamp:
            ts = interaction.timestamp
            if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            elif not hasattr(ts, 'tzinfo'):
                ts = datetime.fromisoformat(str(ts)[:10]).replace(tzinfo=timezone.utc)
            person_interaction_dates[interaction.person_id].append(ts)

    for person_id, dates in person_interaction_dates.items():
        person = person_lookup.get(person_id)
        if not person:
            continue
        circle = circle_map.get(person_id, 7)
        if circle > 3:
            continue
        if len(dates) < 5:
            continue
        dates_sorted = sorted(dates)
        gaps = [(dates_sorted[i + 1] - dates_sorted[i]).days for i in range(len(dates_sorted) - 1)]
        if not gaps:
            continue
        gaps_sorted = sorted(gaps)
        typical_gap = gaps_sorted[len(gaps_sorted) // 2]
        min_gap = {0: 3, 1: 5, 2: 7, 3: 14}.get(circle, 14)
        if typical_gap < min_gap:
            typical_gap = min_gap
        last_contact = dates_sorted[-1]
        days_since = (now - last_contact).days
        if days_since > typical_gap * 1.5:
            neglected.append({
                "person_id": person_id,
                "person_name": person.canonical_name,
                "days_since_contact": days_since,
                "typical_gap_days": typical_gap,
                "dunbar_circle": circle,
            })
    neglected.sort(key=lambda x: (x["dunbar_circle"], -(x["days_since_contact"] / x["typical_gap_days"])))

    health_score_history = []
    if health_period == "month":
        total_days = 61
        num_points = 9
    elif health_period == "year":
        total_days = 730
        num_points = 13
    else:
        total_days = 183
        num_points = 13

    time_points = [now - timedelta(days=int(i * total_days / (num_points - 1))) for i in range(num_points)]
    time_points = sorted(time_points)

    health_raw_counts = []
    for i, point_date in enumerate(time_points):
        if i == 0:
            interval = (time_points[1] - time_points[0]).days if len(time_points) > 1 else 14
            prev_date = point_date - timedelta(days=interval)
        else:
            prev_date = time_points[i - 1]

        period_count = 0
        for interaction in personal_interactions:
            if interaction.timestamp:
                ts = interaction.timestamp
                if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                elif not hasattr(ts, 'tzinfo'):
                    ts = datetime.fromisoformat(str(ts)[:10]).replace(tzinfo=timezone.utc)
                if prev_date < ts <= point_date:
                    period_count += 1

        health_raw_counts.append(period_count)

    health_avg = sum(health_raw_counts) / len(health_raw_counts) if health_raw_counts else 0

    for i, point_date in enumerate(time_points):
        count = health_raw_counts[i]
        if health_avg > 0:
            score = int(min(100, (count / health_avg) * 50))
        else:
            score = 50 if count > 0 else 0
        health_score_history.append({
            "date": point_date.strftime('%Y-%m-%d'), "score": score, "count": count,
        })

    health_score = health_score_history[-1]["score"] if health_score_history else 0

    first_interaction_dates = interaction_store.get_first_interaction_dates(min_interactions=3)
    monthly_new_people = defaultdict(int)
    for person_id, first_dt in first_interaction_dates.items():
        if person_id == my_person_id:
            continue
        if person_id in hidden_person_ids:
            continue
        if person_id in peripheral_person_ids:
            continue
        if person_id not in person_lookup:
            continue
        month_key = first_dt.strftime('%Y-%m')
        monthly_new_people[month_key] += 1

    network_growth = []
    cumulative = 0
    all_months = sorted(monthly_new_people.keys())
    chart_months_cutoff = (now - timedelta(days=days_back)).strftime('%Y-%m')
    for month in all_months:
        cumulative += monthly_new_people[month]
        if month >= chart_months_cutoff:
            network_growth.append({
                "month": month, "new_people": monthly_new_people[month], "cumulative_total": cumulative,
            })

    monthly_messaging = defaultdict(lambda: {
        "total": 0, "by_circle": defaultdict(int), "people_by_circle": defaultdict(set),
    })
    for interaction in all_interactions:
        source = (interaction.source_type or "").lower()
        if source not in ('imessage', 'whatsapp'):
            continue
        if interaction.timestamp:
            ts = interaction.timestamp
            if hasattr(ts, 'strftime'):
                month_key = ts.strftime('%Y-%m')
            else:
                month_key = str(ts)[:7]
        else:
            continue
        person_id = interaction.person_id
        circle = circle_map.get(person_id, 7)
        if circle > 4:
            circle = 5
        monthly_messaging[month_key]["total"] += 1
        monthly_messaging[month_key]["by_circle"][str(circle)] += 1
        monthly_messaging[month_key]["people_by_circle"][str(circle)].add(person_id)

    messaging_by_circle = []
    for month in sorted(monthly_messaging.keys()):
        if month >= chart_months_cutoff:
            data = monthly_messaging[month]
            total = data["total"]
            by_circle_m = dict(data["by_circle"])
            percentages = {c: round(count / total * 100, 1) if total > 0 else 0 for c, count in by_circle_m.items()}
            unique_by_circle = {c: len(people) for c, people in data["people_by_circle"].items()}
            unique_total = len(set().union(*data["people_by_circle"].values())) if data["people_by_circle"] else 0
            messaging_by_circle.append({
                "month": month, "total": total, "by_circle": by_circle_m,
                "circle_percentages": percentages, "unique_by_circle": unique_by_circle,
                "unique_total": unique_total,
            })

    # Tracked relationships: config-driven and not present in a fresh
    # checkout (no config/family_members.json committed) — both the oracle
    # and the real handler resolve this to [] here, so it's exercised for
    # emptiness rather than content.
    tracked_relationships = []

    return {
        "daily": daily_list,
        "by_source": dict(by_source),
        "by_month": dict(by_month),
        "by_circle": dict(by_circle),
        "top_contacts": top_contacts,
        "warming": warming[:10],
        "cooling": cooling[:10],
        "total_count": total_count,
        "relationship_health_score": health_score,
        "health_score_history": health_score_history,
        "health_score_average": round(health_avg, 1),
        "neglected_contacts": neglected[:10],
        "network_growth": network_growth,
        "messaging_by_circle": messaging_by_circle,
        "tracked_relationships": tracked_relationships,
    }


# ============================================================================
# Oracle: pre-#871 /family/interactions algorithm
# ============================================================================

def oracle_family_interactions(
    interaction_store, person_store, my_person_id, selected_ids,
    days_back=365, trend_period="quarter", health_period="quarter",
):
    """Verbatim copy of the pre-#871 get_family_interactions() body."""
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days_back)

    person_lookup = {p.id: p for p in person_store.get_all()}

    selected_names = []
    for pid in selected_ids:
        person = person_lookup.get(pid)
        selected_names.append(person.canonical_name if person else "Unknown")

    all_interactions_raw = interaction_store.get_all_in_range(
        start_date=start_date, end_date=end_date, exclude_person_ids=[my_person_id],
    )
    all_interactions = [i for i in all_interactions_raw if i.person_id in selected_ids]

    daily_data = defaultdict(lambda: {"total": 0, "sources": defaultdict(int)})
    by_source = defaultdict(int)
    by_month = defaultdict(int)
    person_counts_30d = defaultdict(int)
    person_counts_recent = defaultdict(int)
    person_counts_previous = defaultdict(int)

    now = datetime.now(timezone.utc)
    period_days = {"week": 7, "month": 30, "quarter": 90, "year": 365}
    trend_days = period_days.get(trend_period, 90)
    trend_recent_start = now - timedelta(days=trend_days)
    trend_previous_start = now - timedelta(days=trend_days * 2)
    thirty_days_ago = now - timedelta(days=30)

    total_count = 0

    for interaction in all_interactions:
        if interaction.timestamp:
            if hasattr(interaction.timestamp, 'strftime'):
                ts = interaction.timestamp
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                date_str = ts.strftime('%Y-%m-%d')
                month_str = ts.strftime('%Y-%m')
            else:
                date_str = str(interaction.timestamp)[:10]
                month_str = date_str[:7]
                ts = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
        else:
            continue

        source = (interaction.source_type or "unknown").lower()
        person_id = interaction.person_id

        daily_data[date_str]["total"] += 1
        daily_data[date_str]["sources"][source] += 1
        by_source[source] += 1
        by_month[month_str] += 1

        if ts >= thirty_days_ago:
            person_counts_30d[person_id] += 1
        if ts >= trend_recent_start:
            person_counts_recent[person_id] += 1
        elif ts >= trend_previous_start:
            person_counts_previous[person_id] += 1

        total_count += 1

    daily_list = [
        {"date": date, "total": data["total"], "sources": dict(data["sources"])}
        for date, data in sorted(daily_data.items())
    ]

    top_contacts = []
    for person_id, count in sorted(person_counts_30d.items(), key=lambda x: -x[1])[:10]:
        person = person_lookup.get(person_id)
        top_contacts.append({
            "person_id": person_id,
            "person_name": person.canonical_name if person else "Unknown",
            "count": count,
        })

    warming = []
    cooling = []
    for person_id in selected_ids:
        recent = person_counts_recent.get(person_id, 0)
        prev = person_counts_previous.get(person_id, 0)
        if recent == prev:
            continue
        person = person_lookup.get(person_id)
        trend_person = {
            "person_id": person_id,
            "person_name": person.canonical_name if person else "Unknown",
            "recent_count": recent,
            "previous_count": prev,
        }
        if recent > prev:
            warming.append(trend_person)
        else:
            cooling.append(trend_person)

    warming.sort(key=lambda x: x["recent_count"] - x["previous_count"], reverse=True)
    cooling.sort(key=lambda x: x["previous_count"] - x["recent_count"], reverse=True)

    health_score_history = []
    if health_period == "month":
        total_days_history = 61
        num_points = 9
    elif health_period == "year":
        total_days_history = 730
        num_points = 13
    else:
        total_days_history = 183
        num_points = 13

    time_points = [now - timedelta(days=int(i * total_days_history / (num_points - 1))) for i in range(num_points)]
    time_points = sorted(time_points)

    health_raw_counts = []
    for i, point_date in enumerate(time_points):
        if i == 0:
            interval = (time_points[1] - time_points[0]).days if len(time_points) > 1 else 14
            prev_date = point_date - timedelta(days=interval)
        else:
            prev_date = time_points[i - 1]

        period_count = 0
        for interaction in all_interactions:
            if interaction.timestamp:
                ts = interaction.timestamp
                if hasattr(ts, 'tzinfo') and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                elif not hasattr(ts, 'tzinfo'):
                    ts = datetime.fromisoformat(str(ts)[:10]).replace(tzinfo=timezone.utc)
                if prev_date < ts <= point_date:
                    period_count += 1

        health_raw_counts.append(period_count)

    health_avg = sum(health_raw_counts) / len(health_raw_counts) if health_raw_counts else 0

    for i, point_date in enumerate(time_points):
        count = health_raw_counts[i]
        if health_avg > 0:
            score = int(min(100, (count / health_avg) * 50))
        else:
            score = 50 if count > 0 else 0
        health_score_history.append({
            "date": point_date.strftime('%Y-%m-%d'), "score": score, "count": count,
        })

    health_score = health_score_history[-1]["score"] if health_score_history else 0

    return {
        "selected_ids": selected_ids,
        "selected_names": selected_names,
        "daily": daily_list,
        "by_source": dict(by_source),
        "by_month": dict(by_month),
        "top_contacts": top_contacts,
        "warming": warming[:10],
        "cooling": cooling[:10],
        "total_count": total_count,
        "relationship_health_score": health_score,
        "health_score_history": health_score_history,
        "health_score_average": round(health_avg, 1),
    }


# ============================================================================
# Synthetic fixture
# ============================================================================

def _as_set(dict_list):
    """Order-independent comparison helper for lists of flat dicts."""
    return {tuple(sorted(d.items())) for d in dict_list}


@pytest.fixture
def stores(tmp_path):
    interaction_db = str(tmp_path / "interactions.db")
    person_db = str(tmp_path / "crm.db")
    istore = InteractionStore(interaction_db, strict=False)
    pstore = PersonEntityStore(person_db)
    pstore._blocklist.clear()
    return istore, pstore


def _add_interaction(store, person_id, days_ago=0, source_type="imessage",
                      title="Hi", tz_offset_hours=0):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    ts = ts.astimezone(timezone(timedelta(hours=tz_offset_hours)))
    store.add(Interaction(
        id=str(uuid.uuid4()), person_id=person_id, timestamp=ts,
        source_type=source_type, title=title,
    ))


def _add_person(store, id_, name, category="personal", circle=4,
                 strength=10.0, hidden=False, peripheral=False):
    p = PersonEntity(
        id=id_, canonical_name=name, category=category, dunbar_circle=circle,
        hidden=hidden, is_peripheral_contact=peripheral,
    )
    p.relationship_strength = strength
    store.add(p)
    return p


def _seed_synthetic_dataset(istore, pstore):
    """A dataset exercising: gmail sent/received filtering, hidden and
    peripheral exclusion, mixed UTC offsets at a trend boundary, a clearly
    neglected close contact, a clearly healthy close contact, distinct
    per-person counts for top_contacts/warming/cooling (no ties), and a
    couple of Dunbar circles for by_circle."""
    _add_person(pstore, MY_PERSON_ID, "Self", category="self", circle=-1, strength=100)

    # P_CLOSE: family, circle 1 — regular contact but NOT recently (neglected)
    _add_person(pstore, "p-close", "Close Family", category="family", circle=1, strength=95)
    for days_ago in (31, 35, 39, 43, 47):
        _add_interaction(istore, "p-close", days_ago=days_ago, source_type="imessage")

    # P_HEALTHY: family, circle 2 — regular AND recent contact (not neglected)
    _add_person(pstore, "p-healthy", "Healthy Family", category="family", circle=2, strength=85)
    for days_ago in (1, 5, 9, 13, 17):
        _add_interaction(istore, "p-healthy", days_ago=days_ago, source_type="imessage")
    # Extra recent volume so p-healthy has a distinct (higher) 30-day count
    # than everyone else, avoiding top_contacts tie-order ambiguity.
    for days_ago in (2, 3, 4):
        _add_interaction(istore, "p-healthy", days_ago=days_ago, source_type="imessage")

    # P_FRIEND: personal, circle 4 — cooling trend (fewer recent than previous)
    _add_person(pstore, "p-friend", "Friend", category="personal", circle=4, strength=40)
    for days_ago in (10, 20):  # recent (< 90 days)
        _add_interaction(istore, "p-friend", days_ago=days_ago, source_type="imessage")
    for days_ago in (100, 110, 120, 130, 140):  # previous (90-180 days)
        _add_interaction(istore, "p-friend", days_ago=days_ago, source_type="imessage")

    # P_WORK: work, circle 5 — gmail sent vs received filtering
    _add_person(pstore, "p-work", "Work Contact", category="work", circle=5, strength=20)
    for i in range(3):
        _add_interaction(istore, "p-work", days_ago=i + 1, source_type="gmail", title=f"→ Sent {i}")
    for i in range(2):
        _add_interaction(istore, "p-work", days_ago=i + 1, source_type="gmail", title=f"← Received {i}")

    # P_PERIPHERAL: family but peripheral — must be excluded entirely
    _add_person(pstore, "p-peripheral", "Peripheral", category="family", circle=1,
                strength=1, peripheral=True)
    for days_ago in (1, 2, 3, 4, 5):
        _add_interaction(istore, "p-peripheral", days_ago=days_ago, source_type="imessage")

    # P_HIDDEN: family but hidden — must be excluded entirely
    _add_person(pstore, "p-hidden", "Hidden", category="family", circle=1, strength=1, hidden=True)
    for days_ago in (1, 2, 3, 4, 5):
        _add_interaction(istore, "p-hidden", days_ago=days_ago, source_type="imessage")

    # A boundary interaction at exactly the 30-day cutoff, stored with a
    # non-UTC offset, to stress the exact/julianday comparison path.
    _add_person(pstore, "p-boundary", "Boundary", category="personal", circle=6, strength=5)
    _add_interaction(istore, "p-boundary", days_ago=30, source_type="whatsapp", tz_offset_hours=-7)

    # P_OLD_FRIEND: personal, circle 4 — interactions older than 365 days,
    # so a trend_period="year" window (previous bucket 365-730 days ago)
    # reaching further back than a days_back=365 pool must NOT surface them
    # (#897 review finding 1's exact repro case: 2 * trend_days > days_back).
    _add_person(pstore, "p-old-friend", "Old Friend", category="personal", circle=4, strength=30)
    for days_ago in (400, 420, 450):
        _add_interaction(istore, "p-old-friend", days_ago=days_ago, source_type="imessage")

    # A "today" interaction, to pin the pre-existing end-date exclusion
    # quirk (#897 review finding 8): the day-string `<=` bound against
    # `'<end> 23:59:59'` lexically excludes any row dated exactly on the
    # end day, so this must never show up in daily/top_contacts/trend
    # output even though it's within every window tested here.
    _add_interaction(istore, "p-healthy", days_ago=0, source_type="imessage")


def _assert_me_interactions_matches_oracle(result, expected):
    """Shared field-by-field comparison used by every days_back/trend_period
    combination TestMeInteractionsOracle exercises."""
    assert result.total_count == expected["total_count"]
    assert result.by_source == expected["by_source"]
    assert result.by_month == expected["by_month"]
    assert result.by_circle == expected["by_circle"]
    assert [d.model_dump() for d in result.daily] == expected["daily"]
    assert _as_set([tc.model_dump() for tc in result.top_contacts]) == _as_set(expected["top_contacts"])
    assert _as_set([w.model_dump() for w in result.warming]) == _as_set(expected["warming"])
    assert _as_set([c.model_dump() for c in result.cooling]) == _as_set(expected["cooling"])
    assert _as_set([n.model_dump() for n in result.neglected_contacts]) == _as_set(expected["neglected_contacts"])
    assert result.relationship_health_score == expected["relationship_health_score"]
    assert result.health_score_average == expected["health_score_average"]
    assert [h.model_dump() for h in result.health_score_history] == expected["health_score_history"]
    assert result.tracked_relationships == []
    assert expected["tracked_relationships"] == []


class TestMeInteractionsOracle:
    def test_matches_oracle_on_synthetic_dataset(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        expected = oracle_me_interactions(istore, pstore, MY_PERSON_ID, days_back=365)
        result = get_me_interactions(days_back=365)

        _assert_me_interactions_matches_oracle(result, expected)

    def test_matches_oracle_when_trend_window_exceeds_days_back_quarter(self, monkeypatch, stores):
        """#897 review finding 1: days_back=30 with the default
        trend_period="quarter" (90-day trend windows) means
        2 * trend_days (180) > days_back (30) — the trend/top-contact
        queries must clip to the days_back pool exactly like the original
        Python implementation's `all_interactions` fetch did, not reach
        further back than days_back on their own."""
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        expected = oracle_me_interactions(
            istore, pstore, MY_PERSON_ID, days_back=30, trend_period="quarter",
        )
        result = get_me_interactions(days_back=30, trend_period="quarter")

        _assert_me_interactions_matches_oracle(result, expected)
        # p-friend's "previous" trend interactions (100-140 days ago) exist
        # but are outside the 30-day pool, so they must not count — without
        # the pool clamp, previous_count would be 5 (cooling); clamped,
        # it's 0 (warming, since the pool's own 2 recent-window rows still
        # count). A regression here would mean either the test dataset
        # stopped exercising the clamp, or the clamp itself broke.
        friend_warming = next((w for w in result.warming if w.person_id == "p-friend"), None)
        assert friend_warming is not None, "p-friend should warm once the 90-180 day pool is clipped away"
        assert friend_warming.previous_count == 0
        assert not any(c.person_id == "p-friend" for c in result.cooling)

    def test_matches_oracle_when_trend_window_exceeds_days_back_year(self, monkeypatch, stores):
        """#897 review finding 1's exact reproduction: days_back=365 with
        trend_period="year" (365-day trend windows) means
        2 * trend_days (730) > days_back (365) — the "previous" bucket
        would otherwise reach back to 730 days and pick up p-old-friend's
        400-450-day-old interactions, which the original algorithm's
        365-day-bounded pool never contained."""
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        expected = oracle_me_interactions(
            istore, pstore, MY_PERSON_ID, days_back=365, trend_period="year",
        )
        result = get_me_interactions(days_back=365, trend_period="year")

        _assert_me_interactions_matches_oracle(result, expected)
        assert not any(w.person_id == "p-old-friend" for w in result.warming)
        assert not any(c.person_id == "p-old-friend" for c in result.cooling)

    def test_neglected_contact_identified_correctly(self, monkeypatch, stores):
        """Sanity check on top of the oracle diff: the specific close contact
        with a stale last-contact date is flagged, the recently-active one
        isn't, and peripheral/hidden people never appear."""
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        result = get_me_interactions(days_back=365)
        neglected_ids = {n.person_id for n in result.neglected_contacts}
        assert "p-close" in neglected_ids
        assert "p-healthy" not in neglected_ids
        assert "p-peripheral" not in neglected_ids
        assert "p-hidden" not in neglected_ids

    def test_gmail_received_excluded_from_totals(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        result = get_me_interactions(days_back=30)
        # 3 sent, 2 received seeded for p-work; only the 3 sent should count.
        assert result.by_source.get("gmail") == 3

    def test_hidden_and_peripheral_excluded_from_by_circle(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        result = get_me_interactions(days_back=30)
        # p-peripheral and p-hidden are both circle 1; if they leaked in,
        # circle "1" would show 10 interactions (5 each) instead of 0.
        assert result.by_circle.get("1", 0) == 0


class TestMeStatsOracle:
    def test_totals_match_sql_sum_semantics(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)
        for pid, emails, meetings, messages in [
            ("p-close", 10, 2, 50), ("p-healthy", 5, 1, 20), ("p-hidden", 999, 999, 999),
        ]:
            person = pstore.get_by_id(pid)
            person.email_count, person.meeting_count, person.message_count = emails, meetings, messages
            pstore.update(person)

        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)

        result = get_me_stats()
        assert result.total_emails == 15  # 10 + 5, hidden person excluded
        assert result.total_meetings == 3
        assert result.total_messages == 70


def _assert_family_interactions_matches_oracle(result, expected):
    """Shared field-by-field comparison for TestFamilyInteractionsOracle."""
    assert result.selected_ids == expected["selected_ids"]
    assert result.selected_names == expected["selected_names"]
    assert result.total_count == expected["total_count"]
    assert result.by_source == expected["by_source"]
    assert result.by_month == expected["by_month"]
    assert [d.model_dump() for d in result.daily] == expected["daily"]
    assert _as_set([tc.model_dump() for tc in result.top_contacts]) == _as_set(expected["top_contacts"])
    assert _as_set([w.model_dump() for w in result.warming]) == _as_set(expected["warming"])
    assert _as_set([c.model_dump() for c in result.cooling]) == _as_set(expected["cooling"])
    assert result.relationship_health_score == expected["relationship_health_score"]
    assert result.health_score_average == expected["health_score_average"]
    assert [h.model_dump() for h in result.health_score_history] == expected["health_score_history"]


class TestFamilyInteractionsOracle:
    def test_matches_oracle_on_synthetic_dataset(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)
        selected_ids = ["p-close", "p-healthy", "p-friend"]

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        expected = oracle_family_interactions(istore, pstore, MY_PERSON_ID, selected_ids, days_back=365)
        result = get_family_interactions(person_ids=",".join(selected_ids), days_back=365)

        _assert_family_interactions_matches_oracle(result, expected)

    def test_matches_oracle_when_trend_window_exceeds_days_back(self, monkeypatch, stores):
        """#897 review finding 1 also applies to /family/interactions: a
        trend window longer than half of days_back must clip to the
        days_back pool, matching the original algorithm's `all_interactions`
        fetch (bounded to days_back) rather than reaching further back on
        its own."""
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)
        selected_ids = ["p-friend", "p-old-friend"]

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        expected = oracle_family_interactions(
            istore, pstore, MY_PERSON_ID, selected_ids, days_back=30, trend_period="quarter",
        )
        result = get_family_interactions(
            person_ids=",".join(selected_ids), days_back=30, trend_period="quarter",
        )

        _assert_family_interactions_matches_oracle(result, expected)
        # p-old-friend has no interactions within 180 days of "now" at all,
        # so it must never appear in either bucket; p-friend's 100-140-day
        # "previous" interactions are outside the 30-day pool, so it warms
        # (2 recent, 0 previous) rather than cools (2 recent, 5 previous).
        assert not any(w.person_id == "p-old-friend" for w in result.warming)
        assert not any(c.person_id == "p-old-friend" for c in result.cooling)
        friend_warming = next((w for w in result.warming if w.person_id == "p-friend"), None)
        assert friend_warming is not None
        assert friend_warming.previous_count == 0

    def test_gmail_received_not_excluded_unlike_me_dashboard(self, monkeypatch, stores):
        """Family/interactions has no "sent email only" rule — a real
        behavioral difference from /me/interactions that must be preserved."""
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        result = get_family_interactions(person_ids="p-work", days_back=30)
        # All 5 gmail interactions (3 sent + 2 received) should count.
        assert result.by_source.get("gmail") == 5

    def test_restricts_to_selected_ids_only(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        result = get_family_interactions(person_ids="p-close", days_back=365)
        assert result.total_count == 5  # only p-close's 5 interactions


class TestFamilyTimeline:
    """
    #871's family/timeline acceptance criterion: filter by person_id IN (...)
    in SQL rather than loading every interaction in the window and filtering
    in Python. These use the same synthetic dataset as the aggregate oracle
    tests above.
    """

    def test_restricts_to_selected_ids_via_sql(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        # This call and test_excludes_non_selected_people()'s below share the
        # exact same parameters against the same seeded dataset shape -- they
        # only get independent results because the CRM aggregate response
        # cache (#917) is cleared between every test (autouse
        # reset_aggregate_cache() in tests/reset_singletons.py), not because
        # anything here makes the calls distinguishable to it.
        result = get_family_timeline(person_ids="p-close", source_type=None, days_back=365, date=None, offset=0, limit=100)
        assert result.count == 5
        assert all("Close Family" in item.title for item in result.items)

    def test_excludes_non_selected_people(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        # p-healthy has 8 interactions; selecting only p-close (5) must not
        # pick any of them up even though both are in the same window.
        result = get_family_timeline(person_ids="p-close", source_type=None, days_back=365, date=None, offset=0, limit=100)
        assert result.count == 5

    def test_pagination_has_more(self, monkeypatch, stores):
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        result = get_family_timeline(person_ids="p-healthy", source_type=None, days_back=365, date=None, offset=0, limit=3)
        assert len(result.items) == 3
        assert result.has_more is True

    def test_hidden_selected_person_shows_unknown_name(self, monkeypatch, stores):
        """A selected person who happens to be hidden still has their
        interactions shown (family/timeline never excludes by hidden status,
        unlike /me/timeline), but the name lookup shows "Unknown" — matching
        get_all()'s default (hidden-excluding) view, not get_by_id()'s."""
        istore, pstore = stores
        _seed_synthetic_dataset(istore, pstore)

        monkeypatch.setattr('api.routes.crm.get_interaction_store', lambda: istore)
        monkeypatch.setattr('api.routes.crm.get_person_entity_store', lambda: pstore)
        monkeypatch.setattr('api.routes.crm.MY_PERSON_ID', MY_PERSON_ID)

        result = get_family_timeline(person_ids="p-hidden", source_type=None, days_back=365, date=None, offset=0, limit=100)
        assert result.count == 5
        assert all(item.title.startswith("Unknown:") for item in result.items)
