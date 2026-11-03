# Source provenance

Public development starts from a clean source snapshot. The private development
repository retains earlier commits, reviews, acquisition evidence and CI records.
Its Git objects, runtime data, logs and attachments are not part of this repository.

`design/PUBLIC-SNAPSHOT.json` identifies the initial signed public source commit
and every file blob in that baseline. `script/ci/check_provenance.py` verifies
complete coverage against that immutable Git tree. Later working file changes do
not rewrite this baseline. Pushed commits must independently pass GitHub signature
verification and the normal review and candidate CI gates.

`design/ENGLISH-MIGRATION.json` remains a historical migration descriptor. Its
original Git objects and acquisition fixtures are retained only in the private
archive. They cannot be retrieved from this public repository. The public snapshot
check establishes a new evidence boundary. It does not claim to revalidate the
private English migration or reuse its historical CI results.

Personal observation fixtures and their derived storage databases are excluded.
Tests added to public development must use independently specified synthetic
inputs. Licences, copyright and third-party attribution remain applicable.
