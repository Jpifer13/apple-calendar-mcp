"""EventKit-backed calendar access.

This is the only module that talks to macOS frameworks. It converts EventKit
objects into plain JSON-friendly dicts so the MCP layer above never handles
PyObjC types, and raises :class:`CalendarError` with actionable messages
instead of letting ``None``/``NSError`` pairs leak upwards.
"""

from __future__ import annotations

import threading
import time as time_module
from datetime import datetime, timedelta
from typing import Any

from .availability import Interval
from .datetimes import local_timezone, to_iso
from .recurrence import RecurrenceSpec

# EventKit enum values, inlined so the names read clearly at the call sites.
ENTITY_TYPE_EVENT = 0

AUTH_NOT_DETERMINED = 0
AUTH_RESTRICTED = 1
AUTH_DENIED = 2
AUTH_AUTHORIZED = 3  # legacy full access (pre-macOS 14)
AUTH_WRITE_ONLY = 4
AUTH_FULL_ACCESS = 5

AUTH_NAMES = {
    AUTH_NOT_DETERMINED: "not_determined",
    AUTH_RESTRICTED: "restricted",
    AUTH_DENIED: "denied",
    AUTH_AUTHORIZED: "full_access",
    AUTH_WRITE_ONLY: "write_only",
    AUTH_FULL_ACCESS: "full_access",
}
READABLE_STATUSES = {AUTH_AUTHORIZED, AUTH_FULL_ACCESS}

SPAN_THIS_EVENT = 0
SPAN_FUTURE_EVENTS = 1
SPANS = {"this": SPAN_THIS_EVENT, "future": SPAN_FUTURE_EVENTS}

AVAILABILITY_NAMES = {-1: "unspecified", 0: "busy", 1: "free", 2: "tentative", 3: "unavailable"}
AVAILABILITY_VALUES = {name: value for value, name in AVAILABILITY_NAMES.items() if value >= 0}
STATUS_NAMES = {0: "none", 1: "confirmed", 2: "tentative", 3: "canceled"}
CALENDAR_TYPE_NAMES = {0: "local", 1: "caldav", 2: "exchange", 3: "subscription", 4: "birthday"}
PARTICIPANT_STATUS_NAMES = {
    0: "unknown", 1: "pending", 2: "accepted", 3: "declined",
    4: "tentative", 5: "delegated", 6: "completed", 7: "in_process",
}

FREQUENCY_VALUES = {"daily": 0, "weekly": 1, "monthly": 2, "yearly": 3}
FREQUENCY_NAMES = {value: name for name, value in FREQUENCY_VALUES.items()}

# EventKit weekdays run Sunday=1..Saturday=7; Python's weekday() is Monday=0.
_PY_TO_EK_WEEKDAY = {py: ((py + 1) % 7) + 1 for py in range(7)}
_EK_TO_PY_WEEKDAY = {ek: py for py, ek in _PY_TO_EK_WEEKDAY.items()}

# EventKit refuses predicates spanning more than four years.
MAX_QUERY_SPAN = timedelta(days=365 * 4)

_REFRESH_INTERVAL_SECONDS = 30.0
_ACCESS_TIMEOUT_SECONDS = 120.0

_PERMISSION_HELP = (
    "Grant calendar access to the app running this server (Terminal, iTerm, or "
    "Claude) in System Settings > Privacy & Security > Calendars, then restart "
    "that app so the new permission takes effect."
)


class CalendarError(RuntimeError):
    """A calendar operation could not be completed."""


class AccessError(CalendarError):
    """The process is not allowed to read or write the calendar database."""


def _frameworks() -> tuple[Any, Any]:
    try:
        import EventKit  # type: ignore[import-not-found]
        import Foundation  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on platform
        raise CalendarError(
            "PyObjC's EventKit bindings are unavailable. This server only runs on "
            "macOS; install dependencies with `poetry install`."
        ) from exc
    return EventKit, Foundation


