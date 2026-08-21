# G4C-IAC-01 job reliability decision

## Execution nonce

The database bootstrap CLI requires a nonce matching its audited format. The
Container Apps Job template currently embeds `schema-inspect` arguments but no
nonce. A static Bicep parameter would satisfy syntax but would reuse an audit
identity across executions. The repository does not establish a documented
Azure execution-name environment variable that can safely provide uniqueness.

Classification: `BLOCKED_REQUIRES_RUNTIME_DESIGN`.

Do not add a static nonce or silently generate one in the image. The safe next
step is to choose and document an Azure-supported per-execution nonce source,
while retaining the CLI override for controlled manual runs.

## Immutable image reference

The repository constructs immutable image references in the publish workflow
from a registry digest and validates the resolved digest as 64 lowercase hex.
The Bicep parameter is otherwise caller-supplied, so malformed manual input is
not reproducible from repository image construction. `tools/validate_immutable_image.py`
provides a fail-closed guard for deployment tooling and rejects incomplete,
null, empty, or non-hex digests.

Classification: `NOT_REPRODUCIBLE_FROM_REPOSITORY` with a repository-side
validation guard.
