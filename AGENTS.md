# Agents working on couchy-agent

Claude, Codex and Grok agents work this repository as part of the same tick
that covers [getcouchy/couchy](https://github.com/getcouchy/couchy). The
protocol is that repository's
[`AGENTS.md`](https://github.com/getcouchy/couchy/blob/main/AGENTS.md); read it
before picking up work here. It applies here unchanged:

- An `swarm/agent/<name>` label on an issue means it is taken. Put yours on before
  you start, and on the PR you open; never start or finish an issue that
  carries another agent's label.
- The queue is the open issues labelled `couchy-ux/approved` with no `swarm/agent/*`
  label whose blockers are closed. A blocker is often in the other repository
  ("Blocked by getcouchy/couchy#1203").
- Any `swarm/agent/*` PR here is yours to review unless you wrote it in this same
  session. End the review with `<!-- agent: <your name> verdict: ok -->` or
  `verdict: changes`; `ok` turns on auto-merge (squash).
- `swarm/needs-human` is for a real fork and pages the owner; `swarm/hold` holds a
  PR for a wait an agent can end.

What differs here:

- The Agent desk and the heartbeats are in getcouchy/couchy. Nothing sweeps
  this repository, so a claim here does not lapse on its own: release yours
  (remove your label and comment why) if you stop.
- There is no changelog check and no simulator flow. Run `python3 -m unittest discover -s tests -t .` (README, "Test")
  before you push.
