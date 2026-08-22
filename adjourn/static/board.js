/* Adjourn board — the only client-side logic there is.
 *
 * Three jobs, nothing more:
 *   1. poll /api/fragment once a second and swap in server-rendered HTML,
 *   2. animate the countdown rings locally from each card's fire_at,
 *   3. send, cancel, or undo and say honestly what came back.
 *
 * There is deliberately no card markup in this file. The server renders the
 * cards from the same Jinja macros that rendered the page, so what you curl and
 * what you watch can never disagree.
 */

(function () {
  "use strict";

  var POLL_INTERVAL_MS = 1000;
  var RING_INTERVAL_MS = 250;
  var RING_CIRCUMFERENCE = 157.08; /* 2 * pi * r, r = 25 — matches board.html */

  var body = document.body;
  var view = body.dataset.view || "board";
  var domain = body.dataset.domain || "";
  /* Only the board polls. The ledger, the connections page and one meeting's
   * page are read-once documents; polling them would churn HTML nobody is
   * watching change. Domain pages are the board. */
  var pollsForFragments = view === "board";

  /* This run's undo token, minted by the server at start and printed into the
   * page. A page from another origin can POST to 127.0.0.1 but cannot read this
   * attribute and cannot set a custom header on a simple form post, so a stray
   * tab can no longer reverse a live GitHub write mid-demo. */
  var undoToken = body.dataset.undoToken || "";

  var pendingHost = document.getElementById("pending");
  var cardsHost = document.getElementById("cards");
  var gutsHost = document.getElementById("guts") || document.querySelector("[data-guts]");
  var headerHost = document.querySelector("[data-header]");
  var toastElement = document.getElementById("toast");

  var currentVersion = body.dataset.version || "";
  var knownCardKeys = collectCardKeys();
  var toastTimer = null;

  /* --- the rail's live region ---------------------------------------------
   *
   * Two jobs, and both exist because the rail is re-rendered wholesale every
   * second: (1) animate ONLY the rows that are actually new, so a poll that
   * changed nothing does not re-run twenty entrance animations at once, and
   * (2) keep the region pinned to the newest row — but stop following the
   * moment the viewer scrolls up to read something, which is the same courtesy
   * the live captions pane extends.
   */

  var knownFeedSeqs = Object.create(null);
  var feedPinnedToBottom = true;
  var feedSeqCeiling = 0;

  function feedElement() {
    return document.querySelector("[data-rail-feed]");
  }

  function rememberFeedRows(animate) {
    var rows = document.querySelectorAll(".rail-row[data-seq]");
    var highest = 0;
    for (var index = 0; index < rows.length; index += 1) {
      var row = rows[index];
      var seq = parseInt(row.dataset.seq, 10);
      if (isNaN(seq)) {
        continue;
      }
      if (seq > highest) {
        highest = seq;
      }
      if (animate && !knownFeedSeqs[seq]) {
        row.classList.add("is-new");
      }
      knownFeedSeqs[seq] = true;
    }
    /* A run that restarts resets seq to 1 (see the feed spec). A ceiling that
     * moved BACKWARDS is that restart, so forget the old run rather than
     * silently refusing to animate the new one's first eighty rows. */
    if (highest < feedSeqCeiling) {
      knownFeedSeqs = Object.create(null);
      for (var j = 0; j < rows.length; j += 1) {
        knownFeedSeqs[rows[j].dataset.seq] = true;
      }
    }
    feedSeqCeiling = highest;
  }

  function watchFeedScroll() {
    var feed = feedElement();
    if (!feed || feed.dataset.scrollBound === "1") {
      return;
    }
    feed.dataset.scrollBound = "1";
    feed.addEventListener("scroll", function () {
      var slack = 24;
      feedPinnedToBottom =
        feed.scrollTop + feed.clientHeight >= feed.scrollHeight - slack;
    });
  }

  function followFeed() {
    var feed = feedElement();
    if (feed && feedPinnedToBottom) {
      feed.scrollTop = feed.scrollHeight;
    }
  }

  /* --- card entrance ----------------------------------------------------- */

  function collectCardKeys() {
    var keys = Object.create(null);
    var cards = document.querySelectorAll(".card[data-key]");
    for (var index = 0; index < cards.length; index += 1) {
      keys[cards[index].dataset.key] = true;
    }
    return keys;
  }

  /* The gap between one arriving card and the next. Short enough that a replay
   * landing five at once still finishes in under half a second, long enough
   * that they read as five things rather than one block appearing. Capped, so a
   * meeting that fires eleven actions does not end with a card waiting half a
   * second to show up. */
  var CARD_STAGGER_MS = 45;
  var CARD_STAGGER_CAP_MS = 270;

  function markNewCards() {
    var cards = document.querySelectorAll(".card[data-key]");
    var nextKeys = Object.create(null);
    /* Counted across the NEW cards only. Using the card's position in the column
     * would make a single late arrival wait behind every card already above it,
     * which is a delay with nothing behind it. */
    var arriving = 0;
    for (var index = 0; index < cards.length; index += 1) {
      var card = cards[index];
      var key = card.dataset.key;
      nextKeys[key] = true;
      if (!knownCardKeys[key]) {
        card.style.setProperty(
          "--enter-delay",
          Math.min(arriving * CARD_STAGGER_MS, CARD_STAGGER_CAP_MS) + "ms"
        );
        card.classList.add("is-entering");
        arriving += 1;
      }
    }
    knownCardKeys = nextKeys;
  }

  /* --- countdown rings ---------------------------------------------------- */

  function tickCountdownRings() {
    var now = Date.now();
    var cards = document.querySelectorAll(".card-pending[data-fire-at]");
    for (var index = 0; index < cards.length; index += 1) {
      var card = cards[index];
      var fireAt = Date.parse(card.dataset.fireAt);
      if (isNaN(fireAt)) {
        continue;
      }
      var windowSeconds = parseFloat(card.dataset.window) || 60;
      var remaining = Math.max(0, (fireAt - now) / 1000);
      var fraction = Math.max(0, Math.min(1, remaining / windowSeconds));

      var progress = card.querySelector(".ring-progress");
      if (progress) {
        progress.setAttribute(
          "stroke-dashoffset",
          (RING_CIRCUMFERENCE * (1 - fraction)).toFixed(2)
        );
      }
      var label = card.querySelector("[data-countdown-label]");
      if (label) {
        /* At zero the ring is full and the orchestrator is about to send; "0s"
         * reads as a dead timer, "now" reads as what is actually happening. */
        label.textContent = remaining <= 0 ? "now" : Math.ceil(remaining) + "s";
      }
    }
  }

  /* --- polling ------------------------------------------------------------ */

  function snapshotDrafts() {
    var drafts = {};
    var cards = document.querySelectorAll(".card-compose[data-key]");
    for (var index = 0; index < cards.length; index += 1) {
      var card = cards[index];
      var body = card.querySelector(".compose-body");
      var subject = card.querySelector(".compose-subject");
      drafts[card.dataset.key] = {
        text: body ? body.value : "",
        subject: subject ? subject.value : ""
      };
    }
    return drafts;
  }

  function restoreDrafts(drafts) {
    if (!drafts) {
      return;
    }
    var cards = document.querySelectorAll(".card-compose[data-key]");
    for (var index = 0; index < cards.length; index += 1) {
      var card = cards[index];
      var saved = drafts[card.dataset.key];
      if (!saved) {
        continue;
      }
      var body = card.querySelector(".compose-body");
      var subject = card.querySelector(".compose-subject");
      if (body && typeof saved.text === "string") {
        body.value = saved.text;
      }
      if (subject && typeof saved.subject === "string") {
        subject.value = saved.subject;
      }
    }
  }

  function applyFragment(fragment) {
    if (!fragment || fragment.unchanged) {
      return;
    }
    var drafts = snapshotDrafts();
    if (headerHost && fragment.header_html) {
      headerHost.innerHTML = fragment.header_html;
    }
    if (pendingHost) {
      pendingHost.innerHTML = fragment.pending_html || "";
    }
    if (cardsHost) {
      cardsHost.innerHTML = fragment.cards_html || "";
    }
    restoreDrafts(drafts);
    if (gutsHost && Object.prototype.hasOwnProperty.call(fragment, "guts_html")) {
      gutsHost.innerHTML = fragment.guts_html || "";
      rememberFeedRows(true);
      watchFeedScroll();
      followFeed();
    }
    currentVersion = fragment.version || currentVersion;
    body.dataset.version = currentVersion;
    markNewCards();
    tickCountdownRings();
  }

  function pollOnce() {
    if (!pollsForFragments) {
      return Promise.resolve();
    }
    return fetch("/api/fragment?version=" + encodeURIComponent(currentVersion) +
      (domain ? "&domain=" + encodeURIComponent(domain) : ""), {
      headers: { Accept: "application/json" },
      cache: "no-store"
    })
      .then(function (response) {
        return response.ok ? response.json() : null;
      })
      .then(applyFragment)
      .catch(function () {
        /* The orchestrator may be restarting. Stay quiet and try again in 1s. */
      });
  }

  /* --- undo --------------------------------------------------------------- */

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
    }, 4200);
  }

  function sendUndo(cardId, button) {
    button.disabled = true;
    button.textContent = "…";
    var headers = { Accept: "application/json" };
    if (undoToken) {
      headers["X-Adjourn-Undo-Token"] = undoToken;
    }
    fetch("/undo/" + encodeURIComponent(cardId), {
      method: "POST",
      headers: headers,
      cache: "no-store"
    })
      .then(function (response) {
        return response.json().catch(function () {
          return { ok: false, message: "the board could not read the response" };
        });
      })
      .then(function (outcome) {
        showToast(outcome.message || (outcome.ok ? "undone" : "refused"), outcome.ok ? "good" : "bad");
        currentVersion = ""; /* force a re-render on the next poll */
        if (!pollsForFragments) {
          /* One meeting's page does not poll, so it reloads to show the card in
           * its undone state rather than leaving a button that lies. */
          window.setTimeout(function () { window.location.reload(); }, 900);
          return Promise.resolve();
        }
        return pollOnce();
      })
      .catch(function () {
        showToast("undo could not reach the board", "bad");
        button.disabled = false;
        /* Restore the word the button actually had — a countdown says "Cancel". */
        button.textContent = button.dataset.undoLabel || "Undo";
      });
  }

  function sendNow(cardId, button) {
    var card = button.closest ? button.closest(".card-compose") : null;
    var body = card ? card.querySelector(".compose-body") : null;
    var subject = card ? card.querySelector(".compose-subject") : null;
    button.disabled = true;
    button.textContent = "…";
    var headers = {
      Accept: "application/json",
      "Content-Type": "application/json"
    };
    if (undoToken) {
      headers["X-Adjourn-Undo-Token"] = undoToken;
    }
    fetch("/send/" + encodeURIComponent(cardId), {
      method: "POST",
      headers: headers,
      cache: "no-store",
      body: JSON.stringify({
        text: body ? body.value : "",
        subject: subject ? subject.value : ""
      })
    })
      .then(function (response) {
        return response.json().catch(function () {
          return { ok: false, message: "the board could not read the response" };
        });
      })
      .then(function (outcome) {
        showToast(outcome.message || (outcome.ok ? "sent" : "refused"), outcome.ok ? "good" : "bad");
        currentVersion = "";
        if (!pollsForFragments) {
          window.setTimeout(function () { window.location.reload(); }, 900);
          return Promise.resolve();
        }
        return pollOnce();
      })
      .catch(function () {
        showToast("send could not reach the board", "bad");
        button.disabled = false;
        button.textContent = button.dataset.sendLabel || "Send";
      });
  }

  function sendAgain(cardId, button) {
    button.disabled = true;
    button.textContent = "…";
    var headers = { Accept: "application/json" };
    if (undoToken) {
      headers["X-Adjourn-Undo-Token"] = undoToken;
    }
    fetch("/resend/" + encodeURIComponent(cardId), {
      method: "POST",
      headers: headers,
      cache: "no-store"
    })
      .then(function (response) {
        return response.json().catch(function () {
          return { ok: false, message: "the board could not read the response" };
        });
      })
      .then(function (outcome) {
        showToast(outcome.message || (outcome.ok ? "sent again" : "refused"), outcome.ok ? "good" : "bad");
        currentVersion = "";
        if (!pollsForFragments) {
          window.setTimeout(function () { window.location.reload(); }, 900);
          return Promise.resolve();
        }
        return pollOnce();
      })
      .catch(function () {
        showToast("send again could not reach the board", "bad");
        button.disabled = false;
        button.textContent = button.dataset.sendLabel || "Send again";
      });
  }

  document.addEventListener("click", function (event) {
    var sendButton = event.target.closest ? event.target.closest("[data-send]") : null;
    if (sendButton) {
      event.preventDefault();
      sendNow(sendButton.dataset.send, sendButton);
      return;
    }
    var resendButton = event.target.closest ? event.target.closest("[data-resend]") : null;
    if (resendButton) {
      event.preventDefault();
      sendAgain(resendButton.dataset.resend, resendButton);
      return;
    }
    var button = event.target.closest ? event.target.closest("[data-undo]") : null;
    if (!button) {
      return;
    }
    event.preventDefault();
    sendUndo(button.dataset.undo, button);
  });

  /* --- start -------------------------------------------------------------- */

  markNewCards();
  tickCountdownRings();
  /* The server-rendered rows are the starting state, not an arrival: remember
   * them WITHOUT animating, so opening the page does not play eighty entrances. */
  rememberFeedRows(false);
  watchFeedScroll();
  followFeed();

  if (pollsForFragments) {
    window.setInterval(pollOnce, POLL_INTERVAL_MS);
    window.setInterval(tickCountdownRings, RING_INTERVAL_MS);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        pollOnce();
      }
    });
  }
})();
