"""Lane 1 — the Meetings tab: the live view, its proxies, and start/stop.

NOTHING IN THIS FILE STARTS A REAL RECORDING. The audit fleet owns the engine's
trigger bus tonight, so every start/stop test drives a mocked engine client and
asserts on what this server did with the answer. The only tests that touch the
real engine are the `live_engine` ones at the bottom, and they are strictly
read-only: /api/record/status and /api/live with the recorder IDLE.
"""

from __future__ import annotations

try:  # real pytest when it is installed; the package's shim when it is not,
    import pytest  # so this suite runs on the interpreter DEMO.md documents.
except ModuleNotFoundError:  # pragma: no cover - depends on the environment
    from adjourn.tests import minipytest as pytest

from adjourn import meetings_ui
from adjourn.meetings_ui import (
    EngineClient,
    build_live_state,
    create_meetings_application,
)

IDLE = {
    "recording": False,
    "elapsed": 0.0,
    "levels": {"mic": 0.0, "system": 0.0},
    "disk": {"free": 45070831616, "state": "ok", "message": None},
}

RUNNING = {
    "recording": True,
    "elapsed": 754.2,
    "levels": {"mic": 0.42, "system": 0.11},
    "disk": {"free": 900000, "state": "low", "message": "Running low on disk space."},
}


class MockEngine:
    """A recorder that can be told what to say, and remembers what it was asked."""

    base_url = "http://127.0.0.1:5005"

    def __init__(self, status=None, live=None, start=None, stop=None):
        self.status = status if status is not None else (IDLE, 200)
        self.live = live if live is not None else ({"enabled": True, "turns": [], "seq": 0}, 200)
        self.start = start if start is not None else ({"id": "20260820-120000"}, 200)
        self.stop = stop if stop is not None else ({"id": "20260820-120000",
                                                    "status": "processing"}, 200)
        self.calls = []

    def get(self, endpoint, params=None):
        self.calls.append(("GET", endpoint, params))
        if endpoint == "/api/record/status":
            return self.status
        if endpoint == "/api/live":
            return self.live
        return None, 0

    def post(self, endpoint, payload=None):
        self.calls.append(("POST", endpoint, payload))
        if endpoint == "/api/record/start":
            return self.start
        if endpoint == "/api/record/stop":
            return self.stop
        return None, 0

    def meeting(self, meeting_id):
        return None, 404

    def posts_to(self, endpoint):
        return [call for call in self.calls if call[0] == "POST" and call[1] == endpoint]


class FakeLauncher:
    """Stands in for meetingscribe_source.ensure_engine_running.

    The record-start route now starts MeetingScribe when 5005 is not answering.
    That is a real `open -a` and a real write to state/engine_launch.json — two
    things a proxy unit test must not do, and the second of which would leave a
    file in the shipped tree after the suite. Every test here gets this instead;
    the two that care about launching set `.status` themselves.
    """

    def __init__(self, status="already_running"):
        self.status = status
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return {"status": self.status, "detail": f"fake launcher: {self.status}",
                "app": "MeetingScribe", "url": "http://127.0.0.1:5005",
                "waited_seconds": 0.0, "at": "2026-08-21T00:00:00+00:00"}


@pytest.fixture
def launcher(monkeypatch):
    from adjourn import meetingscribe_source

    fake = FakeLauncher()
    monkeypatch.setattr(meetingscribe_source, "ensure_engine_running", fake)
    return fake


@pytest.fixture
def client_for(launcher):
    def build(engine):
        return create_meetings_application(engine).test_client()
    return build


# --- the reload contract -----------------------------------------------------


def test_live_state_is_re_derived_from_the_engine_not_remembered():
    """Press Record, reload, and the timer is still counting — because the
    engine is asked, not a cookie."""
    state = build_live_state(MockEngine(status=(RUNNING, 200)))
    assert state["recording"] is True
    assert state["elapsed"] == 754.2
    assert state["elapsed_clock"] == "12:34"
    assert state["mic_level"] == 0.42
    assert state["disk_message"] == "Running low on disk space."


