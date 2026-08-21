"""Lane 3 — the guards, the repo migration, and the publish tree.

Every check here exists because an audit found the opposite, or because a
migration would otherwise be silently half-done. Run from the repo root:

    python -m adjourn.tests.test_lane3_guards

Reads only. No GitHub write, no Slack send, no journal line. The one network
call is a `gh api` GET against the allowlisted repository, and it is skipped
when `gh` is not authenticated.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["ADJOURN_SIM"] = "1"  # before any adjourn import

from adjourn import config, secrets_store  # noqa: E402
from adjourn.executors import __init__ as executors_package  # noqa: E402,F401
from adjourn import executors  # noqa: E402
from adjourn.executors import github_update_executor as github  # noqa: E402
from adjourn.executors import slack_send_executor as slack  # noqa: E402
from adjourn.tools import github_world, publish_tree, seed_demo_world  # noqa: E402

_passed = 0
_failed: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  ok   {label}")
    else:
        _failed.append(label)
        print(f"  FAIL {label} {detail}")


def raises(label: str, exception: type[BaseException], call) -> None:
    try:
        call()
    except exception as error:
        check(label, True)
        print(f"       -> {str(error)[:96]}")
        return
    except Exception as error:  # noqa: BLE001
        check(label, False, f"raised {type(error).__name__}: {error}")
        return
    check(label, False, "did not raise")


print("\n== the repository migration ==")
check("the allowlist holds exactly one repo", len(config.ALLOWED_GITHUB_REPOS) == 1,
      str(sorted(config.ALLOWED_GITHUB_REPOS)))
check("and it is Adjourn's own", config.ALLOWED_GITHUB_REPOS == frozenset({"sharique2004/adjourn"}),
      str(sorted(config.ALLOWED_GITHUB_REPOS)))
check("the default matches the allowlist", config.DEFAULT_GITHUB_REPO in config.ALLOWED_GITHUB_REPOS)
check("github_repo() resolves inside the allowlist",
      config.github_repo() in config.ALLOWED_GITHUB_REPOS, config.github_repo())

_was = os.environ.get("ADJOURN_REPO")
os.environ["ADJOURN_REPO"] = "someone-else/private"
check("an ADJOURN_REPO outside the allowlist is ignored, not obeyed",
      config.github_repo() == config.DEFAULT_GITHUB_REPO, config.github_repo())
if _was is None:
    os.environ.pop("ADJOURN_REPO", None)
else:
    os.environ["ADJOURN_REPO"] = _was

# Spelled in parts so this file does not itself become the last thing in the
# package that names the retired repository.
RETIRED_REPO = "sharique2004/" + "mmm" + "-demo"
_still_naming = [
    str(path.relative_to(config.PACKAGE_ROOT))
    for path in config.PACKAGE_ROOT.rglob("*.py")
    if "__pycache__" not in str(path) and path.name != Path(__file__).name
    and RETIRED_REPO in path.read_text(encoding="utf-8", errors="ignore")
]
check("no source file still names the retired repo", not _still_naming, str(_still_naming))

print("\n== the executor registry ==")
check("pr_review_suggestion is registered",
      "pr_review_suggestion" in executors.REGISTRY)
check("it points at the module the lane built",
      executors.REGISTRY.get("pr_review_suggestion") == "pr_review_suggestion_executor")
check("every registered kind actually imports",
      all(hasattr(executors.load_executor(kind), "execute") for kind in executors.REGISTRY))
check("and every one can be undone",
      all(hasattr(executors.load_executor(kind), "undo") for kind in executors.REGISTRY))

print("\n== secrets never reach a child process ==")
check("no secret-shaped variable is left in os.environ",
      not [name for name in os.environ if config._is_secret_shaped(name)],
      str([name for name in os.environ if config._is_secret_shaped(name)]))
check("the FalkorDB password is still readable in-process",
      bool(config.read_private_setting("FALKORDB_CLOUD_PASSWORD"))
      or not (config.ENV_PATH.exists()
              and "FALKORDB_CLOUD_PASSWORD=" in config.ENV_PATH.read_text(encoding="utf-8")))
check("LINEAR_TEAM_KEY is exempt — a team handle is not a credential",
      not config._is_secret_shaped("LINEAR_TEAM_KEY"))
check("a *_TOKEN name is quarantined", config._is_secret_shaped("ANY_TOKEN"))
check("a *_API_KEY name is quarantined", config._is_secret_shaped("SOME_API_KEY"))

child_environment = config.child_process_environment()
check("a child environment carries no credential",
      not [name for name in child_environment if config._is_secret_shaped(name)])
check("but does carry HOME, which is where `gh` and the model CLI keep theirs",
      "HOME" in child_environment and "PATH" in child_environment)

inherited = subprocess.run(
    [sys.executable, "-c", "import os; print('\\0'.join(os.environ.values()))"],
    capture_output=True, text=True, timeout=30,
).stdout
for label, value in (
    ("slack bot token", secrets_store.get_secret(secrets_store.SLACK_BOT_TOKEN)),
    ("linear api key", secrets_store.get_secret(secrets_store.LINEAR_API_KEY)),
    ("falkordb password", config.read_private_setting("FALKORDB_CLOUD_PASSWORD")),
):
    if value:
        check(f"a fully-inheriting child cannot see the {label}", value not in inherited)

raises("a secret in argv is refused before the process starts", ValueError,
       lambda: secrets_store.assert_no_secret_in_argv(
           ["gh", "api", f"x?t={secrets_store.get_secret(secrets_store.SLACK_BOT_TOKEN)}"]
       ) if secrets_store.get_secret(secrets_store.SLACK_BOT_TOKEN)
       else (_ for _ in ()).throw(ValueError("no token provisioned; guard vacuously holds")))

print("\n== Slack: the destination is configuration, not payload ==")
check("there is a channel allowlist at all", bool(config.ALLOWED_SLACK_CHANNELS))
check("it is the one channel", "#all-test" in config.ALLOWED_SLACK_CHANNELS)
raises("a channel outside it is refused", PermissionError,
       lambda: slack.assert_channel_is_allowed("#general"))
raises("an empty channel is refused", PermissionError,
       lambda: slack.assert_channel_is_allowed(""))


class _Action:
    kind = "slack_send"
    payload = {"channel": "#general", "text": "hello"}
    quote = speaker = meeting_id = ""


check("a payload channel does not become the destination",
      slack.resolve_channel(_Action()) != "#general", slack.resolve_channel(_Action()))
check("a payload channel does not waive the channel secret",
      len(slack.required_secrets_for(_Action())) == 2)

print("\n== GitHub: the target issue must exist, in the allowed repo ==")
raises("a repo outside the allowlist is refused", PermissionError,
       lambda: github.assert_repo_is_allowed("torvalds/linux"))
if github.is_github_cli_ready():
    raises("an issue number that resolves to nothing is refused",
           github.IssueNotVerified,
           lambda: github.verify_issue_target(config.github_repo(), 99_999))
else:
    print("  skip gh is not authenticated — the live existence check was not exercised")

print("\n== the demo-world scripts ==")
check("they refuse to name any repo but the allowlisted one",
      github_world.target_repo() in config.ALLOWED_GITHUB_REPOS)
check("the prop patches two files", len(github_world.prop_paths()) == 2)
check("both of them are the landing page",
      set(github_world.prop_paths()) == {"web/app/globals.css", "web/app/page.tsx"})

sources = {path: (config.REPO_ROOT / path).read_text(encoding="utf-8")
           for path in github_world.prop_paths()}
patched = github_world.patched_files(sources)
check("the old hex appears exactly once in the patched diff",
      sum(text.count(github_world.PROP_OLD_HEX) for text in patched.values()) == 1)
check("applying the patch twice changes nothing the second time",
      github_world.patched_files(patched) == patched)
raises("a moved anchor is an error, not a silent overwrite", github_world.WorldError,
       lambda: github_world.Patch(
           "x.css", "ANCHOR THAT IS NOT THERE\n", "INSERTED").apply("a file body\n"))

check("spoken numbers read as speech",
      (seed_demo_world.spoken_number(5), seed_demo_world.spoken_number(22),
       seed_demo_world.spoken_number(30)) == ("five", "twenty-two", "thirty"))
raises("a number it cannot spell is refused rather than guessed",
       github_world.WorldError, lambda: seed_demo_world.spoken_number(1000))

check("the roadmap mirrors the four live Linear tickets",
      sorted(e["linear"] for e in seed_demo_world.ROADMAP if e["linear"])
      == ["SHA-5", "SHA-6", "SHA-7", "SHA-8"])

# GitHub numbers issues in creation order, and the demo SPEAKS two of those
# numbers. prior-standup says "issue two" about the cache layer and "issue one"
# about the auth migration, and memory builds its topic -> issue index from
# those spoken words. Seed the repo in a different order and nothing errors:
# every card still fires, onto issues whose titles have nothing to do with them.
check("issue #1 will be the auth migration, because the prior standup says so",
      "Auth migration" in seed_demo_world.ROADMAP[0]["title"],
      seed_demo_world.ROADMAP[0]["title"])
check("issue #2 will be the cache layer, which is the conflict beat's target",
      "Cache layer" in seed_demo_world.ROADMAP[1]["title"],
      seed_demo_world.ROADMAP[1]["title"])
_prior = (config.FIXTURES_DIR / "prior-standup.jsonl").read_text(encoding="utf-8")
check("...and those are the numbers actually spoken in that transcript",
      "issue two" in _prior and "Issue one" in _prior)
check("an entry with no Linear ticket does not claim a mirror it lacks",
      "no ticket on the Linear board"
      in seed_demo_world.issue_body(seed_demo_world.ROADMAP[0]))
check("an entry with one does",
      "Mirrors `SHA-6`" in seed_demo_world.issue_body(seed_demo_world.ROADMAP[1]))

print("\n== the fixtures agree with the prop ==")
import json  # noqa: E402

ground_truth = json.loads(
    (config.FIXTURES_DIR / "pr-review-beat.fixtures.json").read_text(encoding="utf-8"))
statement = ground_truth["statements"][0]
spoken = [json.loads(line)["text"]
          for line in (config.FIXTURES_DIR / "pr-review-beat.jsonl")
          .read_text(encoding="utf-8").splitlines()
          if json.loads(line).get("segment_id") == "prb-s05"][0]
check("the ground-truth quote is byte-verbatim to the transcript",
      statement["quote"] == spoken)
check("the spoken number matches entity_refs.issue_number",
      f"pull {seed_demo_world.spoken_number(statement['entity_refs']['issue_number'])}" in spoken,
      spoken)
check("the old hex is NOT in the transcript — it lives only in the diff",
      github_world.PROP_OLD_HEX not in spoken)
check("and it is recorded with its provenance",
      statement["_extras"]["old_value_provenance"] == "diff")

print("\n== the publish tree ==")
with tempfile.TemporaryDirectory() as temporary:
    out = Path(temporary) / "tree"
    files = publish_tree.build(out)
    relative = {str(path.relative_to(out)) for path in files}
    check("it produces a README, a LICENSE and a .gitignore",
          {"README.md", "LICENSE", ".gitignore"} <= relative)
    check("the README leads with the line the product is named for",
          "the meeting is the to-do" in (out / "README.md").read_text(encoding="utf-8").lower())
    check("no .env travels", not any(name.endswith(".env") for name in relative))
    check("no bytecode travels", not any(".pyc" in name for name in relative))
    check("no node_modules travels", not any("node_modules" in name for name in relative))
    check("state ships its doc and nothing else",
          {name for name in relative if name.startswith("adjourn/state")}
          == {"adjourn/state/README.md"})
    check("recaps ship their doc and nothing else",
          {name for name in relative if name.startswith("adjourn/recaps")}
          == {"adjourn/recaps/README.md"})
    check("the fixtures do travel — they are the product's floor",
          "adjourn/fixtures/pr-review-beat.jsonl" in relative)
    # The needle is computed, not written down, so this file cannot be the one
    # that fails the check it is making.
    build_root = str(config.REPO_ROOT)
    leaked_paths = [name for name in relative if name.endswith((".py", ".md", ".txt"))
                    and build_root in (out / name).read_text(encoding="utf-8", errors="ignore")]
    check("no path to the machine this was built on travels", not leaked_paths,
          str(leaked_paths))

    failures, _ = publish_tree.sweep(out)
    check("no live credential appears anywhere in the tree", not failures, str(failures))

    secrets = publish_tree.live_secret_values()
    if secrets:
        label, value = next(iter(secrets.items()))
        (out / "CANARY.md").write_text(f"planted {label}: {value}", encoding="utf-8")
        planted, _ = publish_tree.sweep(out)
        check("and the sweep can actually find one when it is there",
              any("CANARY" in line for line in planted), str(planted))
    else:
        print("  skip no credentials provisioned — the sweep could not be proven live")

print("\n" + "=" * 60)
if _failed:
    print(f"{_passed} passed, {len(_failed)} failed: {_failed}")
    sys.exit(1)
print(f"ALL {_passed} CHECKS PASSED")