class CalendarStore:
    """A thin, synchronous wrapper around ``EKEventStore``."""

    def __init__(self) -> None:
        self._store: Any = None
        self._lock = threading.Lock()
        self._last_refresh = 0.0

    # ---------------------------------------------------------------- access

    def authorization_status(self) -> int:
        EventKit, _ = _frameworks()
        return int(EventKit.EKEventStore.authorizationStatusForEntityType_(ENTITY_TYPE_EVENT))

    def access_report(self) -> dict[str, Any]:
        """Describe current permission state without forcing a prompt."""
        status = self.authorization_status()
        name = AUTH_NAMES.get(status, "unknown")
        report: dict[str, Any] = {
            "status": name,
            "can_read": status in READABLE_STATUSES,
            "can_write": status in READABLE_STATUSES or status == AUTH_WRITE_ONLY,
        }
        if status in READABLE_STATUSES:
            report["detail"] = "Full calendar access granted."
        elif status == AUTH_WRITE_ONLY:
            report["detail"] = (
                "Only write access was granted, so events cannot be listed. " + _PERMISSION_HELP
            )
        elif status == AUTH_DENIED:
            report["detail"] = "Calendar access was denied. " + _PERMISSION_HELP
        elif status == AUTH_RESTRICTED:
            report["detail"] = (
                "Calendar access is restricted by a device policy or parental controls."
            )
        else:
            report["detail"] = (
                "Calendar access has not been requested yet; the first calendar "
                "operation will trigger the system prompt."
            )
        if status in READABLE_STATUSES:
            try:
                report["calendars"] = len(self.calendars())
            except CalendarError:
                pass
        return report

    def _ensure_store(self) -> Any:
        with self._lock:
            if self._store is None:
                EventKit, _ = _frameworks()
                self._store = EventKit.EKEventStore.alloc().init()
                self._request_access(self._store)
            return self._store

    def _request_access(self, store: Any) -> None:
        status = self.authorization_status()
        if status in READABLE_STATUSES:
            return
        if status in (AUTH_DENIED, AUTH_RESTRICTED, AUTH_WRITE_ONLY):
            raise AccessError(self.access_report()["detail"])

        finished = threading.Event()
        outcome: dict[str, Any] = {}

        def handler(granted: bool, error: Any) -> None:
            outcome["granted"] = bool(granted)
            outcome["error"] = error
            finished.set()

        # macOS 14 split the old all-or-nothing request into full/write-only.
        if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
            store.requestFullAccessToEventsWithCompletion_(handler)
        else:  # pragma: no cover - older macOS
            store.requestAccessToEntityType_completion_(ENTITY_TYPE_EVENT, handler)

        if not finished.wait(_ACCESS_TIMEOUT_SECONDS):
            raise AccessError(
                "Timed out waiting for the calendar permission prompt to be answered. "
                + _PERMISSION_HELP
            )
        if not outcome.get("granted"):
            error = outcome.get("error")
            reason = f" ({error.localizedDescription()})" if error is not None else ""
            raise AccessError(f"Calendar access was not granted{reason}. " + _PERMISSION_HELP)

    def _refresh_if_stale(self) -> None:
        """Pick up edits made in Calendar.app or synced from other devices."""
        now = time_module.monotonic()
        if now - self._last_refresh < _REFRESH_INTERVAL_SECONDS:
            return
        self._last_refresh = now
        try:
            self._ensure_store().refreshSourcesIfNecessary()
        except Exception:  # pragma: no cover - best effort only
            pass

    # ------------------------------------------------------------- calendars

    def calendars(self) -> list[dict[str, Any]]:
        store = self._ensure_store()
        calendars = store.calendarsForEntityType_(ENTITY_TYPE_EVENT) or []
        default = store.defaultCalendarForNewEvents()
        default_id = default.calendarIdentifier() if default is not None else None
        return [_calendar_to_dict(c, is_default=c.calendarIdentifier() == default_id) for c in calendars]

    def resolve_calendar(self, reference: str | None) -> Any:
        """Find a calendar by identifier or (case-insensitive) title."""
        store = self._ensure_store()
        if not reference:
            calendar = store.defaultCalendarForNewEvents()
            if calendar is None:
                raise CalendarError(
                    "macOS has no default calendar for new events; pass `calendar` explicitly."
                )
            return calendar

        calendar = store.calendarWithIdentifier_(reference)
        if calendar is not None:
            return calendar

        wanted = reference.strip().lower()
        matches = [
            c
            for c in (store.calendarsForEntityType_(ENTITY_TYPE_EVENT) or [])
            if (c.title() or "").strip().lower() == wanted
        ]
        if not matches:
            available = ", ".join(sorted(c.title() for c in (store.calendarsForEntityType_(ENTITY_TYPE_EVENT) or [])))
            raise CalendarError(f"No calendar named {reference!r}. Available: {available}.")
        if len(matches) > 1:
            ids = ", ".join(c.calendarIdentifier() for c in matches)
            raise CalendarError(
                f"{len(matches)} calendars are named {reference!r}; pass an identifier instead ({ids})."
            )
        return matches[0]

    def _resolve_calendars(self, references: list[str] | None) -> list[Any] | None:
        if not references:
            return None
        return [self.resolve_calendar(ref) for ref in references]

    # ---------------------------------------------------------------- events

    def events_between(
        self,
        start: datetime,
        end: datetime,
        *,
        calendars: list[str] | None = None,
    ) -> list[Any]:
        """Raw ``EKEvent`` objects overlapping the window, in start order."""
        if end - start > MAX_QUERY_SPAN:
            raise CalendarError(
                "EventKit cannot query more than four years at a time; narrow the range."
            )
        store = self._ensure_store()
        self._refresh_if_stale()
        predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
            _to_nsdate(start), _to_nsdate(end), self._resolve_calendars(calendars)
        )
        events = list(store.eventsMatchingPredicate_(predicate) or [])
        events.sort(key=lambda e: (e.startDate().timeIntervalSince1970(), e.title() or ""))
        return events

    def list_events(
        self,
        start: datetime,
        end: datetime,
        *,
        calendars: list[str] | None = None,
        query: str | None = None,
        include_canceled: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        events = self.events_between(start, end, calendars=calendars)
        needle = query.strip().lower() if query else None
        results: list[dict[str, Any]] = []
        for event in events:
            if not include_canceled and int(event.status() or 0) == 3:
                continue
            if needle and not _matches(event, needle):
                continue
            results.append(event_to_dict(event))
            if limit is not None and len(results) >= limit:
                break
        return results

    def busy_intervals(
        self,
        start: datetime,
        end: datetime,
        *,
        calendars: list[str] | None = None,
        include_tentative: bool = True,
        include_all_day: bool = False,
    ) -> list[Interval]:
        """Intervals that should block scheduling within the window."""
        intervals: list[Interval] = []
        for event in self.events_between(start, end, calendars=calendars):
            if int(event.status() or 0) == 3:  # canceled
                continue
            availability = int(event.availability())
            if availability == 1:  # explicitly free
                continue
            if availability == 2 and not include_tentative:
                continue
            if event.isAllDay() and not include_all_day:
                # All-day entries are usually markers (birthdays, holidays,
                # travel days) rather than blocked time. Skip them wholesale
                # rather than trusting availability: Calendar.app saves new
                # all-day events as "busy", so honouring that flag would let
                # every birthday swallow a whole day of candidate slots.
                continue
            intervals.append((_from_nsdate(event.startDate()), _from_nsdate(event.endDate())))
        return intervals

    def find_event(self, event_id: str, occurrence_start: datetime | None = None) -> Any:
        """Locate an event, optionally a specific occurrence of a repeating one."""
        store = self._ensure_store()
        self._refresh_if_stale()

        if occurrence_start is not None:
            window_start = occurrence_start - timedelta(days=1)
            window_end = occurrence_start + timedelta(days=2)
            predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
                _to_nsdate(window_start), _to_nsdate(window_end), None
            )
            candidates = [
                event
                for event in (store.eventsMatchingPredicate_(predicate) or [])
                if event.eventIdentifier() == event_id
            ]
            if candidates:
                return min(
                    candidates,
                    key=lambda e: abs(_from_nsdate(e.startDate()) - occurrence_start),
                )

        event = store.eventWithIdentifier_(event_id)
        if event is None:
            raise CalendarError(
                f"No event with identifier {event_id!r}. Identifiers come from "
                "list_events; they change if an event is deleted and recreated."
            )
        return event

    def get_event(self, event_id: str, occurrence_start: datetime | None = None) -> dict[str, Any]:
        return event_to_dict(self.find_event(event_id, occurrence_start), detailed=True)

    def create_event(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        calendar: str | None = None,
        all_day: bool = False,
        location: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        availability: str | None = None,
        alarms_minutes_before: list[int] | None = None,
        recurrence: RecurrenceSpec | None = None,
    ) -> dict[str, Any]:
        EventKit, Foundation = _frameworks()
        store = self._ensure_store()

        target = self.resolve_calendar(calendar)
        if not target.allowsContentModifications():
            raise CalendarError(
                f"The calendar {target.title()!r} is read-only (subscribed or delegated)."
            )

        event = EventKit.EKEvent.eventWithEventStore_(store)
        event.setCalendar_(target)
        event.setTitle_(title)
        event.setAllDay_(bool(all_day))
        event.setStartDate_(_to_nsdate(start))
        event.setEndDate_(_to_nsdate(end))
        if not all_day:
            event.setTimeZone_(Foundation.NSTimeZone.localTimeZone())
        self._apply_optional_fields(
            event,
            location=location,
            notes=notes,
            url=url,
            availability=availability,
            alarms_minutes_before=alarms_minutes_before,
            recurrence=recurrence,
        )

        self._save(event, SPAN_THIS_EVENT)
        return event_to_dict(event, detailed=True)

    def update_event(
        self,
        event_id: str,
        *,
        occurrence_start: datetime | None = None,
        span: str = "this",
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        all_day: bool | None = None,
        calendar: str | None = None,
        location: str | None = None,
        notes: str | None = None,
        url: str | None = None,
        availability: str | None = None,
        alarms_minutes_before: list[int] | None = None,
        recurrence: RecurrenceSpec | None = None,
        clear_recurrence: bool = False,
    ) -> dict[str, Any]:
        event = self.find_event(event_id, occurrence_start)

        if title is not None:
            event.setTitle_(title)
        if all_day is not None:
            event.setAllDay_(bool(all_day))
        if start is not None:
            event.setStartDate_(_to_nsdate(start))
        if end is not None:
            event.setEndDate_(_to_nsdate(end))
        if calendar is not None:
            target = self.resolve_calendar(calendar)
            if not target.allowsContentModifications():
                raise CalendarError(f"The calendar {target.title()!r} is read-only.")
            event.setCalendar_(target)

        self._apply_optional_fields(
            event,
            location=location,
            notes=notes,
            url=url,
            availability=availability,
            alarms_minutes_before=alarms_minutes_before,
            recurrence=recurrence,
            clear_recurrence=clear_recurrence,
        )

        if _from_nsdate(event.endDate()) <= _from_nsdate(event.startDate()) and not event.isAllDay():
            raise CalendarError("The update would leave the event ending before it starts.")

        self._save(event, _span_value(span))
        return event_to_dict(event, detailed=True)

    def delete_event(
        self,
        event_id: str,
        *,
        occurrence_start: datetime | None = None,
        span: str = "this",
    ) -> dict[str, Any]:
        store = self._ensure_store()
        event = self.find_event(event_id, occurrence_start)
        snapshot = event_to_dict(event)

        ok, error = store.removeEvent_span_commit_error_(event, _span_value(span), True, None)
        if not ok:
            raise CalendarError(f"Could not delete the event: {_describe_error(error)}")
        return {"deleted": True, "span": span, "event": snapshot}

    # --------------------------------------------------------------- helpers

    def _apply_optional_fields(
        self,
        event: Any,
        *,
        location: str | None,
        notes: str | None,
        url: str | None,
        availability: str | None,
        alarms_minutes_before: list[int] | None,
        recurrence: RecurrenceSpec | None,
        clear_recurrence: bool = False,
    ) -> None:
        EventKit, Foundation = _frameworks()

        if location is not None:
            event.setLocation_(location or None)
        if notes is not None:
            event.setNotes_(notes or None)
        if url is not None:
            event.setURL_(Foundation.NSURL.URLWithString_(url) if url else None)

        if availability is not None:
            key = availability.strip().lower()
            if key not in AVAILABILITY_VALUES:
                raise CalendarError(
                    f"Unknown availability {availability!r}. "
                    f"Use one of: {', '.join(sorted(AVAILABILITY_VALUES))}."
                )
            event.setAvailability_(AVAILABILITY_VALUES[key])

        if alarms_minutes_before is not None:
            for alarm in list(event.alarms() or []):
                event.removeAlarm_(alarm)
            for minutes in alarms_minutes_before:
                if minutes < 0:
                    raise CalendarError("alarms_minutes_before values must be zero or positive.")
                event.addAlarm_(EventKit.EKAlarm.alarmWithRelativeOffset_(-float(minutes) * 60.0))

        if clear_recurrence:
            event.setRecurrenceRules_(None)
        elif recurrence is not None:
            event.setRecurrenceRules_([_build_recurrence_rule(recurrence)])

    def _save(self, event: Any, span: int) -> None:
        store = self._ensure_store()
        ok, error = store.saveEvent_span_commit_error_(event, span, True, None)
        if not ok:
            raise CalendarError(f"Could not save the event: {_describe_error(error)}")