def test_live_state_of_an_idle_engine():
    state = build_live_state(MockEngine())
    transport = {key: value for key, value in state.items() if key != "engine"}
    # `track_names` is the two configured speaker names, resolved server-side so
    # a caption never falls back to the recorder's own "You"/"Them".
    assert transport.pop("track_names") == {"mic": "Sharique", "system": "Guest"}
    assert transport == {
        "online": True, "base_url": "http://127.0.0.1:5005", "recording": False,
        "elapsed": 0.0, "elapsed_clock": "00:00", "mic_level": 0.0,
        "system_level": 0.0, "disk_state": "ok", "disk_message": "",
    }
    # `engine` is the auto-launch pill, added alongside the transport rather than
    # inside it: the transport is the recorder's state and the pill is the app's
    # state, and a Record button that is disabled because the engine is closed
    # needs to say which of the two it is.
    assert state["engine"]["state"] == "up"
    assert state["engine"]["can_launch"] is False


def test_engine_pill_reports_down_when_the_engine_is_unreachable():
    """A closed engine says so, and says what pressing Start will do about it."""
    state = build_live_state(MockEngine(status=(None, 0)))
    pill = state["engine"]
    assert pill["state"] in {"down", "timed_out", "failed"}
    assert pill["can_launch"] is True
    # Never an empty pill: with no recorded attempt it explains the affordance,
    # with one it reports what that attempt did.
    assert pill["detail"]
    assert pill["headline"]


def test_engine_pill_reads_a_recorded_launch_attempt():
    """A launch that timed out is reported as a timeout, in the app's own words."""
    from adjourn import meetingscribe_source

    original = meetingscribe_source.read_engine_launch_state
    meetingscribe_source.read_engine_launch_state = lambda: {
        "status": "timed_out",
        "detail": "MeetingScribe was launched but did not answer within 30s",
        "waited_seconds": 30.0,
        "at": "2026-08-21T02:00:00+00:00",
    }
    try:
        pill = build_live_state(MockEngine(status=(None, 0)))["engine"]
    finally:
        meetingscribe_source.read_engine_launch_state = original
    assert pill["state"] == "timed_out"
    assert pill["headline"] == "launch timed out"
    assert "did not answer" in pill["detail"]
    assert pill["waited"] == 30.0


def test_a_reachable_engine_overrides_a_stale_timeout():
    """A launch that timed out and an engine answering now are not a
    contradiction — they are a slow start, and the pill must not cry wolf."""
    from adjourn import meetingscribe_source

    original = meetingscribe_source.read_engine_launch_state
    meetingscribe_source.read_engine_launch_state = lambda: {
        "status": "timed_out", "detail": "did not answer within 30s",
    }
    try:
        pill = build_live_state(MockEngine())["engine"]
    finally:
        meetingscribe_source.read_engine_launch_state = original
    assert pill["state"] == "up"
    assert pill["can_launch"] is False


def test_live_state_of_an_unreachable_engine_is_offline_not_a_crash():
    state = build_live_state(MockEngine(status=(None, 0)))
    assert state["online"] is False
    assert state["recording"] is False
    assert state["elapsed_clock"] == "00:00"


def test_live_state_survives_an_engine_that_omits_levels_and_disk():
    state = build_live_state(MockEngine(status=({"recording": True}, 200)))
    assert state["recording"] is True
    assert state["mic_level"] == 0.0
    assert state["disk_message"] == ""


def test_live_page_renders_a_running_recorder_as_running(client_for):
    html = client_for(MockEngine(status=(RUNNING, 200))).get("/meetings/live").get_data(as_text=True)
    assert 'data-recording="true"' in html
    assert 'data-status="recording" data-transport' in html
    assert ">12:34<" in html
    assert "Running low on disk space." in html


