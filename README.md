# github-mirror

A small, self-hosted service that keeps a **one-way, read-only backup** of all your GitHub repositories
on GitLab (GitLab.com or a self-managed instance). GitLab Free has no pull mirroring, so this container
does the work itself. Python standard library only, no dependencies beyond `git`.

On startup and then every `SYNC_INTERVAL` it:

1. Lists the GitHub repositories you own (`/user/repos?affiliation=owner`, private included), applying the
   fork / archived / exclude filters.
2. Finds or creates `GITLAB_NAMESPACE/<repo>` on GitLab. New projects are private by default and get the
   `github-mirror` topic plus a `[github-mirror]` description marker. CI is disabled on them.
3. Updates a bare mirror clone in `/data/repos/<repo>.git`, fetching only `refs/heads/*` and `refs/tags/*`
   (GitHub's `refs/pull/*` is never fetched or pushed).
4. Pushes to GitLab with explicit refspecs. New and updated refs go first (without prune). Then GitLab's default branch is
   set to match GitHub's and force push is allowed on it. Then a second push **with prune** removes
   branches and tags deleted on GitHub. This order also handles a renamed default branch (`master` → `main`).
5. Prints a summary line.

It never pushes anything back to GitHub and never deletes GitLab projects.

## Quick start

You need two tokens:

- **GitHub:** a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to
  *All repositories* and the permissions **Contents: read-only** and **Metadata: read-only**.
  A classic token with the `repo` scope also works.
- **GitLab:** a [personal access token](https://gitlab.com/-/user_settings/personal_access_tokens) with the `api` scope.

### With Docker Compose

```sh
git clone https://github.com/Anson10124/github-mirror.git
cd github-mirror
cp .env.example .env          # then fill in your tokens and namespace
docker compose up -d --build
docker compose logs -f
```

Tip: set `DRY_RUN=true` for the first start to see what would be created and pushed, without writing anything.

### With plain Docker

```sh
docker build -t github-mirror .
docker run -d --name github-mirror --restart unless-stopped \
  -e GITHUB_TOKEN=... \
  -e GITLAB_TOKEN=... \
  -e GITLAB_NAMESPACE=your-gitlab-username \
  -v github-mirror-data:/data \
  github-mirror
```

### On a PaaS (Coolify, Dokploy, CapRover, …)

Deploy the repository as a Dockerfile application, set the environment variables below in the platform's UI,
and attach a persistent volume at **`/data`**. No port needs to be exposed.

## Safety rules

- **Ownership:** a GitLab project is only written to if it has the `github-mirror` topic **or** the
  `[github-mirror]` description marker. A same-named project without either is skipped with a WARNING
  (`skipped_foreign` in the summary) unless `ADOPT_EXISTING=true`.
- **Credentials:** tokens live only in environment variables. Git gets them through `GIT_ASKPASS`, one
  command at a time. They never appear in URLs, git config, the image, or logs (logs are also redacted as a backstop).
- **One run at a time:** a file lock in `/data/.lock` stops runs overlapping, even with a manual `once`.
- **Rate limits:** GitHub/GitLab 429s and GitHub primary/secondary limits are retried with backoff,
  honouring `Retry-After` / reset headers.
- **Isolation:** one failing repo is logged and the others carry on.
- **Removed repos:** repos that disappear from GitHub are logged as `no longer on GitHub`. Their GitLab project and local copy are kept.

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `GITHUB_TOKEN` | **required** | GitHub token with read access to all your repos. Fine-grained: *All repositories*, Contents read-only + Metadata read-only. Classic: `repo`. |
| `GITLAB_TOKEN` | **required** | GitLab personal access token with the `api` scope. |
| `GITLAB_NAMESPACE` | **required** | GitLab user or group to mirror into, e.g. `your-username` or `your-group/backups`. |
| `GITLAB_URL` | `https://gitlab.com` | GitLab instance. |
| `SYNC_INTERVAL` | `1h` | Time between runs: `3600`, `30m`, `1h`, `1h30m` (minimum 60 s). |
| `INCLUDE_FORKS` | `false` | Mirror forks too. |
| `INCLUDE_ARCHIVED` | `true` | Mirror archived repos. |
| `EXCLUDE_REPOS` | *(empty)* | Comma-separated repo names to skip. Case-insensitive, `*` wildcards allowed. |
| `GITLAB_VISIBILITY` | `private` | Visibility for **newly created** projects: `private`, `internal`, `public`. |
| `ENABLE_LFS` | `false` | Also mirror Git LFS objects (`git lfs fetch --all` / `git lfs push --all`). |
| `DRY_RUN` | `false` | Only print what would be created/pushed. Makes read-only API calls and `git ls-remote`, writes nothing. |
| `ADOPT_EXISTING` | `false` | Take over existing GitLab projects that lack the marker (adds the topic, then force-syncs them). |

The persistent volume must be mounted at **`/data`**.

### Adding an exclusion

Edit `EXCLUDE_REPOS` (in `.env`, or in your platform's environment settings), e.g.

```
EXCLUDE_REPOS=huge-dataset,old-experiment-*,dotfiles
```

then restart the container. Excluding a repo stops future syncs only. The GitLab project and
`/data/repos/<name>.git` are left as they are.

### Name mapping

GitLab project paths can't start with `.` or `-` or end in `.git`/`.atom`, so names are mapped predictably
(and each mapping is logged as `name mapping: A -> B`):

- characters outside `A-Z a-z 0-9 . _ -` become `-`
- a leading `.` becomes `dot-` (`.github` → `dot-github`); a leading `-` becomes `dash-`
- a trailing `.git` / `.atom` gets `-repo` appended

## Reading the logs

Use `docker compose logs -f` (or your platform's log viewer). Every line is `UTC-timestamp LEVEL message`.
A normal run looks like:

```
INFO    GitHub: 25 repositories owned, 23 selected, 2 filtered out
INFO    skip some-fork: fork (INCLUDE_FORKS=false)
INFO    my-website: up to date
INFO    my-app: synced your-username/my-app (2 updated, 0 deleted)
INFO    Run finished in 18.2s: created=0 synced=1 unchanged=22 empty=0 skipped_filtered=2 skipped_foreign=0 failed=0 disappeared=0
INFO    Next run in 3582s
```

- **The summary line** (`Run finished …` or `DRY RUN finished …`) is the one to look at. Search the logs for `Run finished`.
- `WARNING skipped_foreign …`: a same-named GitLab project exists that wasn't created by the mirror.
- `ERROR failed <repo> …`: that repo failed (the reason follows). The run continues, and it's retried next interval.
- `WARNING … no longer on GitHub`: the repo was deleted or renamed on GitHub. Delete
  `/data/repos/<name>.git` (and the GitLab project, by hand, if you want) to stop the warning.

### Commands inside the container

Run these with `docker compose exec github-mirror <command>` (or your platform's terminal):

```sh
python -m mirror once         # one sync right now (skipped if a scheduled run is in progress)
python -m mirror healthcheck  # what the Docker healthcheck sees
cat /data/state.json          # last run time, last summary, failed repos
```

## Health

The Docker `HEALTHCHECK` reports **unhealthy** if the last *fully successful* run (no failed repos) is
older than 3 × `SYNC_INTERVAL`. Before the first success, the limit counts from container start.
So a repo that keeps failing shows up as an unhealthy container in `docker ps` or your platform's dashboard.

## Development

Python 3.10+, standard library only. The tests use real git against local repos and a fake GitLab whose
server hooks reject pushes the way GitLab does:

```sh
python3 -m unittest discover -s tests -t . -v
```

## License

[MIT](LICENSE)
