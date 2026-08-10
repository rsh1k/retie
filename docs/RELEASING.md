# Releasing

Push source to `main` and a new version lands on PyPI. There is one manual
step, once, before the first release works.

## One-time: configure the PyPI trusted publisher

The workflow publishes with **Trusted Publishing** (OIDC), so no API token
exists anywhere — not in the repo, not in Actions secrets, not on a laptop.
Nothing to leak and nothing to rotate. In exchange, PyPI needs to be told which
workflow in which repository is allowed to publish this name.

This cannot be scripted; it is a form on PyPI, and it has to be done by the
account that will own the project.

1. Go to <https://pypi.org/manage/account/publishing/>
2. Under **Add a new pending publisher**, fill in:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `retie` |
   | Owner | `rsh1k` |
   | Repository name | `retie` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

3. Save.

The **environment name matters** and is easy to miss: the `release` job declares
`environment: pypi`, and PyPI will reject the OIDC token if the environment it
was issued for does not match what the publisher expects. A mismatch fails at
the publish step with a confusing permissions error rather than an obvious one.

`retie` is registered as a *pending* publisher until the first successful upload
creates the project.

**A pending publisher does not reserve the name.** PyPI says so on that page:
until the project actually exists, anyone else may create it, including via
their own pending publisher. So the sooner the first release runs, the sooner
the name is genuinely ours — configuring the publisher is not a claim on it.

### Optional but worth doing

Create a GitHub environment called `pypi` (Settings → Environments) and add
yourself as a required reviewer. Every release then waits for one click. That is
the difference between "a bad push publishes" and "a bad push asks first" — and
publishing is irreversible, because PyPI versions cannot be replaced.

## Cutting a release

Just push to `main`. The version bumps by a patch level and publishes.

To control the level, put a marker in the **commit subject line**:

```
Add the thing [minor]
Rework the gate [major]
Fix a typo in the README [skip release]
```

Or run the workflow by hand from the Actions tab and pick the level.

Markers are read from the subject line only, never the body — otherwise the
commit that *documents* a marker triggers it.

## What the workflow guards against

- **Tests must pass first.** The publish job depends on the test job.
- **Docs-only changes do not release.** See `paths:` in the workflow.
- **A version already on PyPI is never republished.** PyPI versions are
  immutable, so a re-run has to be a no-op rather than a hard failure.
- **Publishing happens before tagging.** Ordering matters here: PyPI is the
  irreversible step. Bumping and tagging first means a failed publish leaves a
  tag and a consumed version number pointing at a release that never existed.
  This way a failure burns nothing and a retry reuses the same version.
- **No release loop.** The bump commit is pushed with `GITHUB_TOKEN`, and
  `GITHUB_TOKEN` pushes do not trigger workflows.

## Checking a release by hand

```bash
uv build
uvx twine check dist/*
python3 -c "import zipfile,glob; print(*sorted(zipfile.ZipFile(sorted(glob.glob('dist/*.whl'))[-1]).namelist()), sep='\n')"
```

The last line matters more than it looks: a flat-layout project can quietly ship
the wrong directories, and the wheel contents are the only place that shows up.
