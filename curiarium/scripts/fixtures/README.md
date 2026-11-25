# Proposal-engine fixtures

`proposal-labels.json` is the ground truth `curiarium/scripts/propose_import.py`
scores itself against: folders the operator confirmed by creating a project from
them, and the three folders he confirmed are collections.

Keys are **root-relative**, so the same file works against any library root:

```
python3 curiarium/scripts/propose_import.py --root /mnt/ceph/3D_models \
    --projects /tmp/project-keys.json \
    --labels curiarium/scripts/fixtures/proposal-labels.json --validate
```

`--projects` is a local export of the live project keys (one `{"key", "project"}`
object per project, taken read-only from `Project.metadata_json -> _bundle.key`).
It is not committed: it names the owner's whole library.

Exit code is 1 when a confirmed single product would be split, or a confirmed
collection would be merged — so the fixture is a regression gate for the rules,
run locally against the real tree. Rules change; the labels do not.

`expect` is one of:

| value | meaning |
|---|---|
| `one` | the recommended shape must be one project (folder or git) |
| `split` | the recommended shape must be split, with `count` top folders |
| `arrange` | only the `arrange_contains` substrings are asserted |

`arrange_contains` asserts text the proposal must print, so a rename or
flattening rule cannot silently disappear.
