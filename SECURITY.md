# Security policy

## Supported versions

Only the latest minor release (currently 0.8.x) receives fixes. Please update before reporting.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the **Security** tab of this repository and choose **Report a vulnerability**. Please do not open a public issue or discussion for a security problem. Include the PVArr version, how you run it, and steps to reproduce. This is a single-maintainer project, so replies are best effort.

## Known boundary: no authentication

PVArr has no login. Anything that can reach port 8999 can start, stop and delete recordings and make PVArr fetch URLs. It must stay on a trusted LAN and must not be port-forwarded; see [the note in Quick start](README.md#quick-start). If it has to leave your network, put it behind a reverse proxy that does authentication. Reports that amount to "an unauthenticated caller on the LAN can use the API" are this documented boundary, not a vulnerability.