def test_live_page_disables_the_right_button_for_each_state(client_for):
    idle = client_for(MockEngine()).get("/meetings/live").get_data(as_text=True)
    assert "data-start\n              >" in idle or "data-start >" in idle
    assert "data-stop\n              disabled>" in idle or "data-stop disabled>" in idle

    running = client_for(MockEngine(status=(RUNNING, 200))).get("/meetings/live").get_data(as_text=True)
    assert "data-start\n              disabled>" in running or "data-start disabled>" in running


def test_a_closed_engine_leaves_start_pressable_because_pressing_it_starts_the_engine(client_for):
    """Start used to be disabled over a closed engine. It is the one button that
    should not be: the proxy launches the recorder in the background before it
    forwards, so pressing Start is what starts it. Stop stays disabled — there is
    nothing to stop.

    THE LAUNCH RECORD IS STUBBED, and it has to be. `read_engine_launch_state`
    reads the SHIPPED tree's `adjourn/state/engine_launch.json` — a real file
    that a `--watch` run or a rehearsal leaves behind, and whose contents change
    the pill this page renders. Without the stub this test passed on a clean
    machine and failed on one that had rehearsed, which is the worst kind of
    red: it appears the morning of a demo and looks like a regression. The
    subject here is a closed engine nobody has pressed Start on yet, so the
    honest fixture for that file is an empty dict.
    """
    from adjourn import meetingscribe_source

    original = meetingscribe_source.read_engine_launch_state
    meetingscribe_source.read_engine_launch_state = lambda: {}
    try:
        html = client_for(MockEngine(status=(None, 0))).get("/meetings/live").get_data(as_text=True)
    finally:
        meetingscribe_source.read_engine_launch_state = original
    assert html.count("disabled>") == 1
    assert "data-stop\n              disabled>" in html or "data-stop" in html
    assert "not answering" in html
    # ...and the page says what pressing it will do, rather than telling the
    # presenter to go and start the engine by hand — WITHOUT naming a second
    # product, and while promising the background launch the brief requires.
    assert "Press Start recording" in html
    assert "in the background" in html
    assert "You stay on this page." in html
    assert "meetingscribe" not in html.lower()


# --- the proxies -------------------------------------------------------------


def test_record_status_proxy_passes_the_engine_through_verbatim(client_for):
    response = client_for(MockEngine()).get("/meetings/api/record/status")
    assert response.status_code == 200
    assert response.get_json() == IDLE


def test_live_proxy_forwards_since_as_an_integer(client_for):
    engine = MockEngine()
    client_for(engine).get("/meetings/api/live?since=42")
    assert ("GET", "/api/live", {"since": 42}) in engine.calls


def test_live_proxy_coerces_a_junk_since_rather_than_500ing(client_for):
    engine = MockEngine()
    response = client_for(engine).get("/meetings/api/live?since=drop-tables")
    assert response.status_code == 200
    assert ("GET", "/api/live", {"since": 0}) in engine.calls


def test_live_proxy_clamps_a_negative_since(client_for):
    engine = MockEngine()
    client_for(engine).get("/meetings/api/live?since=-9")
    assert ("GET", "/api/live", {"since": 0}) in engine.calls


def test_an_unreachable_engine_becomes_a_503_shaped_like_a_refusal(client_for):
    """One error shape for the JavaScript, whether the engine said no or was
    never there."""
    response = client_for(MockEngine(status=(None, 0))).get("/meetings/api/record/status")
    assert response.status_code == 503
    payload = response.get_json()
    assert payload["reason"] == "engine_unreachable"
    assert isinstance(payload["error"], str) and payload["error"]


def test_an_engine_answer_that_is_not_json_becomes_a_readable_refusal(client_for):
    """An HTML error page from the engine must not reach the browser as one."""
    response = client_for(MockEngine(status=(None, 500))).get("/meetings/api/record/status")
    assert response.status_code == 500
    assert response.get_json()["reason"] == "bad_response"


