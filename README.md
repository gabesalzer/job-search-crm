# Job Search CRM

A personal CRM for running a job search like a revenue pipeline. It captures job
postings (including a paste-a-URL scraper), lets you rate them so the system
learns what you're looking for, and tracks each application through stages the
way a CRM tracks an opportunity — so you can see *where* and *why* you fall off,
which resume versions get traction, and who your champions are.

The data model is deliberately built on a Salesforce analogy:

| This app        | Salesforce analogy |
| --------------- | ------------------ |
| Company         | Account            |
| Job Application | Opportunity        |
| Person          | Contact            |
| Job Posting     | Product            |
| Stage History   | Opportunity field history |

See [`ARCHITECTURE.md`](./ARCHITECTURE.md) for the full data model and the design
reasoning behind every relationship.

## Stack

- **FastAPI** + **SQLAlchemy** + **SQLite** — a real relational backend, single-user,
  zero external database to run.
- Server-rendered UI (Jinja2): a kanban **Pipeline** board, a **Postings** triage
  page, and a **Companies** view — plus a JSON API under `/api` and interactive
  docs at `/docs`.
- A job-posting **scraper**: paste a URL and it fills the fields. Greenhouse and
  Lever are read from their public APIs; other pages via schema.org JobPosting
  data; JS-heavy sites (LinkedIn/Indeed/Workday) work if a Firecrawl key is set.

## Quickstart

First-time setup, from the project folder:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Then open http://127.0.0.1:8000 for the app and http://127.0.0.1:8000/docs for
the interactive API.

### Running it after setup

The steps above are one-time. After that, restarting is just two lines from the
project folder (wherever you keep it) — the app uses relative paths, so its
location doesn't matter:

```bash
source .venv/bin/activate
uvicorn app.main:app --reload
```

Press `Ctrl+C` in that terminal to stop the server.

### Optional: Firecrawl

A Firecrawl key is only needed to scrape JavaScript-heavy boards (LinkedIn,
Indeed, Workday). Company career pages and Greenhouse/Lever/Ashby work without
one. To enable it, put `FIRECRAWL_API_KEY=fc-...` in `.env` and restart.

### Optional: the model-backed features

Three features call the Anthropic API. Put `ANTHROPIC_API_KEY=sk-ant-...` in
`.env` and restart to enable them; set `BRIEF_MODEL` too if you want to pin a
model other than the default. With no key, none of them render and the rest of
the app is entirely unaffected — clone this repo without a key and you get a
working CRM that never contacts anyone.

If a model call comes back with **"anthropic-workspace-id is required when
authenticating with an identity-linked API key"**, the key is a personal or
service-account key that can reach more than one workspace, and the API wants
to be told which. Set `ANTHROPIC_WORKSPACE_ID` alongside the key — find it at
Settings → Workspaces in the Console. It is optional and harmless to leave
unset: a legacy workspace key carries its workspace implicitly. This is a
property of the key rather than of the app, so it can appear on a key rotation
with no deploy in between.

These are the only parts of the project that send your data anywhere. They
differ in *when* they fire and in *how much* they send, which is the thing
worth knowing before you turn them on:

- **The Brief** never fires on its own. The button is the only thing that
  triggers it, and it sends one application.
- **The automatic thread read** fires when you *save* an email thread, without
  a separate press. Saving a thread sends that thread — and the company, role,
  stage and your context note for the application it's attached to — and gets
  back two 0–100 numbers and a one-line reason. It does nothing when you've
  already rated the thread yourself, nothing on a thread with no body, and
  nothing on a save that didn't change the messages. Everything already in
  your database stays untouched until you press *Read it now* on it.
- **Ask** sends the whole pipeline — every application, every meeting
  transcript, every email body — on every question. This is far the widest of
  the three and it is deliberate: a question like "what concerns came up about
  my fit" has no useful answer from one record. Nothing fires until you press
  *Ask*, so it is a button like the Brief, but the button is much bigger. The
  page says so under the composer with the current count, rather than only
  here.

If you'd rather nothing left the box automatically, leave the key unset and
score threads by hand; every other feature works identically.

## Usage

