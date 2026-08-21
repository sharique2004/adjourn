/* The Meetings tab — the only client-side logic there is.
 *
 * Two pages share this file and each uses one half of it:
 *
 *   library   debounced search that asks the SERVER to re-render the rows.
 *             There is no row markup here, for board.js's reason: the rows come
 *             from the same Jinja macro that rendered the page, so what you curl
 *             and what you watch can never disagree.
 *
 *   live      poll the caption stream at 1s, the recorder at 250ms while it is
 *             running, and send start/stop through this server's proxy.
 *
 * The live page holds NO recording state of its own. Every tick, the transport
 * is redrawn from what /api/record/status just said — so a reload mid-recording
 * lands on a page that is already counting, and a recording stopped from the
 * native HUD turns this page's button back to Start without anyone telling it.
 * The captions are the one thing kept locally, because they are append-only and
 * `since` exists precisely so they are not re-sent.
 */

(function () {
  "use strict";

  var SEARCH_DEBOUNCE_MS = 220;
  var LIVE_POLL_MS = 1000;
  var STATUS_POLL_RECORDING_MS = 250;
  var STATUS_POLL_IDLE_MS = 2000;
  var TOAST_MS = 5200;
  /* Enough to read a long meeting back; past this the oldest scroll out of the
   * DOM. A four-hour meeting is tens of thousands of turns and the browser
   * should not be holding all of them to show the last screen. */
  var MAX_CAPTIONS = 600;

  var body = document.body;
  var view = body.dataset.view || "";
  var toastElement = document.getElementById("toast");
  var toastTimer = null;

  /* --- shared ------------------------------------------------------------- */

  function showToast(message, tone) {
    if (!toastElement) {
      return;
    }
    toastElement.textContent = message;
    toastElement.dataset.tone = tone || "";
    toastElement.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      toastElement.hidden = true;
    }, TOAST_MS);
  }

  function getJSON(url) {
    return fetch(url, {
      headers: { Accept: "application/json" },
      cache: "no-store"
    }).then(function (response) {
      return response.json().then(function (payload) {
        return { ok: response.ok, status: response.status, payload: payload };
      });
    });
  }

  function postJSON(url, bodyObject) {
    return fetch(url, {
      method: "POST",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      cache: "no-store",
      body: JSON.stringify(bodyObject || {})
    }).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (payload) {
        return { ok: response.ok, status: response.status, payload: payload };
      });
    });
  }

  function formatClock(seconds) {
    var total = Math.max(0, Math.floor(Number(seconds) || 0));
    var pad = function (n) { return n < 10 ? "0" + n : String(n); };
    if (total >= 3600) {
      return Math.floor(total / 3600) + ":" + pad(Math.floor((total % 3600) / 60)) + ":" + pad(total % 60);
    }
    return pad(Math.floor(total / 60)) + ":" + pad(total % 60);
  }

  /* --- library: search ----------------------------------------------------- */

  function setUpSearch() {
    var form = document.querySelector("[data-search]");
    var field = form && form.querySelector(".searchfield");
    var rowsHost = document.querySelector("[data-rows]");
    var countHost = document.querySelector("[data-row-count]");
    var totalHost = document.querySelector("[data-total-count]");
    var hoursHost = document.querySelector("[data-total-hours]");
    var busyHost = document.querySelector("[data-searching]");
    if (!form || !field || !rowsHost) {
      return;
    }

    var timer = null;
    var inFlight = 0;

    function render(query) {
      /* Sequence number rather than an AbortController: two searches typed
       * quickly can land out of order, and the only answer that may paint is
       * the newest one. Aborting would work too; this also covers the case
       * where the older request already made it to the wire. */
      inFlight += 1;
      var ticket = inFlight;
      if (busyHost) {
        busyHost.hidden = false;
      }
      return getJSON("/meetings/api/rows?q=" + encodeURIComponent(query))
        .then(function (result) {
          if (ticket !== inFlight) {
            return;
          }
          if (!result.ok || !result.payload) {
            showToast("Search could not reach the engine.", "bad");
            return;
          }
          rowsHost.innerHTML = result.payload.html || "";
          if (countHost) {
            countHost.textContent = result.payload.total;
          }
          if (totalHost) {
            totalHost.textContent = result.payload.total +
              (result.payload.total === 1 ? " recording" : " recordings");
          }
          if (hoursHost) {
            /* The server labels this, so a short library reads "0m" rather
             * than "0.0 hours". `hours` is still sent for older callers. */
            hoursHost.textContent = result.payload.total_label ||
              (result.payload.hours + " hours");
          }
          /* Keep the address bar honest: the URL you are looking at is the URL
           * that reproduces what you are looking at. */
          var next = query
            ? "/meetings/?q=" + encodeURIComponent(query)
            : "/meetings/";
          window.history.replaceState(null, "", next);
        })
        .catch(function () {
          if (ticket === inFlight) {
            showToast("Search could not reach the engine.", "bad");
          }
        })
        .then(function () {
          if (ticket === inFlight && busyHost) {
            busyHost.hidden = true;
          }
        });
    }

    field.addEventListener("input", function () {
      window.clearTimeout(timer);
      timer = window.setTimeout(function () {
        render(field.value.trim());
      }, SEARCH_DEBOUNCE_MS);
    });

    form.addEventListener("submit", function (event) {
      /* The form still works without JavaScript — this only saves the reload. */
      event.preventDefault();
      window.clearTimeout(timer);
      render(field.value.trim());
    });
  }

  /* --- live: transport ----------------------------------------------------- */

  function setUpLive() {
    var host = document.querySelector("[data-live]");
    if (!host) {
      return;
    }

    var transport = host.querySelector("[data-transport]");
    var timerHost = host.querySelector("[data-timer]");
    var stateHost = host.querySelector("[data-state-label]");
    var startButton = host.querySelector("[data-start]");
    var stopButton = host.querySelector("[data-stop]");
    var captionsHost = host.querySelector("[data-captions]");
    var emptyHost = host.querySelector("[data-captions-empty]");
    var captionCount = host.querySelector("[data-caption-count]");
    var diskHost = host.querySelector("[data-disk]");
    var listeningChip = host.querySelector("[data-listening]");
    var emptyLine = host.querySelector("[data-captions-empty-line]");

    var micName = host.dataset.micName || "You";
    var systemName = host.dataset.systemName || "Them";

    var engineHost = host.querySelector("[data-engine]");
    var engineHeadline = host.querySelector("[data-engine-headline]");
    var engineDetail = host.querySelector("[data-engine-detail]");

    var since = 0;
    var captions = 0;
    var busy = false;             /* a start/stop is in flight */
    var recording = host.dataset.recording === "true";
    var statusTimer = null;
    /* The first poll BACKFILLS whatever the engine's caption buffer already
     * holds — which is the whole point on a page reloaded mid-recording. That
     * batch must not scroll: landing a fresh visitor at the bottom of the
     * backlog puts the transport controls off-screen before they have seen
     * them. Only captions that arrive after the page has settled follow. */
    var backfilling = true;

    /* NO STALE CAPTIONS ON AN IDLE ENGINE.
     *
     * The engine's caption buffer survives the recording that filled it, and
     * `since` starting at 0 meant the very first poll backfilled the WHOLE of it
     * — so opening the Live tab before anyone had spoken showed six lines of the
     * previous rehearsal's transcript under a transport reading IDLE 00:00. On a
     * projector that is the demo's own script, garbled, before the demo starts.
     *
     * The backfill exists for a page reloaded MID-recording, which is exactly the
     * recording=true case. When the page opens on an idle engine we still take
     * the first payload's `seq` — that is how we skip the backlog — and render
     * nothing from it. */
    var skipFirstBatch = !recording;

    function setBusy(next) {
      busy = next;
      applyButtons();
    }

    function applyButtons() {
      if (startButton) {
        startButton.disabled = busy || recording;
      }
      if (stopButton) {
        stopButton.disabled = busy || !recording;
      }
    }

    /* --- the recorder ---------------------------------------------------- */

    function applyStatus(snapshot) {
      var wasRecording = recording;
      recording = !!snapshot.recording;

      if (timerHost) {
        timerHost.textContent = formatClock(snapshot.elapsed);
      }
      if (transport) {
        transport.dataset.status = recording ? "recording" : "idle";
      }
      if (stateHost) {
        stateHost.textContent = recording ? "RECORDING" : "IDLE";
      }
      applyListening();

      var levels = snapshot.levels || {};
      setMeter("mic", levels.mic);
      setMeter("system", levels.system);

      var disk = snapshot.disk || {};
      if (diskHost) {
        if (disk.message) {
          diskHost.textContent = disk.message;
          diskHost.dataset.state = disk.state || "";
          diskHost.hidden = false;
        } else {
          diskHost.hidden = true;
        }
      }

      applyButtons();

      if (wasRecording !== recording) {
        /* The poll cadence follows the recorder, not the other way round:
         * 250ms of meters matters while a meeting runs and is pure noise while
         * nothing is being captured. */
        schedulePolling();
        if (recording && !wasRecording) {
          /* A NEW SESSION STARTS FROM AN EMPTY PANE, however it was started.
           * This used to live in the Start button's handler alone, so a session
           * begun from the recorder's own HUD reset `since` and left the
           * previous meeting's rows sitting above the new ones. The transition is
           * the honest place for it: it fires for the button and the HUD alike. */
          clearCaptions();
        }
      }
    }

    /* THE LISTENING STATE, in the two places an eye lands. The transport alone
     * is not enough from the back of a room: a green RECORDING label over a pane
     * reading "nothing being said yet" is indistinguishable from a dead
     * transport, which is the one impression this tab must never give. Driven
     * from `recording` — the recorder's own answer — so it is equally true for a
     * session started from this button and one started from the recorder's HUD. */
    function applyListening() {
      if (listeningChip) {
        listeningChip.hidden = !recording;
      }
      if (captionsHost) {
        captionsHost.dataset.listeningState = recording ? "true" : "false";
      }
      if (emptyLine) {
        emptyLine.textContent = recording
          ? "Listening. Nothing said yet."
          : "Nothing being said yet.";
      }
    }

    function clearCaptions() {
      since = 0;
      skipFirstBatch = false;   /* this session's buffer is the one we want */
      captions = 0;
      if (!captionsHost) {
        return;
      }
      captionsHost.innerHTML = "";
      if (captionCount) {
        captionCount.textContent = "0";
      }
      if (emptyHost) {
        captionsHost.appendChild(emptyHost);
        emptyHost.hidden = false;
      }
      applyListening();
    }

    /* --- the engine pill --------------------------------------------------- */

    function applyEngine(state) {
      if (!engineHost || !state) {
        return;
      }
      engineHost.dataset.state = state.state || "down";
      engineHost.hidden = state.state === "up";
      if (engineHeadline) {
        engineHeadline.textContent = state.headline || "";
      }
      if (engineDetail) {
        engineDetail.textContent = state.detail || "";
      }
    }

    function pollEngine() {
      return getJSON("/meetings/api/engine")
        .then(function (result) {
          if (result.ok && result.payload) {
            applyEngine(result.payload.engine);
          }
        })
        .catch(function () {
          /* The board itself is restarting; the pill keeps what it had. */
        });
    }

    function setMeter(name, value) {
      var fill = host.querySelector('[data-meter-fill="' + name + '"]');
      var label = host.querySelector('[data-meter-value="' + name + '"]');
      var percent = Math.max(0, Math.min(100, Math.round((Number(value) || 0) * 100)));
      if (fill) {
        fill.style.width = percent + "%";
      }
      if (label) {
        label.textContent = percent;
      }
    }

    function pollStatus() {
      var wasOnline = host.dataset.online === "true";
      return getJSON("/meetings/api/record/status")
        .then(function (result) {
          var online = !!(result.ok && result.payload);
          host.dataset.online = online ? "true" : "false";
          if (online) {
            applyStatus(result.payload);
          }
          /* The pill only has to be re-read when reachability CHANGED — an
           * engine that has been up for ten minutes has nothing new to say, and
           * this poll runs four times a second while recording. */
          if (online !== wasOnline) {
            pollEngine();
          }
        })
        .catch(function () {
          host.dataset.online = "false";
          if (wasOnline) {
            pollEngine();
          }
        });
    }

    function schedulePolling() {
      window.clearInterval(statusTimer);
      statusTimer = window.setInterval(
        pollStatus,
        recording ? STATUS_POLL_RECORDING_MS : STATUS_POLL_IDLE_MS
      );
    }

    /* --- the captions ----------------------------------------------------- */

    function pollCaptions() {
      return getJSON("/meetings/api/live?since=" + since)
        .then(function (result) {
          if (!result.ok || !result.payload) {
            return;
          }
          var payload = result.payload;
          /* A SEQUENCE THAT WENT BACKWARDS IS A NEW SESSION, not a glitch. The
           * engine mints a fresh caption session on every start and its seq
           * restarts at 1, so a `since` left over from the last meeting (say 6)
           * asks a three-turn new session for turns after 6 and is answered with
           * silence — forever. clearCaptions() covers the paths we know about;
           * this covers the ones we do not, and costs one comparison a second. */
          if (typeof payload.seq === "number" && payload.seq < since) {
            /* Drop the pane and go back to `since = 0`; the next tick, a second
             * away, backfills the new session from its first turn. Returning is
             * the point — assigning payload.seq here would skip exactly the
             * turns this branch exists to rescue. */
            clearCaptions();
            return;
          }
          if (typeof payload.seq === "number") {
            since = payload.seq;
          }
          if (skipFirstBatch) {
            /* `since` has just been advanced past the whole buffer; drop the
             * batch itself. Everything from here on is this session's. */
            skipFirstBatch = false;
            backfilling = false;
            return;
          }
          var turns = payload.turns || [];
          for (var index = 0; index < turns.length; index += 1) {
            appendCaption(turns[index]);
          }
          if (turns.length) {
            trimCaptions();
            if (captionCount) {
              captionCount.textContent = captions;
            }
          }
          backfilling = false;
        })
        .catch(function () {
          /* The engine may be restarting. Stay quiet; try again in a second. */
        });
    }

    function appendCaption(turn) {
      if (!captionsHost || !turn || !turn.text) {
        return;
      }
      if (emptyHost && !emptyHost.hidden) {
        emptyHost.hidden = true;
      }

      /* The one place in this lane that builds markup in JavaScript, and it is
       * deliberate: a caption arrives every second or two and round-tripping
       * the whole list through the server to add one line would re-send the
       * entire meeting each tick. Every value below goes in through
       * textContent, so a transcript is never parsed as HTML. */
      var article = document.createElement("article");
      article.className = "caption is-entering";
      article.dataset.track = turn.track || "";
      article.dataset.partial = turn.partial ? "true" : "false";

      var clock = document.createElement("span");
      clock.className = "caption-clock";
      clock.textContent = formatClock(turn.start);

      var bodyElement = document.createElement("div");
      bodyElement.className = "caption-body";

      var who = document.createElement("h3");
      who.className = "caption-who";
      /* The engine's own name for the speaker if it has one; otherwise the
       * configured name for the track, handed down by the server. Never the
       * recorder's raw "You"/"Them", which only means anything to the person
       * holding the microphone. */
      who.textContent = turn.who || (turn.track === "mic" ? micName : systemName);

      var text = document.createElement("p");
      text.className = "caption-text";
      text.textContent = turn.text;

      bodyElement.appendChild(who);
      bodyElement.appendChild(text);
      article.appendChild(clock);
      article.appendChild(bodyElement);
      captionsHost.appendChild(article);
      captions += 1;

      /* Follow the conversation, but only when the reader has not scrolled up
       * to read something — yanking the page out from under them is the single
       * rudest thing a live transcript can do. */
      if (!backfilling && isNearBottom()) {
        article.scrollIntoView({ block: "end", behavior: "auto" });
      }
    }

    function isNearBottom() {
      var slack = window.innerHeight * 0.4;
      return window.innerHeight + window.scrollY >= document.body.scrollHeight - slack;
    }

    function trimCaptions() {
      while (captionsHost.querySelectorAll(".caption").length > MAX_CAPTIONS) {
        captionsHost.removeChild(captionsHost.querySelector(".caption"));
      }
    }

    /* --- start / stop ------------------------------------------------------ */

    function refuse(result) {
      /* The engine ships a human sentence AND a stable code on every refusal.
       * Show the sentence — it names the device or the permission at fault —
       * and never replace it with a generic one. */
      var payload = result.payload || {};
      showToast(payload.error || "The engine refused (" + result.status + ").", "bad");
    }

    if (startButton) {
      startButton.addEventListener("click", function () {
        setBusy(true);
        /* A Record click over a closed engine starts the recorder in the
         * BACKGROUND and waits up to 30s. The button says only that something
         * is starting — one short word, so the control keeps its size under the
         * cursor at the exact moment of the demo's first click. What is starting
         * and how long it may take is the engine pill's sentence, immediately
         * below, which has the room to say it. */
        startButton.textContent = "Starting…";
        if (engineHost && host.dataset.online !== "true") {
          applyEngine({ state: "launching", headline: "starting the recorder",
                        detail: "Adjourn is starting the recorder in the background " +
                                "and waiting for it to answer. Stay on this page." });
        }
        postJSON("/meetings/api/record/start", {})
          .then(function (result) {
            if (result.ok) {
              showToast("Recording.", "good");
              clearCaptions();
            } else {
              refuse(result);
            }
          })
          .catch(function () {
            showToast("Could not reach the engine.", "bad");
          })
          .then(function () {
            startButton.textContent = "Start recording";
            setBusy(false);
            /* Whatever happened, the recorder is the authority on what is true
             * now — including when start failed halfway — and the launch pill has
             * a new attempt to report either way. */
            pollEngine();
            return pollStatus();
          });
      });
    }

    if (stopButton) {
      stopButton.addEventListener("click", function () {
        setBusy(true);
        stopButton.textContent = "Stopping…";
        postJSON("/meetings/api/record/stop", {})
          .then(function (result) {
            if (result.ok) {
              var id = (result.payload && result.payload.id) || "";
              showToast(id ? "Stopped. Processing " + id + "…" : "Stopped.", "good");
            } else {
              refuse(result);
            }
          })
          .catch(function () {
            showToast("Could not reach the engine.", "bad");
          })
          .then(function () {
            stopButton.textContent = "Stop";
            setBusy(false);
            return pollStatus();
          });
      });
    }

    /* --- go ---------------------------------------------------------------- */

    applyButtons();
    applyListening();
    pollStatus();
    pollCaptions();
    schedulePolling();
    window.setInterval(pollCaptions, LIVE_POLL_MS);

    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        pollStatus();
        pollCaptions();
      }
    });
  }

  /* --- start --------------------------------------------------------------- */

  if (view === "library") {
    setUpSearch();
  } else if (view === "live") {
    setUpLive();
  }
})();
