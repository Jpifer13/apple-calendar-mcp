# apple-calendar-mcp

An [MCP](https://modelcontextprotocol.io) server that lets Claude read and manage
**Apple Calendar** on macOS.

It talks to the system calendar database through **EventKit**, the same framework
Calendar.app uses — not AppleScript. That means real event identifiers, proper
recurrence rules, attendee and alarm data, and fast queries. Every account you
have configured in Calendar.app (iCloud, Google, Exchange, subscribed calendars)
is visible automatically.

Nothing leaves your Mac: the server runs locally over stdio and makes no network
calls of its own.

## Requirements

- macOS (EventKit is macOS-only)
- Python 3.11+
- Calendar access granted to whichever app launches the server

## Install

```bash
git clone https://github.com/Jpifer13/apple-calendar-mcp.git
cd apple-calendar-mcp
poetry install
```

Verify the install and your calendar permissions:

```bash
poetry run python -c "
from apple_calendar_mcp.store import CalendarStore
print(CalendarStore().access_report())
"
```

The first run triggers the macOS calendar permission prompt. Click **Allow**.

## Connect it to Claude

### Claude Code

```bash
claude mcp add apple-calendar --scope user -- "$(pwd)/.venv/bin/apple-calendar-mcp"
```

`--scope user` makes the server available in every project rather than only the
directory it was added from. Drop the flag to limit it to one project.

### Claude Desktop

Add this to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "apple-calendar": {
      "command": "/absolute/path/to/apple-calendar-mcp/.venv/bin/apple-calendar-mcp"
    }
  }
}
```

Use an absolute path to the executable inside `.venv`, then restart the client.

## Tools

| Tool | What it does |
| --- | --- |
| `check_calendar_access` | Report permission state and how to fix it. Start here when something fails. |
| `list_calendars` | Every calendar with its account, color, writability, and which is the default. |
| `list_events` | Events overlapping a time range, optionally filtered by calendar or text. |
| `search_events` | Text search over title, location, and notes across a wide window. |
| `get_event` | Full detail for one event: attendees, alarms, notes, organizer. |
| `find_free_slots` | Open blocks of a given length, respecting working hours and busy/free status. |
| `create_event` | Create a timed or all-day event, with alarms and repeat rules. |
| `update_event` | Change any field; move between calendars; edit one occurrence or a whole series. |
| `delete_event` | Delete one occurrence or an occurrence and everything after it. |

Read-only tools are annotated as such, and `update_event` / `delete_event` are
flagged destructive, so clients can apply their own confirmation rules.

## Date formats

Anywhere a time is accepted:

| Form | Example |
| --- | --- |
| ISO 8601 | `2026-03-04T14:30`, `2026-03-04T14:30:00-08:00`, `2026-03-04T22:30Z` |
| Date only | `2026-03-04` (local midnight; as a range end it covers the whole day) |
| Keywords | `now`, `today`, `tomorrow`, `yesterday` |
| Weekday | `friday` (the *next* Friday) |
| Offsets | `+90m`, `+2h`, `-1d`, `+3w` |

Values without a timezone are interpreted in your Mac's local timezone, which is
resolved as a real IANA zone so ranges that cross a daylight-saving boundary keep
the wall-clock time you asked for.

## Things worth knowing

**All-day events end on an inclusive last day.** `start: "2026-03-10"`,
`end: "2026-03-12"` is a three-day event, matching how people describe them and
how EventKit stores them.

**Repeating events share one identifier.** Pass `occurrence_start` to target a
specific occurrence, and `span` to choose the reach of an edit:

- `span: "this"` — only that occurrence (EventKit detaches it from the series)
- `span: "future"` — that occurrence and every later one

**Free/busy rules.** `find_free_slots` ignores canceled events and anything
marked *free*. All-day events are skipped unless you pass `include_all_day`,
because Calendar.app saves them as "busy" and a single holiday would otherwise
swallow an entire day of candidate slots.

**Attendees are read-only.** EventKit exposes invitees but does not allow adding
them, so `create_event` cannot send invitations. Create the event here, then add
people in Calendar.app.

**Read-only calendars.** Subscribed calendars, holiday calendars, and the
Birthdays calendar reject writes; the server says which calendar refused.

## Example prompts

- "What's on my calendar this week?"
- "Find me a 45-minute slot on Tuesday or Wednesday afternoon."
- "Schedule a 1:1 with Sam every other Tuesday at 10am for the next 8 weeks."
- "Move tomorrow's standup to 9:30 and add a 10-minute reminder."
- "Delete just the Thanksgiving occurrence of the weekly sync."

## Troubleshooting

**"Calendar access was denied"** — open System Settings → Privacy & Security →
Calendars and enable access for the app that launches the server (Terminal,
iTerm, or Claude). Restart that app afterwards; macOS only re-reads the
permission at launch.

**"Only write access was granted"** — macOS 14+ can grant write-only access,
which cannot list events. Toggle the app to full access in the same pane.

**No prompt appears** — the prompt is attributed to the *parent* app, so it may
be behind another window, or already answered for that app in the past.

## Development

```bash
poetry run pytest
```

The test suite covers the pure logic — date parsing, DST handling, recurrence
normalization, and free/busy interval math — so it runs without touching your
real calendar.

Layout:

```
src/apple_calendar_mcp/
  server.py        MCP tool definitions
  store.py         EventKit bridge (the only module importing macOS frameworks)
  availability.py  free/busy interval math
  recurrence.py    repeat-rule normalization
  datetimes.py     date parsing and timezone handling
```

## License

MIT
