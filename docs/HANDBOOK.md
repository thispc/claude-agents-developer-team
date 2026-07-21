# devteam — how it works

*Written for someone who does not write code. Every technical term is explained
where it first appears, and again in the glossary at the end.*

A rendered version of this document, with diagrams, is published separately as a
web page. This is the same content in plain text so it lives with the source.

---

## What it actually is

Imagine hiring a small software agency. You describe what you want. A manager
reads it, works out the pieces, decides who is needed, and hands each person a
task. They go away, write the code, test it, and submit it for review. The
manager checks the work, sends back what is not good enough, approves what is,
and every so often asks you what you think.

**devteam is that agency, staffed by AI.** The manager is an AI. The engineers
are AI. They are not one chatbot pretending to be a team — they are separate
programs running at the same time, each with its own instructions and its own
copy of the code, unable to see inside each other. They coordinate the way a real
team does: tasks, written handovers, reviews, pull requests.

The unusual part is not the AI, it is the ownership. It runs on your computer or
your cloud account, spends your AI credits, and pushes to your code repository.
Nothing is hosted for you and there is no account to sign up for.

---

## Why "your own" matters

**Your own AI keys.** An *API key* is a password that lets a program use an AI
model and bills you for it. You supply your own, so you see the real cost and can
switch models. Each user spends their own money — the software refuses to run one
user's projects on another's credit. Claude, OpenAI and Gemini are supported, as
is a model you host yourself.

**Your own cloud.** The system is a program you run: on a laptop to try it, on a
rented server to use it properly. There is no version where your code sits on
someone else's machine.

**Your own version control.** *Version control* is the system that stores code
and remembers every change ever made — the industry's shared filing cabinet.
GitHub is the best known. devteam works with GitHub, with a company's private
GitHub, and with self-hosted alternatives, so code can stay inside a network.

---

## Who is on the team

| Who | What they are | What they do |
|---|---|---|
| **You** | The boss | Write the brief, answer when asked, decide what ships |
| **The manager** | AI | Plans, assigns, reviews, decides |
| **The teammates** | AI | Write the code, one task at a time |
| **The scheduler** | Ordinary software | Decides what starts when; costs nothing to run |
| **The round table** | AI, optional | Argues about your idea before anyone builds |

### Why the manager cannot write code

The manager physically cannot open a file or run a command. It can only create
tasks, read reports, ask questions, send work back, and approve things.

An AI that can both assign work and do the work will, under pressure, quietly do
the work itself and report that the team delivered it. That is not hypothetical —
it happened during development, and the manager reported completed work nobody had
built. Removing the ability removes the temptation.

### Why teammates have names

Each teammate is a durable record: a name, a role, a personality, which model they
run on, and short notes on what they have built. A task sent back for a second
attempt goes to the same person, who remembers the first.

"Send it back to Priya, she wrote it" is a different instruction from "run the
backend task again". The first keeps the context; the second pays to rediscover it.

---

## The life of a project

1. **You write a brief.** A paragraph or two, plus how many rounds to run and how
   independent the team should be.
2. **Optionally a round table argues first.** Five or six AIs *from different
   companies* propose approaches, critique each other, and revise. Different
   companies is the point — two copies of one model tend to agree, and agreement
   is not the same as being right.
3. **The manager plans.** The brief becomes concrete tasks, recording which must
   wait for others. Unrelated work runs side by side.
4. **The team is hired.** Roles become named people with personalities. A persona
   the round table argued for is written into that teammate's standing brief.
5. **Tasks are handed out.** Whatever is unblocked starts, up to a limit you set.
   When it cannot start everything, it starts whatever is holding up the most
   other work.
6. **A teammate builds.** Their own private copy of the code, the handover notes
   from whoever they build on, and the ability to ask a named colleague — who
   answers with their own actual work in front of them.
7. **The platform runs the tests — not the AI.** The single most important
   safeguard. The raw result is recorded before the AI writes its summary.
   Without it you have only the AI's word, and AIs will describe broken code in
   glowing terms. With it, the manager judges evidence rather than a claim.
8. **The work is reviewed.** Test result first, write-up second. Teammates can
   review independently, never seeing each other's opinions, and never their own
   work. The manager is shown the *disagreement*, not a verdict.
9. **Approved or sent back.** Approved work becomes a *pull request* — a formal
   proposal you can read and comment on. Rejected work returns to the same person
   with specifics. Two failures escalate to a stronger model; repeated failure is
   treated as a planning problem, not an effort problem.
10. **The round ends and you are asked** what should change before the next one.

---

## What the machine looks like

Six pieces; only two cost money to run.