# --------------------------------------------------------------- conversions


def _to_nsdate(value: datetime) -> Any:
    _, Foundation = _frameworks()
    return Foundation.NSDate.dateWithTimeIntervalSince1970_(value.timestamp())


def _from_nsdate(value: Any) -> datetime:
    return datetime.fromtimestamp(value.timeIntervalSince1970(), tz=local_timezone())


def _span_value(span: str) -> int:
    key = (span or "this").strip().lower()
    if key not in SPANS:
        raise CalendarError(f"span must be 'this' or 'future', not {span!r}.")
    return SPANS[key]


def _describe_error(error: Any) -> str:
    if error is None:
        return "EventKit reported a failure without a reason."
    try:
        return str(error.localizedDescription())
    except Exception:  # pragma: no cover - defensive
        return str(error)


def _matches(event: Any, needle: str) -> bool:
    for field in (event.title(), event.location(), event.notes()):
        if field and needle in str(field).lower():
            return True
    return False


def _calendar_to_dict(calendar: Any, *, is_default: bool = False) -> dict[str, Any]:
    source = calendar.source()
    return {
        "id": calendar.calendarIdentifier(),
        "title": calendar.title(),
        "color": _color_to_hex(calendar.color()),
        "type": CALENDAR_TYPE_NAMES.get(int(calendar.type()), "unknown"),
        "account": source.title() if source is not None else None,
        "writable": bool(calendar.allowsContentModifications()),
        "is_default": is_default,
    }


