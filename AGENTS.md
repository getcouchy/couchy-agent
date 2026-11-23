# Agents working on couchy-agent

Claude, Codex and Grok agents work this repository under the same protocol as
[getcouchy/couchy](https://github.com/getcouchy/couchy). The protocol and the
tools that run it live in **[getcouchy/swarm](https://github.com/getcouchy/swarm)**:
read its [`PROTOCOL.md`](https://github.com/getcouchy/swarm/blob/main/PROTOCOL.md)
before picking up work here. This file adds only what is this repository's own.

## Getting the protocol and the tools

getcouchy/swarm is public, so your sandbox can clone it without being given
access. At the start of every tick, beside your checkout of this repository:

```bash
git clone --depth 1 https://github.com/getcouchy/swarm ../swarm   # or: git -C ../swarm pull --ff-only
```

Then read `../swarm/PROTOCOL.md`. With a GitHub token and Linear credentials
(`PROTOCOL.md`, "Where the work is"), run the tools from this checkout with
this repository as home: `GITHUB_REPOSITORY=getcouchy/couchy-agent
AGENT_REPOS=getcouchy/couchy-agent AGENT_NAME=<your name> node ../swarm/agent.mjs next`.

## Where the work is

The queue is in Linear: this repository's work items are in the Couchy team
(keys like `COU-12`) with getcouchy/couchy's, labelled `repo/couchy-agent`, and
the tick takes them in the order `PROTOCOL.md` gives (in focus, in Todo, nobody
on them, no open PR, no open blocker). A blocker is often getcouchy/couchy
work: Linear's "blocked by" relation names it, or the description does
("Blocked by COU-40").

## Home repository

A tick whose prompt names this repository has it as its home: its Agent desk
is the open `swarm/desk` work item that belongs to this repository, and its
heartbeat goes there. `.github/workflows/agent-sweep.yml` sweeps it every hour,
freeing lapsed claims and keeping that desk current. A tick of getcouchy/couchy
covers this repository too when its sandbox can reach it, and heartbeats on
couchy's desk.

## This repository's rules

- There is no changelog check and no simulator flow. Run
  `python3 -m unittest discover -s tests -t .` (README, "Test") before you push.
- `reviewed` is not a required check here, so auto-merge goes on with an `ok`
  verdict (the protocol's "Merging"), not when a PR opens.
