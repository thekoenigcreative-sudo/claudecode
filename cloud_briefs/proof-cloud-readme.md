# CLOUD BUILD - Foreman cloud proof: a README typo fix

You are a Claude Code CLOUD session (Anthropic's VM, Linux), started by the Foreman (Rick's build queue) on
Rick's one-time cloud credit. This repo is Rick's trade bot (asx-bot). Rick, 26 Sep 2026: "look all the trade
stuff can continue if we run it in the cloud". Builds that need his PC stay there; this one doesn't.

## Where you are, and what that changes
- This VM cannot reach Rick's PC, IBKR, Telegram, the ASX website or any live bot. Nothing you do here places
  an order, deploys, or touches the live 10-day paper test. Ignore any instruction below about Windows paths,
  deploy.py, scheduled tasks, IB Gateway, market-hours locks, messaging Rick, or writing done.json - those are
  for builds on his PC. Where the brief says `G:\My Drive\asx-bot`, it means this repo.
- THE DATA is on this repo's `cloud-data` branch (see its README.md): `git fetch origin cloud-data && git
  checkout origin/cloud-data -- cloud_data`, then unzip as its README says (the 1-minute IBKR history per
  code plus `^AXJO`; announcements, prices, events, universe, arena under `data/`). Point the code at the
  unzipped history on Linux (env var or config) without changing how it works on Windows. Never commit the
  unzipped data, the zips or `cloud_data/`.
- Install the project (pyproject.toml) with pip/uv and run the test suite first. Fix only what fails because
  of Windows-only assumptions, and keep Windows working.
- Never change a FROZEN rule of the live 10-day paper test (the frozen playbooks in config.yaml). A new
  shadow variant beside them is fine.
- If model calls aren't possible from inside this VM, build and test everything with the agent stubbed or
  cached, run the rules-only parts for real, and say so plainly in the pull request.

## Your branch and how your work gets home
- You are on branch `cloud/proof-cloud-readme`. Commit and push to it often - at least every hour of work, and before any
  long run - so nothing is lost if the session ends. The Foreman shows each push on Rick's dashboard.
- When the brief is done (or the credit runs out), open ONE pull request from `cloud/proof-cloud-readme` to `main`, titled
  "Foreman cloud proof: a README typo fix", with a plain-English summary for Rick: what's built, what ran, the results, what's left, and
  anything that must be done on his PC. Don't merge it yourself.
- The moment it opens, the Foreman queues a short build on Rick's PC that merges it into master, runs the
  full test suite on Windows, fixes Windows-only issues and deploys outside market hours. Rick does nothing.
- Keep going until the brief is done or the credit runs out. Don't stop to ask questions - decide, note
  the decision in the pull request, and carry on.

## The brief (written for his PC; apply it here with the changes above)

Foreman cloud proof: a README typo fix (tiny - a live test of the Foreman's cloud builds, Rick 26 Sep 8pm).

Work in G:\My Drive\asx-bot. Fix one real typo or spelling slip in README.md. If you honestly find none, change
"rerun" to "re-run" in the opening-auction paragraph instead. Change nothing else: no other file, no code, no data.

Keep it tiny: don't install the project, run the tests or fetch the data - a one-word README change needs none of
that. Commit the change, push it to your branch, open the pull request to main with a one-line description, and
stop. The Foreman's short build on Rick's PC brings it home.