def _color_to_hex(color: Any) -> str | None:
    if color is None:
        return None
    try:
        import AppKit  # type: ignore[import-not-found]

        rgb = color.colorUsingColorSpace_(AppKit.NSColorSpace.sRGBColorSpace())
        if rgb is None:
            return None
        channels = (rgb.redComponent(), rgb.greenComponent(), rgb.blueComponent())
        return "#" + "".join(f"{round(c * 255):02x}" for c in channels)
    except Exception:
        return None


def event_to_dict(event: Any, *, detailed: bool = False) -> dict[str, Any]:
    """Convert an ``EKEvent`` into a JSON-serializable summary."""
    start = _from_nsdate(event.startDate())
    end = _from_nsdate(event.endDate())
    all_day = bool(event.isAllDay())
    calendar = event.calendar()
    url = event.URL()

    payload: dict[str, Any] = {
        "id": event.eventIdentifier(),
        "title": event.title() or "(no title)",
        "all_day": all_day,
        "start": start.date().isoformat() if all_day else to_iso(start),
        "end": end.date().isoformat() if all_day else to_iso(end),
        "calendar": calendar.title() if calendar is not None else None,
        "calendar_id": calendar.calendarIdentifier() if calendar is not None else None,
        "location": event.location() or None,
        "url": str(url.absoluteString()) if url is not None else None,
        "availability": AVAILABILITY_NAMES.get(int(event.availability()), "unspecified"),
        "status": STATUS_NAMES.get(int(event.status() or 0), "none"),
        "recurring": bool(event.hasRecurrenceRules()),
    }
    if not all_day:
        payload["duration_minutes"] = round((end - start).total_seconds() / 60)

    notes = event.notes()
    if notes:
        payload["notes"] = notes if detailed else _truncate(notes, 280)

    if payload["recurring"]:
        rules = list(event.recurrenceRules() or [])
        if rules:
            payload["recurrence"] = _recurrence_to_dict(rules[0])
        payload["occurrence_start"] = to_iso(start)

    if detailed:
        payload["alarms_minutes_before"] = _alarms_to_minutes(event)
        payload["organizer"] = _participant_to_dict(event.organizer())
        payload["attendees"] = [
            _participant_to_dict(p) for p in (event.attendees() or []) if p is not None
        ]
        modified = event.lastModifiedDate()
        if modified is not None:
            payload["last_modified"] = to_iso(_from_nsdate(modified))

    return payload


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _alarms_to_minutes(event: Any) -> list[int]:
    minutes: list[int] = []
    for alarm in event.alarms() or []:
        offset = alarm.relativeOffset()
        if offset is not None:
            minutes.append(int(round(-float(offset) / 60.0)))
    return minutes


