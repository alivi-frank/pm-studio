"""PM Studio - a local PM/dev-coordination studio driven by the headless Claude CLI.

A single stakeholder talks to a PM agent through a local web chat; the PM turns goals
into a live spec, dispatches background dev agents into the repo, is auto re-invoked
the moment each dev task finishes, and merges parallel session worktrees back into
main. Install into any git repo and run `python -m pm_studio` from the repo root.
"""

__version__ = "0.1.1"