- **Postings** → *New posting*: paste a job URL and hit *Fetch details*, or fill
  it in by hand. The company (Account) is created automatically if it doesn't
  exist yet. Rate postings ▲ / — / ▼ to record what you're looking for.
- **Pipeline**: *New application* → optionally pick a posting to link it and
  auto-fill company + title. Drag cards between stage columns; every move is
  written to stage history. Each application also carries a **Source**
  (Referral / Recruiter Inbound / Outbound — how it originated) and a
  **Context** field: standing notes on why the role is worth pursuing, what
  you know about team, comp, and timeline, and what would make you walk.
  Context is deliberately separate from Notes — Context is durable input
  you'd re-read when judging whether to keep spending effort here, while
  Notes stays a running log of what happened lately.
- **Companies**: employers and staffing agencies, typed so you can later report
  on which employers vs. which agencies get you traction.
- **Meetings**: interviews and calls, attached to an application. Capture the
  summary and transcript by hand, or (with `GRANOLA_API_KEY` set) load and
  import a note from Granola. Every application's edit page shows its
  meetings as a related list, and a meeting's edit page shows which
  application (and, through it, which JD and resume) it belongs to.
- **People**: recruiters, hiring managers, interviewers, referrals. A
  person's company is their *own* employer — independent of whichever
  application they're optionally tied to, so an agency recruiter's company
  is the agency, not the employer you're interviewing at. Mark someone a
  *Champion* to flag they're rooting for you.