def _participant_to_dict(participant: Any) -> dict[str, Any] | None:
    if participant is None:
        return None
    url = participant.URL()
    email = None
    if url is not None:
        text = str(url.absoluteString() or "")
        email = text[len("mailto:") :] if text.startswith("mailto:") else text or None
    return {
        "name": participant.name() or email,
        "email": email,
        "status": PARTICIPANT_STATUS_NAMES.get(int(participant.participantStatus()), "unknown"),
        "is_me": bool(participant.isCurrentUser()),
    }


def _recurrence_to_dict(rule: Any) -> dict[str, Any]:
    frequency = FREQUENCY_NAMES.get(int(rule.frequency()), "unknown")
    payload: dict[str, Any] = {"frequency": frequency, "interval": int(rule.interval())}

    days = rule.daysOfTheWeek() or []
    if days:
        payload["days_of_week"] = sorted(
            _EK_TO_PY_WEEKDAY[int(d.dayOfTheWeek())]
            for d in days
            if int(d.dayOfTheWeek()) in _EK_TO_PY_WEEKDAY
        )

    end = rule.recurrenceEnd()
    if end is not None:
        count = int(end.occurrenceCount())
        if count:
            payload["count"] = count
        elif end.endDate() is not None:
            payload["until"] = to_iso(_from_nsdate(end.endDate()))
    return payload


def _build_recurrence_rule(spec: RecurrenceSpec) -> Any:
    EventKit, _ = _frameworks()

    end = None
    if spec.count is not None:
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithOccurrenceCount_(spec.count)
    elif spec.until is not None:
        end = EventKit.EKRecurrenceEnd.recurrenceEndWithEndDate_(_to_nsdate(spec.until))

    frequency = FREQUENCY_VALUES[spec.frequency]

    if spec.days_of_week:
        days = [
            EventKit.EKRecurrenceDayOfWeek.dayOfWeek_(_PY_TO_EK_WEEKDAY[day])
            for day in spec.days_of_week
        ]
        return EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_daysOfTheWeek_daysOfTheMonth_monthsOfTheYear_weeksOfTheYear_daysOfTheYear_setPositions_end_(
            frequency, spec.interval, days, None, None, None, None, None, end
        )
    return EventKit.EKRecurrenceRule.alloc().initRecurrenceWithFrequency_interval_end_(
        frequency, spec.interval, end
    )