# --- start / stop, against a mocked engine -----------------------------------


def test_start_posts_to_the_engine_and_returns_its_answer(client_for):
    engine = MockEngine()
    response = client_for(engine).post("/meetings/api/record/start", json={})
    assert response.status_code == 200
    assert response.get_json()["id"] == "20260820-120000"
    assert engine.posts_to("/api/record/start") == [("POST", "/api/record/start", {})]


def test_start_forwards_expected_speakers_when_it_is_sane(client_for):
    engine = MockEngine()
    client_for(engine).post("/meetings/api/record/start", json={"expected_speakers": 3})
    assert engine.posts_to("/api/record/start")[0][2] == {"expected_speakers": 3}


@pytest.mark.parametrize("value", [0, -1, "three", None, 3.5, [3]])
def test_start_drops_a_nonsense_expected_speakers_rather_than_forwarding_it(client_for, value):
    engine = MockEngine()
    client_for(engine).post("/meetings/api/record/start", json={"expected_speakers": value})
    assert engine.posts_to("/api/record/start")[0][2] == {}


def test_start_passes_a_refusal_through_with_both_its_halves(client_for):
    """The engine ships a human sentence AND a stable code. Swallowing either
    is how a Record button ends up saying 'something went wrong' over a message
    that named the missing device."""
    refusal = ({"error": "No microphone was found.", "reason": "audio_failed"}, 500)
    response = client_for(MockEngine(start=refusal)).post("/meetings/api/record/start", json={})
    assert response.status_code == 500
    body = response.get_json()
    # Both halves, unchanged. `engine_launch` rides along beside them — the route
    # records the auto-launch attempt on every start — so this asserts the two
    # halves survive rather than that nothing else may ever be added, which is
    # the property the docstring is actually about.
    assert body["error"] == "No microphone was found."
    assert body["reason"] == "audio_failed"


def test_start_asks_for_the_engine_before_it_proxies(client_for, launcher):
    """A Record click with MeetingScribe closed used to be a bare connection
    error. The route starts it first — via `open`, so the app keeps its own
    microphone permission — and only then proxies."""
    engine = MockEngine()
    client_for(engine).post("/meetings/api/record/start", json={})
    assert launcher.calls == 1
    assert engine.posts_to("/api/record/start")


def test_start_refuses_rather_than_proxying_when_the_engine_never_came_up(client_for, launcher):
    launcher.status = "timed_out"
    engine = MockEngine()
    response = client_for(engine).post("/meetings/api/record/start", json={})
    assert response.status_code == 503
    assert response.get_json()["reason"] == "engine_timed_out"
    # And it did NOT pretend to start a recording against a dead engine.
    assert engine.posts_to("/api/record/start") == []


def test_stop_does_not_try_to_launch_anything(client_for, launcher):
    """Stopping a recording that is not running is not a reason to open an app."""
    client_for(MockEngine()).post("/meetings/api/record/stop", json={})
    assert launcher.calls == 0


def test_start_passes_a_disk_full_refusal_through_with_its_disk_payload(client_for):
    refusal = ({"error": "The disk is full.", "reason": "disk_full",
                "disk": {"state": "full", "free": 12}}, 507)
    response = client_for(MockEngine(start=refusal)).post("/meetings/api/record/start", json={})
    assert response.status_code == 507
    assert response.get_json()["disk"]["state"] == "full"


def test_start_on_an_already_recording_engine_returns_409_not_a_second_recording(client_for):
    refusal = ({"error": "Already recording", "reason": "already_recording"}, 409)
    engine = MockEngine(start=refusal)
    response = client_for(engine).post("/meetings/api/record/start", json={})
    assert response.status_code == 409
    assert response.get_json()["reason"] == "already_recording"
    assert len(engine.posts_to("/api/record/start")) == 1   # asked exactly once


