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


@pytest.fixture
def client_for():
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
    assert state == {
        "online": True, "base_url": "http://127.0.0.1:5005", "recording": False,
        "elapsed": 0.0, "elapsed_clock": "00:00", "mic_level": 0.0,
        "system_level": 0.0, "disk_state": "ok", "disk_message": "",
    }


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


def test_live_page_disables_both_buttons_when_the_engine_is_down(client_for):
    html = client_for(MockEngine(status=(None, 0))).get("/meetings/live").get_data(as_text=True)
    assert html.count("disabled>") == 2
    assert "not answering" in html


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
    assert response.get_json() == {"error": "No microphone was found.",
                                   "reason": "audio_failed"}


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
