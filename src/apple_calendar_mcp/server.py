"""MCP server exposing Apple Calendar as a set of tools.

Tool docstrings are the model's only documentation at call time, so they spell
out accepted formats and defaults rather than deferring to the README.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial, wraps
from typing import Any, Awaitable, Callable, TypeVar

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from . import availability as free_busy
from .datetimes import (
    DateParseError,
    humanize_duration,
    parse_clock,
    parse_weekdays,
    parse_when,
    resolve_event_times,
    resolve_range,
    to_iso,
)
from .recurrence import RecurrenceError, parse_recurrence
from .store import CalendarError, CalendarStore

T = TypeVar("T")

INSTRUCTIONS = """\
Read and manage the local Apple Calendar (the same database as Calendar.app on
this Mac, including any iCloud, Google, or Exchange accounts configured there).

Dates accept ISO 8601 ("2026-03-04", "2026-03-04T14:30", "2026-03-04T14:30-08:00"),
the keywords now/today/tomorrow/yesterday, weekday names meaning the next such
day, and offsets like "+2h" or "+3d". Values without a timezone are read in the
Mac's local timezone.

Event identifiers come from list_events or search_events. For a repeating event
every occurrence shares one identifier, so pass occurrence_start to act on a
specific one, and span="this" or span="future" to choose how far an edit reaches.
"""

server = MCPServer("apple-calendar", version=__version__, instructions=INSTRUCTIONS)
calendar_store = CalendarStore()

# Hints let a client distinguish a harmless lookup from an irreversible delete
# and choose its own confirmation behaviour accordingly.
READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ADDS = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
CHANGES = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False
)


def tool_errors(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
    """Surface our own error messages to the model instead of a masked string.

    The SDK only forwards the text of a ToolError; anything else becomes a
    generic "Error executing tool ...". Our messages say what to do differently
    (which calendar names exist, which argument conflicts), so they are worth
    forwarding. ValueError is included because the tools raise it for argument
    validation; unexpected failures still propagate and stay masked.
    """

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except (CalendarError, DateParseError, RecurrenceError, ValueError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


async def _off_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking EventKit call without stalling the server's event loop."""
    return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))


# ----------------------------------------------------------------- read tools


@server.tool(annotations=READS)
@tool_errors
async def check_calendar_access() -> dict[str, Any]:
    """Report whether this server may read and write the Mac's calendars.

    Use this first when any other tool fails with a permission error; it
    explains the current state without triggering a system prompt.
    """
    return await _off_thread(calendar_store.access_report)


@server.tool(annotations=READS)
@tool_errors
async def list_calendars() -> dict[str, Any]:
    """List every calendar on this Mac, with its account, color, and writability.

    Use it to discover the exact calendar names accepted by the other tools, and
    to see which calendar new events land in by default.
    """
    calendars = await _off_thread(calendar_store.calendars)
    return {"count": len(calendars), "calendars": calendars}


