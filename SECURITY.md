# Security policy

Open Energy Planner is early-stage software. The current development branch is
`main`. There is no stable production release with a security support guarantee.

## Report a vulnerability privately

Use **Security > Report a vulnerability** on this GitHub repository when private
vulnerability reporting is available. Include the affected version, the impact
and a minimal reproduction using synthetic data.

If private reporting is unavailable, open only a minimal issue asking the
maintainer to provide a private reporting channel. Do not include vulnerability
details or an exploit in that public issue.

Never include passwords, access tokens, private keys, real Home Assistant
configuration, private network addresses or personal measurements in public
issues, pull requests, screenshots, logs or attachments. Replace sensitive inputs
with synthetic examples. If a credential has been exposed, revoke or rotate it.
Deleting a message or a file does not invalidate a copied credential.

## Physical control and deployment

Keep hardware safety limits, battery protection and device interlocks independent
of OEP. Its virtual commands and automated tests do not establish physical-device
safety or guaranteed financial savings. Validate a deployment in a test environment
and retain backups before updating a live installation.

Untrusted contributions must not run on a maintainer's personal runner or receive
production credentials, deployment permissions or access to a home network.
