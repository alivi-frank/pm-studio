"""Independent compliance judge for dev tasks.

A system may declare non-negotiable git workflow rules (SystemSpec.gitflow). The dev
agent gets them injected into its prompt at dispatch time (tasks.py), but injection
guarantees DELIVERY, not COMPLIANCE - so after a dev task finishes, this module runs a
second, independent agent whose only job is to verify the finished work against those
rules and return a structured verdict.

What makes it a judge rather than a second developer:

- It cannot write. It runs with a read-only tool allowlist (no bypassPermissions, no
  Edit/Write, git restricted to inspection subcommands), enforced by the CLI's own
  allowlist mechanism - not by asking nicely in the prompt.
- Its prompt is composed HERE, from the declared rules file and the observable git
  evidence. Neither the PM (who wrote the task) nor the dev agent (who did the work)
  contributes a word to it, and the dev agent's own account of what it did is never
  shown to the judge - the repository is the only witness.
- It judges an exact commit range (head_before..head_after, recorded by the registry
  around the task), so it cannot blame the dev for work it didn't do or miss work it
  did.

A judge that errors, times out, or answers in a shape we can't parse yields verdict
"inconclusive" - visibly, on the task record - never a silent pass. Fail loud, never
fail open.
"""

import json
import subprocess
from pathlib import Path

from .costing import agent_usage
from .models import MODELS

JUDGE_TIMEOUT_SECONDS = 600

# Inspection only. Every Bash entry is a literal-prefix match the CLI enforces, so the
# judge structurally cannot commit, branch-delete, push, or edit a file - whatever its
# prompt is talked into.
JUDGE_ALLOWED_TOOLS = (
    "Read Grep Glob "
    "Bash(git log:*) Bash(git diff:*) Bash(git show:*) "
    "Bash(git status:*) Bash(git branch:*)"
)

VERDICT_PASS = "pass"
VERDICT_VIOLATION = "violation"
VERDICT_INCONCLUSIVE = "inconclusive"
VERDICTS = (VERDICT_PASS, VERDICT_VIOLATION, VERDICT_INCONCLUSIVE)

JUDGE_PROMPT_TEMPLATE = """\
You are a compliance judge. A development agent has just finished a task in the \
repository at your working directory. Your ONLY job is to decide, from the repository's \
actual state, whether that work followed the non-negotiable git workflow rules below. \
You have read-only access on purpose: you judge, you never fix, and you never take the \
development agent's word for anything - the repository is the only evidence that counts.

NON-NEGOTIABLE GIT WORKFLOW RULES for the {system_label} system (id `{system_id}`), \
from {gitflow_path}:
{rules}

The task the development agent was given:
{description}

The work to judge is everything between commit {head_before} and commit {head_after} - \
inspect it with `git log {head_before}..{head_after}` and \
`git diff {head_before}..{head_after}`. One caveat: the last commit in that range may be \
an automatic bookkeeping snapshot made by the PM Studio server itself (its message starts \
with "Dev task"). That snapshot is not the development agent's commit - never count its \
existence or its message as a violation - but work the agent left uncommitted lands inside \
it, so if the rules require the agent to commit its own work in a particular way, work \
found only in the snapshot can still evidence a violation of THAT rule.

Reply with ONLY a JSON object, no prose before or after it, in exactly this shape:
{{"verdict": "pass" | "violation" | "inconclusive",
  "violations": [{{"rule": "<the rule broken, quoted or closely paraphrased from the rules above>",
                   "evidence": "<concrete, checkable evidence: a commit sha, a branch name, a file path>"}}],
  "summary": "<one or two sentences>"}}

"pass" means every rule you could check is satisfied (violations must be empty). \
"violation" means at least one rule is concretely broken - every entry must cite real \
evidence from the repository, never a guess or a suspicion. "inconclusive" means the \
repository does not let you verify one way or the other - say why in the summary. \
Rules about things this repository cannot show (a remote you cannot reach, a CI run) \
are out of your reach: skip them rather than guessing, and mention the skip in the \
summary."""


