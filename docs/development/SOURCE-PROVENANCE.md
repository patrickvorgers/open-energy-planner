# Source provenance

Public development starts from a clean source snapshot. The private development
repository retains earlier commits, reviews, acquisition evidence and CI records.
Its Git objects, runtime data, logs and attachments are not part of this repository.

The signed public Git history records each commit, its parent revisions and its
complete file tree. Git already identifies every file blob. A duplicate file list
does not provide independent evidence that the contents are safe to publish.
The publication audit and transfer report record the source selection, excluded
private inputs and checks performed. Pushed commits must pass GitHub signature
verification and the normal review and candidate CI gates. CI binds its results
and original artifacts to the exact tested tree.

`design/ENGLISH-MIGRATION.json` remains a historical migration descriptor. Its
original Git objects and acquisition fixtures are retained only in the private
archive. They cannot be retrieved from this public repository. Public development
does not claim to revalidate the private English migration or reuse its historical
CI results.

Personal observation fixtures and their derived storage databases are excluded.
Tests added to public development must use independently specified synthetic
inputs. Licences, copyright and third-party attribution remain applicable.
