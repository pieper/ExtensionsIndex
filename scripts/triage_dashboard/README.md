# Triage dashboard

A page for maintainers that shows every open extension pull request, whose turn it is, and what the
next step is. It is rebuilt by `.github/workflows/triage-dashboard.yml` whenever a pull request,
comment, review, label, assignment or check changes (and every 15 minutes as a fallback), and served
with GitHub Pages. Open pages check for a new snapshot every 30 seconds and redraw in place.

## How to use it

- Type your GitHub login in **You are**. The page remembers it; `?me=<login>` links work too.
- **Yours** lists PRs assigned to you, PRs where your review was requested, and PRs where the
  contributor answered your last comment. To claim a PR, assign yourself on GitHub.
- **New submissions** lists PRs opened in the last 7 days or not yet answered by any maintainer,
  with source checks that read the extension's CMake and Python files as text. They are hints for a
  human review, not verdicts.
- Approving a PR with a GitHub review marks it **Ready to merge** once its checks pass.
- Leave short, human-readable comments; the latest maintainer comment is shown as "the ask" while
  the PR waits on its contributor.

## Files

- `generate.py`: reads the GitHub API and writes `data.json` and `version.json` (standard library
  only). It reuses the previously published `data.json` as a cache, so a run usually needs only a
  few API calls.
- `site/`: the static page (`index.html`, `style.css`, `app.js`). All text from GitHub is inserted
  as text, never as HTML, and the page's content security policy allows no other origins.

## Run it locally

```
python3 scripts/triage_dashboard/generate.py --out /tmp/triage --default-me <your-login>
cp scripts/triage_dashboard/site/* /tmp/triage/
python3 -m http.server -d /tmp/triage 8000
```

Authentication comes from `GITHUB_TOKEN`, or from `gh auth token` when that is not set.