def test_start_never_reports_success_when_the_engine_is_unreachable(client_for):
    """The one failure this module must not have: a Start button that pretends."""
    response = client_for(MockEngine(start=(None, 0))).post("/meetings/api/record/start", json={})
    assert response.status_code == 503
    assert response.get_json()["reason"] == "engine_unreachable"


def test_stop_posts_and_returns_the_finished_meeting(client_for):
    engine = MockEngine()
    response = client_for(engine).post("/meetings/api/record/stop", json={})
    assert response.status_code == 200
    assert response.get_json()["status"] == "processing"
    assert engine.posts_to("/api/record/stop") == [("POST", "/api/record/stop", {})]


def test_stop_when_nothing_is_recording_passes_the_409_through(client_for):
    engine = MockEngine(stop=({"error": "Not recording"}, 409))
    response = client_for(engine).post("/meetings/api/record/stop", json={})
    assert response.status_code == 409
    assert response.get_json()["error"] == "Not recording"


def test_start_and_stop_are_the_only_writes_this_module_can_make():
    assert set(meetings_ui.WRITE_ENDPOINTS) == {"/api/record/start", "/api/record/stop"}
    for endpoint in ("/api/shutdown", "/api/meetings/x/process",
                     "/api/meetings/x/summarize", "/api/record/note"):
        with pytest.raises(ValueError):
            EngineClient().post(endpoint)


def test_a_get_route_never_reaches_a_write_endpoint(client_for):
    engine = MockEngine()
    client = client_for(engine)
    client.get("/meetings/")
    client.get("/meetings/live")
    client.get("/meetings/api/record/status")
    client.get("/meetings/api/live?since=0")
    assert [call for call in engine.calls if call[0] == "POST"] == []


def test_start_and_stop_reject_a_GET(client_for):
    client = client_for(MockEngine())
    assert client.get("/meetings/api/record/start").status_code == 405
    assert client.get("/meetings/api/record/stop").status_code == 405


# --- the engine-DOWN render path, and the no-product-name contract -----------
#
# WHY THESE EXIST. Every earlier assertion about /meetings/live was taken with
# the engine UP, and the engine-down branch is exactly the one the demo walks
# into: a judge clicks Live before anyone has pressed Start. That branch was
# where the 500 lived, and it is where every remaining product-name string in
# this lane rendered — the engine pill's copy is DORMANT while :5005 answers.
# So: render against a port nothing is listening on, and assert both halves.


DEAD_PORT_URL = "http://127.0.0.1:5599"


@pytest.fixture
def dead_engine_client(launcher, monkeypatch):
    """A real EngineClient pointed at a port nothing is listening on.

    Not a MockEngine: the point is to exercise the transport failure itself —
    requests raising ConnectionError inside EngineClient._call — rather than a
    fake that politely returns (None, 0).
    """
    monkeypatch.setattr(meetings_ui.config, "meetingscribe_base_url",
                        lambda: DEAD_PORT_URL)
    return create_meetings_application(EngineClient(DEAD_PORT_URL, timeout=1)).test_client()


def test_live_renders_200_with_the_engine_down(dead_engine_client):
    """The headline requirement: capture is the broken tab, and a dead recorder
    is not an exception. Start/Stop, timer, both meters and the caption region
    are all on the page whether or not anything answers on 5005."""
    response = dead_engine_client.get("/meetings/live")
    assert response.status_code == 200

    html = response.get_data(as_text=True)
    assert "Start recording" in html
    assert "data-stop" in html
    assert "data-timer" in html
    assert ">00:00<" in html
    assert 'data-meter="mic"' in html and 'data-meter="system"' in html
    assert "data-captions" in html
    assert "OFFLINE" in html


def test_live_names_no_second_product_with_the_engine_down(dead_engine_client):
    """The brief's rule, asserted where it can actually break. The engine pill,
    the offline banner and the launch affordance are all rendered in this state
    and every one of them used to name the recorder's .app."""
    html = dead_engine_client.get("/meetings/live").get_data(as_text=True)
    assert "meetingscribe" not in html.lower()
    # ...and it still says something honest about the engine.
    assert "engine" in html.lower()