| Piece | What it does | Costs money? |
|---|---|---|
| Dashboard | The web page you look at | No |
| Conductor | The central program; serves the page, holds the records | No |
| Records | One file on disk with every project, task and result | No |
| Scheduler | Decides what starts when, notices stalled work | No |
| Manager | The AI that plans and supervises | Yes |
| Teammates | Separate AI programs that write code | Yes |

**Why the scheduler is not an AI.** "Task 4 waits on 1 and 2, both are done, so 4
can start" is arithmetic. Asking an AI would cost money every check, take seconds
instead of milliseconds, and occasionally be wrong in a way that is hard to spot.
The rule is: **AI for judgement, ordinary software for bookkeeping.**

**Why teammates are separate programs.** Each has its own private copy of the code
and cannot disturb anyone else. If one crashes or hangs it is killed alone. Two
teammates pointed at the same file would overwrite each other, so that is treated
as a dependency rather than pretended to be parallel work.

---

## How the manager decides

Grounded in published research on multi-agent AI performance rather than in
intuition about what sounds clever.

**Combining opinions beats collecting them.** The finding that shaped this system
is that *how* you aggregate several opinions matters several times more than how
many you gather. So reviewers read independently and never see each other — a
second reviewer who reads the first's verdict tends to agree with it, and you have
paid twice for one opinion. Disagreements are preserved rather than averaged away.

**Competitions, used sparingly.** For genuinely uncertain work, two or three
teammates attempt it at once on different models and the manager picks a winner.
Capped low, because the value is in judging well, not in the number of entries.

**Cheap first, expensive when earned.** Work starts on a fast cheap model and
escalates after repeated failure, relative to whatever just failed. Crucially it
distinguishes *the AI could not do this* from *the provider was busy* — the second
is not a quality failure, and treating it as one spends money on an expensive
model to fix a problem that would have cleared up on its own.

**Slices or layers.** *Agile* splits by outcome so something runs on day one, and
pays in friction where slices touch the same files. *Waterfall* splits by layer,
which suits work whose shape is already known, and pays by keeping a misread brief
alive until it is expensive. The manager is told the cost it is accepting.

**Everything is measured.** Every AI task records the model, attempt number, cost,
duration and whether the work was finally accepted. From that comes the number
that matters: **how often work is accepted on the first attempt** — the honest
measure of whether planning and briefing are working, because it is the one thing
that cannot be improved by spending more.

---

## Sprints and your involvement

Work runs in *sprints* — rounds of building, testing and shipping. You say roughly
how many; the manager can revise that once it understands the work, because your
original number was a guess made before anyone read anything.

| Mode | What happens | Suits |
|---|---|---|
| Ask me | Stops at every meaningful decision | Watching it work |
| Supervised | Proceeds, but stops for decisions that could paper over a problem | Most work |
| Autonomous | Decides everything, records every judgement call for audit | Overnight runs |

Three decisions are always flagged because they are dangerous precisely when made
smoothly: approving work nobody delivered, shipping code whose tests fail, and
giving up after repeated failure.

**Being asked without being blocked.** The end of a sprint is the cheapest moment
to redirect — nothing is built yet. But the question does not block: an overnight
run that stalled an hour per sprint waiting for you would destroy the thing you
asked for. Your answer is read whenever it arrives, labelled with which sprint it
was about so nobody rewrites sealed history.

---

## Where the work ends up

- **Code, in your repository.** Every task arrives as a pull request. Nothing
  lands in your main codebase without passing that gate.
- **A frozen record per sprint.** What was delivered, by whom, with which pull
  request, and whether tests passed. Later sprints cannot rewrite it.
- **Release notes built from facts.** Assembled from what actually happened. If an
  AI writes the prose, its draft is discarded entirely if it cites work the sprint
  did not contain. A release note that cannot be traced to a task is worse than none.
- **Documents, for non-code projects.** Research and strategy work is rendered
  properly rather than dumped as raw text.
- **Running previews.** A branch can be deployed on its own so you can click
  through a change before deciding on it.

---

## How it looks after itself

devteam's own source code can be given to devteam as a project, going through the
same review gate as anything else.

**The daily check.** Once a day it reviews what went wrong — failures to start
work, agents that went silent, dashboard errors, failed requests. Each distinct
fault becomes a tracked item that is *counted*. Previously all of it went into a
running activity feed, visible only to someone already watching, so a fault that
happened forty times over a weekend looked exactly like one that happened once.

How it ranks what is wrong:

- **Severity sets the band, frequency ranks within it** — a rare serious fault
  always outranks a common trivial one.
- **Old problems sink**, because something that stopped happening was probably
  fixed by something else, and without this the top of the list is a museum.
- **A problem that returns after a fix reopens loudly**, because a repair that did
  not hold is more interesting than a fresh fault.

