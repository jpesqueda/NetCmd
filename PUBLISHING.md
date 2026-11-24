# Publishing NetCmd to PyPI

This version is **1.2.1**. Publication requires the GitHub repository to have the updated `netcmd.py` and `pyproject.toml` on the selected commit.

The `.github/workflows/publish.yml` workflow uses PyPI trusted publishing with:
- GitHub owner `jpesqueda`
- repository `NetCmd`
- workflow `publish.yml`
- GitHub environment `pypi`

After reviewing the changes and tests, push them to `main`, then run the workflow through GitHub Actions (or publish a GitHub Release). No PyPI tokens should be committed. Increment the version for each new PyPI upload: PyPI does not permit overwriting an already uploaded distribution.

Before publication:

```powershell
py -m unittest discover -s tests -v
py -m pip install --upgrade build twine
py -m build
py -m twine check dist/*
```

The workflow uses an OIDC token tied to the trusted publisher. For production use, configure GitHub environment protection rules and manually approve publishing.