def test_the_librarys_offline_banner_names_no_second_product(dead_engine_client):
    """The library's offline banner is the same class of leak on the other page.

    Scoped to the CHROME on purpose. The rows themselves carry the engine's own
    `brief` text, which on this machine includes a recording whose one-line
    summary names the recorder — that is DATA, not copy, no template edit can
    reach it, and the presentation filter is what removes it (see the next test).
    """
    response = dead_engine_client.get("/meetings/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "The engine is not answering on" in html
    chrome = html.split('<div id="rows"')[0]
    assert "meetingscribe" not in chrome.lower()


def test_presentation_mode_is_what_clears_the_librarys_foreign_archive(
    dead_engine_client, monkeypatch
):
    """With the filter on and nothing listed, the library is Adjourn's meetings
    and no one else's — and the page is clean end to end.

    This is the assertion that pins the brief's "do not dump the entire history
    into the pitch library" line to something a test can fail on. The filter's
    ENABLEMENT is configuration, not this lane's; its BEHAVIOUR is here.

    A meeting that finished in the last hour may still appear (Stop must not
    vanish the row). That is not the foreign archive this test is pinning.
    """
    monkeypatch.setattr(meetings_ui, "presentation_mode_enabled", lambda: True)
    monkeypatch.setattr(meetings_ui, "presentation_meeting_ids", frozenset)
    response = dead_engine_client.get("/meetings/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "meetingscribe" not in html.lower()
    assert "Salient Interview" not in html


def test_live_renders_with_no_recorded_launch_attempt_at_all(dead_engine_client):
    """state/engine_launch.json does not exist until the first Start click, so
    the very first render of this page reads an empty dict. That is the load a
    judge actually performs, and it must not be the one that raises."""
    from adjourn import meetingscribe_source

    original = meetingscribe_source.read_engine_launch_state
    meetingscribe_source.read_engine_launch_state = lambda: {}
    try:
        response = dead_engine_client.get("/meetings/live")
    finally:
        meetingscribe_source.read_engine_launch_state = original
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Press Start recording" in html
    assert "meetingscribe" not in html.lower()


def test_live_survives_a_launch_state_file_that_is_junk(dead_engine_client):
    """A pill must never take the page down — not for a missing file, and not
    for a file holding something that is not a dict of strings."""
    from adjourn import meetingscribe_source

    original = meetingscribe_source.read_engine_launch_state
    for junk in ({"status": None, "detail": None}, {"detail": 12}, {}):
        meetingscribe_source.read_engine_launch_state = lambda payload=junk: payload
        try:
            assert dead_engine_client.get("/meetings/live").status_code == 200
        finally:
            meetingscribe_source.read_engine_launch_state = original


def test_the_launchers_own_words_are_scrubbed_before_they_reach_the_pill():
    """state/engine_launch.json is written by a module this lane does not own,
    and its `detail` names the .app it opened. The scrub is the boundary."""
    from adjourn import meetingscribe_source

    original = meetingscribe_source.read_engine_launch_state
    meetingscribe_source.read_engine_launch_state = lambda: {
        "status": "failed",
        "detail": "could not launch MeetingScribe: app not found. MeetingScribe is gone.",
    }
    try:
        pill = build_live_state(MockEngine(status=(None, 0)))["engine"]
    finally:
        meetingscribe_source.read_engine_launch_state = original
    assert "MeetingScribe" not in pill["detail"]
    assert "meetingscribe" not in pill["detail"].lower()
    assert "could not launch the recorder" in pill["detail"]
    # A sentence-initial hit keeps its capital rather than reading mid-sentence.
    assert "The recorder is gone." in pill["detail"]


def test_scrub_engine_name_leaves_everything_else_alone():
    assert meetings_ui.scrub_engine_name("") == ""
    assert meetings_ui.scrub_engine_name(None) == ""
    assert meetings_ui.scrub_engine_name(12) == ""
    assert meetings_ui.scrub_engine_name("nothing to do here") == "nothing to do here"


def test_a_failed_launch_refusal_carries_no_product_name(client_for, launcher):
    """The other path the launcher's words take to a browser: the JSON a failed
    Start hands the page, which the toast renders verbatim."""
    launcher.status = "failed"
    response = client_for(MockEngine(status=(None, 0))).post("/meetings/api/record/start")
    assert response.status_code == 503
    body = response.get_data(as_text=True)
    assert "meetingscribe" not in body.lower()
    payload = response.get_json()
    assert payload["error"]
    assert payload["reason"] == "engine_failed"


# --- the listening state -----------------------------------------------------


def test_an_idle_page_does_not_claim_to_be_listening(client_for):
    html = client_for(MockEngine()).get("/meetings/live").get_data(as_text=True)
    assert "data-listening hidden" in html
    assert 'data-listening-state="false"' in html
    assert "Nothing being said yet." in html


def test_a_recording_page_says_it_is_listening_before_any_caption_lands(client_for):
    """The gap between Start and the first sentence is where this tab looks
    broken. It is server-rendered, so a reload mid-meeting shows it too."""
    html = client_for(MockEngine(status=(RUNNING, 200))).get("/meetings/live").get_data(as_text=True)
    assert 'data-listening-state="true"' in html
    assert "data-listening hidden" not in html
    assert "listening" in html
    assert "Listening. Nothing said yet." in html
    # The transport still carries the other half of the claim.
    assert 'data-status="recording" data-transport' in html


# --- against the real engine, READ-ONLY, recorder left alone -----------------


def engine_is_up() -> bool:
    payload, status = EngineClient().get("/api/record/status")
    return status == 200 and isinstance(payload, dict)


live_engine = pytest.mark.skipif(not engine_is_up(),
                                 reason="MeetingScribe is not running on 5005")


@live_engine
def test_live_record_status_proxy_against_the_idle_engine():
    """The proxy path, end to end, WITHOUT starting anything.

    If this ever fails because `recording` is true, the fixture is wrong — the
    fleet is recording — not this code. Asserted as a skip rather than a
    failure for exactly that reason.
    """
    client = create_meetings_application().test_client()
    response = client.get("/meetings/api/record/status")
    assert response.status_code == 200

    payload = response.get_json()
    assert set(["recording", "elapsed", "levels", "disk"]) <= set(payload)
    assert isinstance(payload["levels"], dict)
    assert {"mic", "system"} <= set(payload["levels"])

    if payload["recording"]:
        pytest.skip("something else is recording; the idle assertions do not apply")
    assert payload["elapsed"] == 0.0
    assert payload["levels"]["mic"] == 0.0


@live_engine
def test_live_page_against_the_real_engine_renders_idle_and_offers_start():
    client = create_meetings_application().test_client()
    html = client.get("/meetings/live").get_data(as_text=True)
    assert "ENGINE" in html and "not answering" not in html
    if 'data-recording="true"' in html:
        pytest.skip("something else is recording")
    assert 'data-status="idle" data-transport' in html
    assert html.count("disabled>") == 1        # only Stop is disabled


@live_engine
def test_live_caption_proxy_against_the_real_engine():
    client = create_meetings_application().test_client()
    response = client.get("/meetings/api/live?since=0")
    assert response.status_code == 200
    payload = response.get_json()
    assert "enabled" in payload and isinstance(payload.get("turns"), list)

    # `since` past the head must return nothing — the contract the poller rides.
    head = payload.get("seq", 0)
    tail = client.get(f"/meetings/api/live?since={head + 1000}").get_json()
    assert tail["turns"] == []


if __name__ == "__main__":  # pragma: no cover
    from adjourn.tests.minipytest import main
    raise SystemExit(main(globals(), "Meetings tab — live state, proxies, start/stop"))
