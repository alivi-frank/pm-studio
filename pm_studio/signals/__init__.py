"""Engineering intelligence across the SDLC: the *signal* layer.

Everything the rest of PM Studio knows is a *state* - a ticket's status, a change's
bucket, a project's lifecycle. This package works from *events* instead: every
observable act of engineering (a commit, a status transition, a worklog, a comment,
an agent turn, a meeting) becomes one normalised `Signal`, attributed up the work
model (ticket -> change -> project -> initiative -> goals) and down to a person.

From that one ledger the layer derives, at read time and for any time window:

- where effort actually went (activity-weighted hours per initiative/project/person);
- what that effort produced (shipped changes, closed tickets, releases) - impact;
- flow metrics (cycle, lead, review dwell, reopen, throughput, WIP);
- how people work (hour-of-day, context switching, tagging discipline, AI assist);
- automatically highlighted problems (stale, silent, misattributed, lying statuses);
- audit-ready capitalization / R&D time reports with the evidence behind each hour;
- and an independent AI judge that scores the whole picture and feeds back.

Sources are adapters behind one interface (`sources.base.SignalSource`), so calendar,
email or chat activity slot in beside git and the trackers without touching anything
downstream.
"""
