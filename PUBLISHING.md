# Publishing NetCmd to PyPI

NetCmd is packaged with `pyproject.toml` and a console entry point `netcmd`.
Release publishing uses GitHub Actions **Trusted Publishing (OIDC)**. No PyPI
API token should be committed to this repository.

## First-time PyPI setup (one-time, account owner action)

1. Sign in to https://pypi.org/ and visit
   https://pypi.org/manage/account/publishing/.
2. Under **Add a new pending publisher**, choose **GitHub**.
3. Use these exact values:

   | Field | Value |
   |---|---|
   | PyPI project name | `netcmd` |
   | GitHub owner | `jpesqueda` |
   | Repository name | `NetCmd` |
   | Workflow filename | `publish.yml` |
   | Environment name | `pypi` |

4. Select **Add**. The package name must actually be available on PyPI;
   do not assume that a missing search result reserves it.
5. In GitHub, open **Settings → Environments** and create/configure
   `pypi`. Restrict publication to trusted maintainers and configure
   required reviewers where appropriate.

The publisher is only pending until the first successful upload. Do not
configure PyPI credentials in GitHub Secrets: this workflow uses GitHub OIDC.

## Test and build

```powershell
py -m unittest discover -s tests -v
py -m pip install --upgrade build twine
py -m build
py -m twine check dist/*
```

## Publish the first release

After registration, open GitHub **Actions → Publish NetCmd to PyPI → Run workflow**
and select `main`. The workflow runs tests, builds sdist and wheel, then
uploads them using `pypa/gh-action-pypi-publish`. The `pypi` environment
can require explicit approval. GitHub Releases published later will also
trigger this workflow automatically.

`1.2.0` cannot be uploaded again to PyPI if it was already published.
Increment the package version in `pyproject.toml` **and**
`netcmd.py` before the next upload.

## Install after a successful publication

```powershell
py -m pip install netcmd
netcmd --version
netcmd --help
```

Do not execute network automation against production devices before lab
validation of each platform-specific workflow, especially upgrades and
reconnect sequences.