def inconclusive(summary: str, model: str = "", usage: dict | None = None) -> dict:
    return {
        "verdict": VERDICT_INCONCLUSIVE,
        "violations": [],
        "summary": summary,
        "model": model,
        "agent_usage": usage or {},
    }


def judge_model(fallback: str) -> str:
    """Mechanical compliance-checking against a written rule sheet doesn't need the
    deployment's biggest model - prefer the cheap tier when the deployment offers it,
    fall back to the dispatching registry's model when it doesn't."""
    for candidate in ("sonnet", "haiku"):
        if candidate in MODELS:
            return candidate
    return fallback


def parse_verdict(result_text: str) -> dict | None:
    """The judge's answer as {verdict, violations, summary}, or None when the text
    isn't the JSON object the prompt demanded (the caller records that as
    inconclusive, keeping the raw text for the human). Tolerates the one deviation
    models actually produce - a ```json fence around an otherwise-correct object -
    and nothing looser: a lenient parser here would let a malformed pass through."""
    text = result_text.strip()
    if text.startswith("```"):
        # Drop the opening fence line and the closing fence, keep what's between.
        lines = text.splitlines()
        if lines[-1].strip().startswith("```"):
            text = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return None
    raw_violations = data.get("violations") or []
    if not isinstance(raw_violations, list):
        return None
    violations = [
        {
            "rule": str(v.get("rule", "")).strip(),
            "evidence": str(v.get("evidence", "")).strip(),
        }
        for v in raw_violations
        if isinstance(v, dict)
    ]
    # A "violation" verdict with nothing cited is an unusable answer, not a lenient
    # pass and not a trusted failure - the caller shows it as inconclusive.
    if data["verdict"] == VERDICT_VIOLATION and not violations:
        return None
    return {
        "verdict": data["verdict"],
        "violations": violations,
        "summary": str(data.get("summary", "")).strip(),
    }


def run_judge(
    repo_root: Path,
    system_id: str,
    system_label: str,
    gitflow_path: str,
    rules: str,
    description: str,
    head_before: str,
    head_after: str,
    fallback_model: str,
) -> dict:
    """One judge turn over a finished dev task's commit range. Always returns a verdict
    dict ({verdict, violations, summary, model, agent_usage}); every failure mode maps
    to "inconclusive" with the reason in the summary."""
    model = judge_model(fallback_model)
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        system_id=system_id,
        system_label=system_label,
        gitflow_path=gitflow_path,
        rules=rules,
        description=description,
        head_before=head_before,
        head_after=head_after,
    )
    try:
        proc = subprocess.run(
            [
                "claude",
                "-p", prompt,
                "--output-format", "json",
                "--allowedTools", JUDGE_ALLOWED_TOOLS,
                "--model", model,
            ],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=JUDGE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return inconclusive(f"Judge timed out after {JUDGE_TIMEOUT_SECONDS}s.", model)
    except FileNotFoundError:
        return inconclusive("The `claude` CLI is not installed or not on PATH.", model)

    stdout = proc.stdout.strip()
    if not stdout:
        return inconclusive(
            f"Judge produced no output (exit {proc.returncode}). "
            f"stderr: {proc.stderr.strip()[:500]}",
            model,
        )
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return inconclusive(f"Judge output was not the CLI's JSON: {stdout[:500]}", model)

    usage = agent_usage(data)
    if data.get("is_error") or proc.returncode != 0:
        return inconclusive(
            f"Judge run errored: {str(data.get('result', ''))[:500]}", model, usage
        )
    verdict = parse_verdict(str(data.get("result", "")))
    if verdict is None:
        return inconclusive(
            "Judge did not answer in the required JSON shape: "
            f"{str(data.get('result', ''))[:500]}",
            model,
            usage,
        )
    return {**verdict, "model": model, "agent_usage": usage}
