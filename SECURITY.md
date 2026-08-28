# Security policy

Loomcraft is an orchestration library, not an isolation boundary by itself.
Never expose the optional HTTP adapter without host-level authentication,
authorization, tenant isolation, origin/CSRF checks, rate limits, and a sandbox
for untrusted handlers or model processes.

Please report vulnerabilities privately to the repository maintainers rather
than opening a public issue with exploit details. Include the affected version,
minimal reproduction, impact, and a suggested mitigation when available.