- **Emails**: recruiter/hiring-manager email threads, tied to any number of
  People (a thread with a recruiter and a looped-in hiring manager links
  both) and optionally to an Application (unset for pre-application
  outreach, e.g. a cold recruiter message). Paste the thread text in, or
  upload a file — a PDF export of the thread (Gmail's own "Print all") works
  especially well: its text is extracted the same way a resume upload is,
  and if it's shaped like a Gmail export, the subject, participants, and
  thread start/last-message dates are pulled out and pre-filled
  automatically (anything you type by hand always wins over the auto-filled
  value). You don't need to create the People first, either — leave the
  checklist blank and everyone on the other side of the conversation is
  found-or-created by email address (the dedup key: the same address always
  resolves to the same Person, and an auto-created one also gets a Company
  inferred from their email domain, reusing an existing company if one
  already matches). An application's edit page shows an **Activity** related
  list that merges its meetings and email threads into one chronological
  timeline. With `ANTHROPIC_API_KEY` set, saving a thread also **reads** it —
  the two 0–100 fields the Forecast scores get filled in automatically, with a
  one-line reason and a visible note saying a machine wrote them. It is allowed
  to answer "can't tell", which is the right answer for three messages of
  calendar logistics and leaves email out of the forecast rather than dragging
  it down. Type over either number and it becomes yours permanently; nothing
  ever overwrites a rating you entered.
- **Scoring**: every meeting and email thread can carry a **win-likelihood
  score** — 0–100, your best guess at how likely that application is to end
  up Closed Won given what just happened — plus a one-line reason. The score
  lives on the interaction rather than on the application on purpose: a
  single number on the application would only ever tell you where you stand
  now, while a reading per interaction turns the same judgments into a trend
  you can watch (80 → 55 → 30 after an interview that went badly). Each row
  in the timeline shows its own score. Leave it blank when you haven't
  formed a view — blank means unscored, and `0` means you think it's dead;
  they're different answers, and the app keeps them apart. The application
  used to carry a second, separate number rolled up from these; it doesn't
  anymore, because two numbers answering the same question is a tax on every
  glance. The score now feeds the Forecast directly, as the reading for any
  interaction you haven't rated in more detail. What survived the rollup is
  the **age** — how many days since anything at all happened on this
  pursuit — which rides next to the forecast on the board and turns the
  warning colour past two weeks, because an 80 from six weeks ago and an 80
  from yesterday are the same digits describing very different situations.
- **Forecast**: every application carries two forecasts, side by side and
  deliberately independent. **Manual Forecast** is a picklist — Pipeline,
  Best Case, Commit, Closed — that only you write; nothing in the app ever
  overwrites it. **Automated Forecast** derives the same three categories
  from six inputs: how far the pursuit has got (stage), the quality of the
  most recent meeting, the quality of the most recent email thread, how
  closely the resume and the job description overlap, where the application
  came from (a referral is worth a great deal more than an outbound
  application), and whether there's a **champion** inside — someone actually
  spending their own capital to get you hired, which is a much higher bar
  than a friendly interviewer. The weights sum to 100, so the total reads as
  a rough percentage and *Commit* means what it says: more likely than not,
  75 or above. Meetings and email threads both feed this through the same
  two 0–100 fields — **my performance** and **their engagement** — kept
  separate because a strong performance met with flat engagement means
  something very different from the reverse, and scored against separate
  budgets so a scheduling reply can never overwrite what a panel said. The
  champion field is deliberately three-state: yes, no, and not-assessed.
  Answering *no* honestly makes the record read slightly worse than leaving
  it blank, which is the right incentive. The automated read is computed
  fresh on every
  page load and stored nowhere, so it can never be quietly stale, and it
  reports its own confidence alongside the category, because "Pipeline, I
  have nothing to go on" and "Pipeline, I have plenty to go on and it's bad"
  are the same word and different situations — and confidence gates the
  category, so no record reaches *Commit* on setup facts alone, however good
  they look, until something has actually happened. The board flags any card
  where your call and the arithmetic disagree; the edit page shows the full
  six-part breakdown behind the number.
- **Brief**: a written account of an application, in two sections — how this
  started, and what's happened so far. It synthesizes the fields on the
  application, its stage history, the people on it, and the full text of every
  meeting transcript and email thread attached to it. Unlike the Forecast, it
  is never computed on page load: it runs only when you press the button, and
  the result is stored with the timestamp and the model that wrote it, so you
  always know how old it is and what produced it. When activity lands after
  the brief was written, the panel says so and offers to regenerate rather
  than quietly serving a stale account. It deliberately stops at the present
  tense — no prediction, no recommended next steps; that's what the Forecast
  and your own judgment are for. Requires `ANTHROPIC_API_KEY` to be set; with
  no key the panel simply doesn't appear and nothing else about the app
  changes.
- **Next steps**: each application carries a free-text next step, shown as one
  line on its board card so the whole pipeline reads as a to-do list rather
  than a status display. It lives on the Application rather than the Posting,
  because the board renders applications, the posting link is optional, and one
  posting can carry several applications — a next step hung on the ad would be
  shared between re-application attempts and invisible on any card with no
  posting linked. Long text clips with the full value on hover; a card with
  nothing set shows no line at all.
- **Automatic classification**: every application carries a **Seniority**
  (Director+ / Manager) and a **Speciality** (Systems / Strategy / both), read
  from the linked posting's job description. It runs when you link or change
  the posting — not on every save — and never touches a value you set yourself.
  Blank is a real answer: an individual-contributor posting is neither
  Director+ nor Manager, and the classifier declines rather than rounding it
  into the nearer one, because a category padded with roles that don't belong
  in it produces a comparison that's confidently wrong instead of visibly thin.
  Both fields are filterable on Insights.
- **Company lookup**: each company carries a **Funding stage** and an
  **Employee band**, read off its own website on a button press. These are the
  only derived fields in the app that can't be checked against anything already
  on the record, so they're the only ones that store a source URL and a date —
  a two-year-old "Series A" should read as two years old, not as current. The
  classifier is forbidden from filling them from what a model remembers: a
  recalled funding round is frequently stale, can't be cited, and would sit
  beside a URL it didn't come from. If the page doesn't say, the field stays
  blank and the panel tells you it looked. Needs `FIRECRAWL_API_KEY` for
  JavaScript-heavy sites; plain pages work without one.
- **Insights**: the analytics and the chat on one page, because they answer the
  same question from two directions. On top: three durations — how long you
  work an angle in before committing (Staging → Qualification), how long from
  submitting until someone engages (Applied → Discovery), and full cycle time —
  plus a drop-off funnel, a breakdown of why the lost ones were lost, and a
  table of every application with its own timings. Underneath: ask anything
  about the record, including *asking to change what the charts show*
  ("compare referrals against outbound", "just the ones since June").

  The rule that makes the two safe on one screen: **the model chooses which
  records to look at, and never produces a number.** It emits a filter, which
  is validated against the values that actually exist, applied by the same
  code that computes everything else, and drawn on screen as removable chips
  encoded in the URL — so a chat-driven view is a link you can share, bookmark,
  and undo with the back button. A value no record carries is refused and
  reported rather than silently returning an empty cohort. And the chat is
  handed the *computed* figures rather than left to derive them, so it can't
  quietly disagree with the tile six inches above it.

  Every figure carries the number of records behind it, and an average built on
  fewer than three says "not enough data" instead of showing a number that is
  really just one pursuit. The suppression lives in the data, so the JSON API
  can't render one either. The funnel counts an application toward every stage
  it must have passed through, not only the ones with a history row — without
  that a later stage can report more applications than an earlier one, which is
  not a funnel. The charts need no API key; only asking does.
- **A second opinion**: `skills/application-viability/` is a Claude Skill —
  instructions, not code — that reads a transcript, an email thread, or an
  application's context and returns its own 0–100 score with a one-line
  reason, in the shape the score fields expect. It's deliberately *not* part
  of the app: it runs in a chat session you start, on material you hand it.
  No *transcript* reaches a model as a side effect of ordinary use — meetings
  are still yours to judge, and the only automatic read in the app is of email
  threads, which is a narrower thing on purpose: you were in the room for a
  meeting and the transcript is missing what you saw, while a thread is a
  complete artifact. It's also written to ignore any
  score you've already recorded — a second opinion that's read your first one
  isn't one. See [`skills/README.md`](./skills/README.md) to install it.
- **Editing**: every record type (companies, postings, resumes, applications,
  meetings, people, email threads) has an *Edit* link that opens a form
  pre-filled with its current values — available both from each list/board
  view and from the edit page itself. For meetings, the edit page also
  carries the Granola import controls, so you can re-pull or switch a note's
  transcript onto an existing meeting instead of deleting and recreating it.
  For postings, the URL field auto-fetches a new listing's details as soon as
  you change it.
- **Deleting**: every record type also has a *Delete* button, guarded by a
  confirm dialog, available from both the list view and the edit page.
  Deleting a company cascades to its postings, applications, and people;
  deleting an application cascades to its stage history and meetings (and
  clears the link on any email thread pointed at it). Deleting a person just
  unlinks them from any email threads they were on — the threads themselves
  survive, even if that leaves one with no one linked (an orphaned thread is
  a valid state you can clean up by hand, not something the app deletes for
  you). Deleting a posting, resume, or application-link on a person/thread
  just unlinks it from anything that referenced it (nothing else is
  deleted). Stage history has no delete of its own — it's an audit trail,
  cleaned up only as a side effect of deleting its parent application.

## Data & privacy

Your real job-search data lives only in `data/jobsearch.db`, which is
**gitignored** — it never leaves your machine. The repo ships with the schema and
code only, no personal data and no seed data. Your Firecrawl key lives in `.env`,
also gitignored.

If you deploy this (e.g. via `render.yaml`), it's reachable on the open
internet by default, so the app password-gates itself: set `APP_PASSWORD` (and
`APP_USERNAME`) as environment variables and every route requires HTTP Basic
Auth before it'll serve a page. Locally, leave `APP_PASSWORD` unset and there's
no login prompt, since only you can reach `localhost`. Basic Auth with one
shared password is minimal protection — enough to keep a deployed personal
tool off of casual/automated access, not a substitute for a real auth system
if this ever needs to hold more than one person's data.

## Status

Implemented: the full object model, stage-history tracking, the web UI, the JSON
API, and the URL scraper (Greenhouse/Lever APIs, JSON-LD, optional Firecrawl),
posting-first company creation, application↔posting links, resume upload with
text extraction, Meetings with optional Granola import, Email Threads with
Gmail-export auto-parsing, a combined Meetings+Emails activity timeline on
each application, and edit forms for every record type.

Planned next: a Firecrawl-powered bulk *search* for postings, the three-layer
dedup pipeline, richer company enrichment, and the funnel / resume-traction
analytics views. See `ARCHITECTURE.md` for the roadmap and reasoning.
