# Releasing

`python.yml` builds and tests every pull request and push to `master`. A build
never publishes by itself.

For a release:

1. Keep `VERSION` and `project.version` in `pyproject.toml` equal.
2. Merge the reviewed change into `master`.
3. Create an annotated `vX.Y.Z` tag on that commit and push it.
4. Check the tag workflow. It must pass on Python 3.10 and 3.14.
5. Run the workflow manually with the existing tag and `publish=true`.

The publish job uses the protected `pypi` environment and its
`PYPI_API_TOKEN` secret. It downloads the distributions built by the test job; it
does not rebuild them.

Example:

```bash
gh workflow run python.yml \
  --ref master \
  -f tag=v26.2.0 \
  -f publish=true
```

Verify the new version on PyPI before creating a GitHub Release. Tags, PyPI
publication and the GitHub Release are separate operations.