**What it will not do alone.** Looking, counting and ranking are free and
automatic. Raising a ticket is cheap, reversible and automatic. Rewriting its own
running code is neither and does not happen because something scored highly. An
unattended process that both decides what is wrong and rewrites the program it is
running inside, unwatched, is a specific and bad idea unless the operator has said
otherwise.

**Updating itself safely:** staging (a full second copy that may propose but not
approve) → the whole test suite run *inside the new version* → a canary started
privately with no traffic just to see if it works → promotion by swapping which
version runs, never by re-applying the whole configuration, which has been
observed to silently roll a system backwards → rollback to the previously
known-good version from history rather than rebuilding it, because rebuilding to
go backwards produces a *new* version from old instructions.

**Watched from outside.** A separate service checks the live site from three
continents and emails if it stops answering or its certificate is near expiry.
This must come from outside, because the platform cannot send a message telling
you it has crashed.

---

## The safety rails

| Rail | What it prevents |
|---|---|
| A limit on AI tasks per project | A stuck loop burning credits overnight |
| A spending cap | The same, in money, when billed per use |
| Tests run by the platform | An AI describing broken code as working |
| The manager cannot write code | The manager doing the work and crediting the team |
| Staging cannot approve its own changes | A test environment shipping to production |
| Everyone spends their own credits | One user's work billed to another |
| Limited login attempts | Password guessing; survives restarts, which it did not before |
| A watchdog on silent work | A crashed teammate stalling a project forever |
| Disk space limits | Accumulated code copies filling the disk and stopping everything |

**One honest caveat.** Teammates run with broad permissions inside their own copy
of the code — they must run commands to build and test. On a properly configured
server each is boxed into an isolated container. On a laptop that boundary is
weaker: a teammate is a program on your machine with your files reachable. A
deliberate trade, and the right one for the tool to be useful, but worth knowing
rather than discovering.

---

## What it costs

**The server:** roughly **$40/month** for a small managed cluster. The largest
single component is not computing power but the load balancer — the piece that
accepts traffic from the internet. That is the usual cloud pricing trap.

**The AI:** billed by your provider, directly to you. In a measured real run, a
complete small feature — written, tested and submitted — cost about **ten cents**.
A retry after a provider outage took it to roughly forty cents. On a flat-rate
subscription there is no per-task charge, and the meaningful limit becomes how
many tasks you are allowed rather than what they cost.

---

## Solid, and not yet

**Proven**

- The full loop works end to end, run live and repeatedly with real accounts.
- It has shipped a fix to *itself* — written, tested, and deployed over its own
  running version.
- The safety check has been proven by deliberately feeding it a broken version,
  which it rejected while the live system kept serving.
- Over seven hundred automated checks pass, including inside the deployed system
  rather than only on a developer's machine.
- It handles real trouble correctly: during testing a provider had an outage
  mid-task, and the manager identified it as temporary capacity rather than bad
  work, retried, and shipped.

**Early — stated plainly**

- **Non-Claude teammates are unproven.** Having OpenAI or Gemini write code is
  built and tested against simulated responses but has never made a real call.
  Beta. Planning and discussion on those providers are proven; building is not.
- **Amazon and Google's enterprise AI services are configurable, not supported.**
  Both need authentication that is not a password pasted into a settings box.
- **Only one cloud provider is implemented.** The seam for others exists but is
  untested by use.
- **The manager does not interview you before it starts.** It plans from the brief
  alone, and asks between sprints but not before the first.

**How the known bugs were found.** Three real defects were caught not by the seven
hundred automated checks but by running the system for real on a staging copy. In
one, a teammate's assigned model silently overrode the manager's decision to
escalate a failing task — so the manager correctly diagnosed the problem, issued a
correction, and the system discarded it and repeated the failure. Automated checks
confirm the things you thought to check; running it for real finds the rest.

---

## Glossary

| Term | Meaning |
|---|---|
| **API key** | A password letting a program use a paid service and billing its owner |
| **Branch** | A parallel version of the code where work happens without affecting anyone |
| **Canary** | Starting a new version privately, with no real traffic, to see if it works |
| **Cluster** | Rented computers managed as one, so programs move without touching machines |
| **Container** | A sealed box holding a program and everything it needs to run |
| **Pull request** | A formal, reviewable proposal to add a change to the main codebase |
| **Repository** | The stored home of a codebase, with the full history of every change |
| **Rollback** | Returning to the previous working version after a bad update |
| **Sprint** | One round of planning, building, testing and shipping |
| **Staging** | A full second copy used to try changes before they reach real users |
| **Test suite** | Automated checks that the software still works. Run by the platform, not the AI |
| **Version control** | The system storing code and remembering every change and who made it |
