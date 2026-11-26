# Proposal-engine fixtures

`proposal-labels.json` is the ground truth `curiarium/api/proposal_engine.py` scores
itself against: folders the operator confirmed by creating a project from them, and
the three folders he confirmed are collections. The module that owns `validate()`
is that engine; `curiarium/scripts/propose_import.py` is a shim over it, so the
command below still runs through the script and still scores the same rules.

Keys are **root-relative**, so the same file works against any library root:

```
.venv/bin/python curiarium/scripts/propose_import.py --root /mnt/ceph/3D_models \
    --projects /tmp/project-keys.json \
    --labels curiarium/scripts/fixtures/proposal-labels.json --validate
```

`--facts KEY` dumps the whole fact sheet (metadata, provenance, groups, twin,
cover, documents) with every section marked `kind: fact` or `kind: suggestion`;
`--explain KEY` adds every option's score and confidence. Both run under the repo
virtualenv because the engine reads PDFs with `pypdf` (see below).

`--projects` is a local export of the live project keys (one `{"key", "project"}`
object per project, taken read-only from `Project.metadata_json -> _bundle.key`).
It is not committed: it names the owner's whole library.

Exit code is 1 when a confirmed single product would be split, or a confirmed
collection would be merged — so the fixture is a regression gate for the rules,
run locally against the real tree. Rules change; the labels do not.

These labels are also replayed as tests in
`curiarium/api/tests/test_proposal_engine.py` over synthetic trees, for six of the
seven keys: a rule change that would start splitting a confirmed product reds on
CI without the live library. The seventh, the git-checkout label
`import/endorphin`, is only scored by the host CLI against a real checkout (a
`.git` directory with nothing behind it is no repo). Both previously-wrong wrapper
layouts are asserted there too: a twin zip INSIDE the wrapper child, and the case
where the candidate IS the wrapper. Both must print `drop the wrapper`,
`<title>/_source/<zip>` and `keep "<part>/" as a part`.

`expect` is one of:

| value | meaning |
|---|---|
| `one` | the recommended shape must be one project (folder or git) |
| `split` | the recommended shape must be split, with `count` top folders |
| `arrange` | only the `arrange_contains` substrings are asserted |

`arrange_contains` asserts text the proposal must print, so a rename or
flattening rule cannot silently disappear.

## Running the harness

The engine is app-free (no FastAPI, no database) but it is no longer stdlib-only:
it uses `pypdf` for PDF text, document info and link annotations. Run the harness
with the repo's virtualenv, which installs `curiarium/api/requirements*.lock`:

```
.venv/bin/python curiarium/scripts/propose_import.py --only Vault
```

"App-free" is the property that matters and is tested: the engine imports no app
module, so it runs outside a request, outside a database, and outside Docker.
