"""Operational tools for Adjourn — seeding, scrubbing, and building the push tree.

Nothing in here is imported by the running product. These are the scripts a
human runs from a terminal, kept in the repo because the alternative is a
paragraph in a README that goes stale the first time anybody uses it.

    python -m adjourn.tools.seed_demo_world --dry-run
    python -m adjourn.tools.scrub_demo_world --rehearsal
    python -m adjourn.tools.publish_tree --out /tmp/adjourn-push
"""
