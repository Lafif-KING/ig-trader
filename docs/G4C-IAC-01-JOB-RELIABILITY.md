# G4C-IAC-01 job reliability decision

## Execution nonce

The database bootstrap CLI requires a nonce matching its audited format.
Azure Container Apps Jobs supplies the unique execution name through
`CONTAINER_APP_JOB_EXECUTION_NAME`.

Classification: `RESOLVED`.

Resolution precedence is an explicit `--execution-nonce` value, then
`CONTAINER_APP_JOB_EXECUTION_NAME`, otherwise fail closed. The selected value
always passes the existing nonce validator. A malformed explicit value never
falls back, and no random or static nonce is generated.

## Immutable image reference

The repository constructs immutable image references in the publish workflow
from a registry digest and validates the resolved digest as 64 lowercase hex.
The Bicep parameter is otherwise caller-supplied, so malformed manual input is
not reproducible from repository image construction. `tools/validate_immutable_image.py`
provides a fail-closed guard for deployment tooling and rejects incomplete,
null, empty, or non-hex digests.

`tools/codex/deploy-db-bootstrap.ps1` is the repository deployment entry point.
It runs the validator before invoking `az deployment group create`; validation
failure therefore occurs before any Azure mutation.

Classification: `NOT_REPRODUCIBLE_FROM_REPOSITORY` with a repository-side
validation guard.
