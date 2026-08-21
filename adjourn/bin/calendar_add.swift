// Write a calendar hold into the user's own Calendar.app, via EventKit.
//
// The counterpart to MeetingScribe's tools/calendar_events.swift, which READS
// today's events. This one WRITES: Adjourn turns "I'll have the cache cutover
// done by Friday" into a real block of time on the Mac that heard the sentence.
// No OAuth, no Google Calendar API, no network — the whole point of the
// local-first pitch is that the deadline never leaves the machine.
//
// Commands
//   probe                         -> {"authorization":"fullAccess","canWrite":true,
//                                     "defaultCalendar":"Home","writableCalendars":[...]}
//                                    Never prompts. Safe to call on every run.
//   add    --title T --start EPOCH --end EPOCH
//          [--notes N] [--calendar NAME] [--alarm-minutes 60] [--url U]
//                                 -> {"eventIdentifier":"...","calendar":"Home",...}
//   remove --id EVENT_IDENTIFIER  -> {"removed":true,"eventIdentifier":"..."}
//
// Events are created ATTENDEE-LESS on purpose: an attendee is an invitation, and
// an invitation is an irreversible message to another human. A hold is a note to
// self, so undo (remove) is a genuine undo.
//
// Build:  swiftc -O adjourn/bin/calendar_add.swift -o adjourn/bin/calendar_add
//         (calendar_hold_executor.ensure_calendar_binary() does this on demand
//          and caches the result, rebuilding only when this source is newer.)
//
// Exit codes: 0 ok · 3 calendar access denied/not granted · 4 bad arguments
//             · 5 EventKit failure.
//
// TCC: the first `add` shows the one-time macOS Calendar permission prompt,
// attributed to whatever launched the process (Terminal, the agent, or a
// packaged app). Run headless it may simply be denied — that is expected, and
// the Python side degrades to "the .ics is on disk" rather than retrying.

import EventKit
import Foundation

// ---- output shapes ---------------------------------------------------------

struct AddOut: Codable {
    let eventIdentifier: String
    let calendar: String
    let title: String
    let start: Double
    let end: Double
    let alarmMinutesBefore: Double
}

struct RemoveOut: Codable {
    let removed: Bool
    let eventIdentifier: String
}

struct ProbeOut: Codable {
    let authorization: String
    let canWrite: Bool
    let canRead: Bool
    let defaultCalendar: String?
    let writableCalendars: [String]
}

// ---- plumbing --------------------------------------------------------------

func die(_ message: String, code: Int32 = 3) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(code)
}