@server.tool(annotations=READS)
@tool_errors
async def list_events(
    start: str | None = None,
    end: str | None = None,
    calendars: list[str] | None = None,
    query: str | None = None,
    include_canceled: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """List calendar events overlapping a time range.

    Args:
        start: Beginning of the range. Defaults to now.
        end: End of the range. A date-only value covers that whole day.
            Defaults to seven days after start.
        calendars: Restrict to these calendar names or identifiers. Defaults to all.
        query: Case-insensitive substring filter over title, location, and notes.
        include_canceled: Include events marked canceled. Defaults to False.
        limit: Maximum number of events to return.
    """
    begin, finish = resolve_range(start, end)
    events = await _off_thread(
        calendar_store.list_events,
        begin,
        finish,
        calendars=calendars,
        query=query,
        include_canceled=include_canceled,
        limit=limit,
    )
    return {
        "range": {"start": to_iso(begin), "end": to_iso(finish)},
        "count": len(events),
        "truncated": len(events) >= limit,
        "events": events,
    }


@server.tool(annotations=READS)
@tool_errors
async def search_events(
    query: str,
    start: str | None = None,
    end: str | None = None,
    calendars: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search events by text in their title, location, or notes.

    Args:
        query: Case-insensitive substring to look for.
        start: Beginning of the search window. Defaults to 90 days ago.
        end: End of the search window. Defaults to one year after start.
        calendars: Restrict to these calendar names or identifiers.
        limit: Maximum number of matches to return.
    """
    begin = parse_when(start) if start else parse_when("-90d")
    finish = parse_when(end) if end else begin + timedelta(days=365)
    events = await _off_thread(
        calendar_store.list_events, begin, finish, calendars=calendars, query=query, limit=limit
    )
    return {
        "query": query,
        "range": {"start": to_iso(begin), "end": to_iso(finish)},
        "count": len(events),
        "events": events,
    }


@server.tool(annotations=READS)
@tool_errors
async def get_event(event_id: str, occurrence_start: str | None = None) -> dict[str, Any]:
    """Get the full details of one event, including attendees, alarms, and notes.

    Args:
        event_id: Identifier from list_events or search_events.
        occurrence_start: For a repeating event, the start of the occurrence you
            mean. Without it, the earliest known occurrence is returned.
    """
    occurrence = parse_when(occurrence_start) if occurrence_start else None
    return await _off_thread(calendar_store.get_event, event_id, occurrence)


@server.tool(annotations=READS)
@tool_errors
async def find_free_slots(
    start: str | None = None,
    end: str | None = None,
    duration_minutes: int = 30,
    calendars: list[str] | None = None,
    between_hours: list[str] | None = None,
    days_of_week: list[str] | None = None,
    include_all_day: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Find open blocks of time that could host a meeting of a given length.

    Events marked "free" and canceled events never block a slot. All-day entries
    are ignored unless include_all_day is set, since holidays and birthdays would
    otherwise swallow every candidate slot.

    Args:
        start: Earliest acceptable time. Defaults to now.
        end: Latest acceptable time. Defaults to seven days after start.
        duration_minutes: Minimum length of a usable slot.
        calendars: Only consider conflicts on these calendars. Defaults to all.
        between_hours: Daily window as ["09:00", "17:00"]. Defaults to working hours.
        days_of_week: Allowed days, e.g. ["mon","tue"]. Defaults to Monday-Friday.
        include_all_day: Treat all-day events as busy time.
        limit: Maximum number of slots to return.
    """
    if duration_minutes <= 0:
        raise ValueError("duration_minutes must be positive.")

    begin, finish = resolve_range(start, end)

    if between_hours is None:
        window = (parse_clock("09:00"), parse_clock("17:00"))
    elif len(between_hours) != 2:
        raise ValueError('between_hours must be two times, e.g. ["09:00", "17:00"].')
    else:
        window = (parse_clock(between_hours[0]), parse_clock(between_hours[1]))

    weekdays = parse_weekdays(days_of_week) if days_of_week else [0, 1, 2, 3, 4]

    busy = await _off_thread(
        calendar_store.busy_intervals,
        begin,
        finish,
        calendars=calendars,
        include_all_day=include_all_day,
    )
    slots = free_busy.find_free_slots(
        busy,
        begin,
        finish,
        minimum=timedelta(minutes=duration_minutes),
        between=window,
        weekdays=weekdays,
        limit=limit,
    )
    return {
        "range": {"start": to_iso(begin), "end": to_iso(finish)},
        "duration_minutes": duration_minutes,
        "count": len(slots),
        "slots": [
            {
                "start": to_iso(slot_start),
                "end": to_iso(slot_end),
                "duration": humanize_duration((slot_end - slot_start).total_seconds()),
            }
            for slot_start, slot_end in slots
        ],
    }


# ---------------------------------------------------------------- write tools


@server.tool(annotations=ADDS)
@tool_errors
async def create_event(
    title: str,
    start: str,
    end: str | None = None,
    duration_minutes: int | None = None,
    calendar: str | None = None,
    all_day: bool = False,
    location: str | None = None,
    notes: str | None = None,
    url: str | None = None,
    availability: str | None = None,
    alarms_minutes_before: list[int] | None = None,
    recurrence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a calendar event and return it as saved.

    Args:
        title: Event title.
        start: When it begins. For an all-day event, the first day.
        end: When it ends. For an all-day event this is the last day, inclusive.
            Omit to use duration_minutes.
        duration_minutes: Length instead of an end time. Defaults to 60 for a
            timed event. Cannot be combined with end.
        calendar: Calendar name or identifier. Defaults to the Mac's default
            calendar for new events.
        all_day: Create an all-day event rather than a timed one.
        location: Free-text location.
        notes: Body text / description.
        url: A URL to attach, such as a video-call link.
        availability: How the time shows to others: busy, free, tentative, or
            unavailable.
        alarms_minutes_before: Alerts, in minutes before the start, e.g. [10, 60].
        recurrence: Repeat rule, e.g. {"frequency": "weekly", "interval": 1,
            "days_of_week": ["mon","wed"], "count": 10} or {"frequency":
            "monthly", "until": "2026-12-31"}. Use count or until, not both.
    """
    begin, finish = resolve_event_times(start, end, duration_minutes, all_day=all_day)
    spec = parse_recurrence(recurrence)
    event = await _off_thread(
        calendar_store.create_event,
        title=title,
        start=begin,
        end=finish,
        calendar=calendar,
        all_day=all_day,
        location=location,
        notes=notes,
        url=url,
        availability=availability,
        alarms_minutes_before=alarms_minutes_before,
        recurrence=spec,
    )
    return {"created": True, "event": event}


@server.tool(annotations=CHANGES)
@tool_errors
async def update_event(
    event_id: str,
    occurrence_start: str | None = None,
    span: str = "this",
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    duration_minutes: int | None = None,
    all_day: bool | None = None,
    calendar: str | None = None,
    location: str | None = None,
    notes: str | None = None,
    url: str | None = None,
    availability: str | None = None,
    alarms_minutes_before: list[int] | None = None,
    recurrence: dict[str, Any] | None = None,
    clear_recurrence: bool = False,
) -> dict[str, Any]:
    """Change an existing event. Only the arguments you pass are modified.

    Args:
        event_id: Identifier from list_events or search_events.
        occurrence_start: For a repeating event, which occurrence to edit.
        span: "this" edits one occurrence, "future" edits it and all later ones.
        title: New title.
        start: New start time.
        end: New end time.
        duration_minutes: New length, measured from the new or existing start.
            Cannot be combined with end.
        all_day: Convert between all-day and timed.
        calendar: Move the event to another calendar.
        location: New location. Pass an empty string to clear it.
        notes: New notes. Pass an empty string to clear them.
        url: New URL. Pass an empty string to clear it.
        availability: busy, free, tentative, or unavailable.
        alarms_minutes_before: Replaces all existing alerts. Pass [] to remove them.
        recurrence: Replace the repeat rule. See create_event.
        clear_recurrence: Strip the repeat rule, leaving a single event.
    """
    if end is not None and duration_minutes is not None:
        raise ValueError("Pass either end or duration_minutes, not both.")
    if recurrence is not None and clear_recurrence:
        raise ValueError("Pass either recurrence or clear_recurrence, not both.")

    occurrence = parse_when(occurrence_start) if occurrence_start else None
    new_start = parse_when(start) if start else None
    new_end = parse_when(end, now=new_start) if end else None

    if duration_minutes is not None:
        if duration_minutes <= 0:
            raise ValueError("duration_minutes must be positive.")
        anchor = new_start
        if anchor is None:
            current = await _off_thread(calendar_store.get_event, event_id, occurrence)
            anchor = parse_when(current["start"])
        new_end = anchor + timedelta(minutes=duration_minutes)

    spec = parse_recurrence(recurrence)
    event = await _off_thread(
        calendar_store.update_event,
        event_id,
        occurrence_start=occurrence,
        span=span,
        title=title,
        start=new_start,
        end=new_end,
        all_day=all_day,
        calendar=calendar,
        location=location,
        notes=notes,
        url=url,
        availability=availability,
        alarms_minutes_before=alarms_minutes_before,
        recurrence=spec,
        clear_recurrence=clear_recurrence,
    )
    return {"updated": True, "span": span, "event": event}


@server.tool(annotations=CHANGES)
@tool_errors
async def delete_event(
    event_id: str,
    occurrence_start: str | None = None,
    span: str = "this",
) -> dict[str, Any]:
    """Delete an event. This cannot be undone from here.

    Args:
        event_id: Identifier from list_events or search_events.
        occurrence_start: For a repeating event, which occurrence to delete.
        span: "this" deletes one occurrence, "future" deletes it and all later ones.
    """
    occurrence = parse_when(occurrence_start) if occurrence_start else None
    return await _off_thread(
        calendar_store.delete_event, event_id, occurrence_start=occurrence, span=span
    )


def run() -> None:
    """Serve over stdio, the transport MCP clients launch this with."""
    server.run()
