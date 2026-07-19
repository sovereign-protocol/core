# Security

This alpha is intended for trusted peers and trusted local networks or VPNs.
Direct HTTP is not an Internet-facing security boundary. Connect tokens are
Base64-encoded descriptors, not encryption. Experimental SFTP descriptors may
contain bearer credentials; use a dedicated, jailed, least-privilege account.

Do not open public issues for vulnerabilities. Use GitHub private vulnerability
reporting when enabled, or the maintainer's private contact channel. Include the
affected version, reproduction, impact, and suggested mitigation.