func emit<T: Encodable>(_ value: T) {
    guard let data = try? JSONEncoder().encode(value) else {
        die("could not encode helper output", code: 5)
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

func authorizationName(_ status: EKAuthorizationStatus) -> String {
    switch status {
    case .notDetermined: return "notDetermined"
    case .restricted: return "restricted"
    case .denied: return "denied"
    case .fullAccess: return "fullAccess"
    case .writeOnly: return "writeOnly"
    @unknown default: return "unknown"
    }
}

// ---- arguments -------------------------------------------------------------

var arguments = Array(CommandLine.arguments.dropFirst())
guard let command = arguments.first else {
    die("usage: calendar_add probe | add --title T --start EPOCH --end EPOCH | remove --id ID",
        code: 4)
}
arguments = Array(arguments.dropFirst())

var options: [String: String] = [:]
var index = 0
while index < arguments.count {
    let flag = arguments[index]
    guard flag.hasPrefix("--") else { die("unexpected argument \(flag)", code: 4) }
    guard index + 1 < arguments.count else { die("missing value for \(flag)", code: 4) }
    options[String(flag.dropFirst(2))] = arguments[index + 1]
    index += 2
}

let store = EKEventStore()

/// Ensure we may touch the calendar, prompting ONCE if macOS has not decided yet.
/// `requiresRead` is true for remove (we must look the event up first); creating
/// an event is possible with write-only access.
func ensureCalendarAccess(requiresRead: Bool) {
    let status = EKEventStore.authorizationStatus(for: .event)
    switch status {
    case .fullAccess:
        return
    case .writeOnly:
        if requiresRead {
            die("calendar access is write-only — removing an event needs full access", code: 3)
        }
        return
    case .denied, .restricted:
        die("calendar access denied — allow it in System Settings › Privacy & Security › Calendars",
            code: 3)
    default:
        let semaphore = DispatchSemaphore(value: 0)
        var granted = false
        var failure: String?
        store.requestFullAccessToEvents { ok, error in
            granted = ok
            failure = error?.localizedDescription
            semaphore.signal()
        }
        if semaphore.wait(timeout: .now() + 60) == .timedOut {
            die("calendar permission prompt timed out", code: 3)
        }
        guard granted else {
            die("calendar access not granted\(failure.map { " (\($0))" } ?? "")", code: 3)
        }
    }
}

func writableCalendarTitles() -> [String] {
    store.calendars(for: .event)
        .filter { $0.allowsContentModifications && !$0.isSubscribed }
        .map { $0.title }
}

// ---- commands --------------------------------------------------------------

switch command {

case "probe":
    // Deliberately never prompts: the Python side calls this on every run to
    // decide live-vs-sim, and a permission dialog per action would be hostile.
    let status = EKEventStore.authorizationStatus(for: .event)
    let canWrite = (status == .fullAccess || status == .writeOnly)
    let canRead = (status == .fullAccess)
    emit(ProbeOut(
        authorization: authorizationName(status),
        canWrite: canWrite,
        canRead: canRead,
        defaultCalendar: canWrite ? store.defaultCalendarForNewEvents?.title : nil,
        writableCalendars: canWrite ? writableCalendarTitles() : []
    ))

case "add":
    // Arguments are validated BEFORE access is requested: a malformed command
    // must never be the reason a permission dialog appears on someone's screen.
    guard let title = options["title"],
          let startText = options["start"], let start = Double(startText),
          let endText = options["end"], let end = Double(endText) else {
        die("add requires --title T --start EPOCH --end EPOCH", code: 4)
    }
    guard end > start else { die("--end must be after --start", code: 4) }
    ensureCalendarAccess(requiresRead: false)

    var target = store.defaultCalendarForNewEvents
    if let wanted = options["calendar"] {
        let match = store.calendars(for: .event).first {
            $0.title == wanted && $0.allowsContentModifications
        }
        if let match = match { target = match }
    }
    guard let calendar = target else {
        die("no writable calendar available for new events", code: 5)
    }

    let event = EKEvent(eventStore: store)
    event.title = title
    event.startDate = Date(timeIntervalSince1970: start)
    event.endDate = Date(timeIntervalSince1970: end)
    event.calendar = calendar
    if let notes = options["notes"] { event.notes = notes }
    if let link = options["url"], let parsed = URL(string: link) { event.url = parsed }

    let alarmMinutes = Double(options["alarm-minutes"] ?? "60") ?? 60
    if alarmMinutes > 0 {
        event.addAlarm(EKAlarm(relativeOffset: -alarmMinutes * 60))
    }

    do {
        try store.save(event, span: .thisEvent, commit: true)
    } catch {
        die("could not save event: \(error.localizedDescription)", code: 5)
    }

    emit(AddOut(
        eventIdentifier: event.eventIdentifier ?? "",
        calendar: calendar.title,
        title: title,
        start: start,
        end: end,
        alarmMinutesBefore: alarmMinutes
    ))

case "remove":
    ensureCalendarAccess(requiresRead: true)
    guard let identifier = options["id"], !identifier.isEmpty else {
        die("remove requires --id EVENT_IDENTIFIER", code: 4)
    }
    // An event that is already gone is a successful undo, not an error: the
    // board's undo button must be idempotent.
    guard let event = store.event(withIdentifier: identifier) else {
        emit(RemoveOut(removed: false, eventIdentifier: identifier))
        exit(0)
    }
    do {
        try store.remove(event, span: .thisEvent, commit: true)
    } catch {
        die("could not remove event: \(error.localizedDescription)", code: 5)
    }
    emit(RemoveOut(removed: true, eventIdentifier: identifier))

default:
    die("unknown command \(command) — expected probe, add, or remove", code: 4)
}
